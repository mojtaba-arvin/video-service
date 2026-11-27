"""Application-level Job submission use case.

This module deliberately knows nothing about gRPC, protobuf, Redis or Celery.
Transport adapters translate requests into this command and retain ownership
of transport-specific response formatting and legacy dispatch.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from video_streaming.security import AuthorizationError, require_scope


@dataclass(frozen=True)
class SubmitJobCommand:
    job_id: str
    idempotency_key: str
    request_hash: str
    reference_id: str
    execution_plan: Dict[str, Any]
    execution_backend: str
    enqueue_dispatch: bool
    outbox_dedupe_key: Optional[str]
    principal: Any
    project_id: Optional[str]
    project_revision_id: Optional[str]
    secret_envelopes: Sequence[Any]
    quota_enforcement_enabled: bool
    metadata: Dict[str, Any]


@dataclass(frozen=True)
class SubmitJobResult:
    job_id: str
    created: bool


class SubmitJobService:
    """Authorize and durably register a Job through the workflow repository."""

    def __init__(self, repository):
        if repository is None:
            raise ValueError("workflow repository is required")
        self._repository = repository

    @staticmethod
    def authorize(principal, project_id=None, project_revision_id=None) -> None:
        """Fail before request expansion, encryption or other expensive work."""
        require_scope(principal, "video.jobs:create")
        if bool(project_id) != bool(project_revision_id):
            raise ValueError(
                "project_id and project_revision_id must be supplied together"
            )
        if project_revision_id:
            require_scope(principal, "video.projects:read")

    def execute(self, command: SubmitJobCommand) -> SubmitJobResult:
        if not command.job_id:
            raise ValueError("job_id is required")
        if not command.idempotency_key:
            raise ValueError("idempotency_key is required")
        if not command.request_hash:
            raise ValueError("request_hash is required")
        if not command.execution_plan:
            raise ValueError("execution_plan is required")

        self.authorize(
            command.principal,
            command.project_id,
            command.project_revision_id,
        )

        existing_job_id = self._repository.find_idempotent_job(
            command.idempotency_key,
            command.request_hash,
            owner_principal=command.principal,
        )
        if existing_job_id is not None:
            return SubmitJobResult(job_id=existing_job_id, created=False)

        job_id, created = self._repository.create_job(
            job_id=command.job_id,
            idempotency_key=command.idempotency_key,
            request_hash=command.request_hash,
            reference_id=command.reference_id,
            workflow_name="video.vod",
            execution_plan=command.execution_plan,
            execution_backend=command.execution_backend,
            enqueue_dispatch=command.enqueue_dispatch,
            outbox_dedupe_key=command.outbox_dedupe_key,
            owner_principal=command.principal,
            project_id=command.project_id,
            project_revision_id=command.project_revision_id,
            secret_envelopes=command.secret_envelopes,
            quota_enforcement_enabled=command.quota_enforcement_enabled,
            metadata=command.metadata,
        )
        return SubmitJobResult(job_id=job_id, created=created)


class GetJobStatusService:
    """Authorize a batch of Job reads before returning durable snapshots."""

    def __init__(self, repository):
        if repository is None:
            raise ValueError("workflow repository is required")
        self._repository = repository

    def execute(self, job_ids, principal):
        job_ids = tuple(job_ids)
        # Preserve the adapter's all-or-nothing authorization boundary: no
        # snapshot (or Redis projection) is read until every Job is authorized.
        for job_id in job_ids:
            self._repository.authorize_job_access(
                job_id, principal, "video.jobs:read"
            )
        return {
            job_id: self._repository.get_job_snapshot(job_id)
            for job_id in job_ids
        }



class VideoOperationService:
    """Typed asynchronous video operations shared by MCP and other adapters.

    The service compiles only allow-listed durable plan operations and submits
    them through SubmitJobService. It never accepts commands, FFmpeg argument
    strings, provider credentials, or arbitrary execution-plan fragments.
    """

    def __init__(
            self, repository, durable_dispatch_enabled,
            quota_enforcement_enabled=False):
        if repository is None:
            raise ValueError("workflow repository is required")
        self._repository = repository
        self._durable_dispatch_enabled = bool(durable_dispatch_enabled)
        self._quota_enforcement_enabled = bool(quota_enforcement_enabled)
        self._submit = SubmitJobService(repository)

    @staticmethod
    def _required_text(value, field, maximum=255):
        if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > maximum
                or any(ord(char) < 32 for char in value)):
            raise ValueError("{} must be 1 to {} safe characters".format(
                field, maximum
            ))
        return value.strip()

    @classmethod
    def _base_request(cls, bucket, key, idempotency_key):
        return {
            "bucket": cls._required_text(bucket, "bucket"),
            "key": cls._required_text(key, "key", maximum=1024),
            "idempotency_key": cls._required_text(
                idempotency_key, "idempotency_key"
            ),
        }

    @staticmethod
    def _probe_plan(job_id, bucket, key):
        from video_streaming.workflows.execution_plan import (
            EXECUTION_PLAN_SCHEMA_VERSION,
            validate_execution_plan,
        )
        from video_streaming.workflows.vod_plan import DEFAULT_RETRY_POLICIES

        def node(node_id, operation, dependencies, parameters, retry, profile):
            return {
                "node_id": node_id,
                "operation": operation,
                "stage_key": node_id,
                "depends_on": dependencies,
                "parameters": parameters,
                "retry_policy": dict(DEFAULT_RETRY_POLICIES[retry]),
                "resource_profile": profile,
            }

        input_parameters = {
            "input_name": "video",
            "input_number": 0,
            "bucket": bucket,
            "key": key,
        }
        plan = {
            "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
            "workflow_name": "video.vod",
            "workflow_version": "1",
            "job_id": job_id,
            "nodes": [
                node(
                    "input-video-check", "video.input.check", [],
                    input_parameters, "control", "control",
                ),
                node(
                    "input-video-download", "video.input.download",
                    ["input-video-check"], input_parameters,
                    "input", "upload",
                ),
                node(
                    "input-video-analyze", "video.input.analyze",
                    ["input-video-download"],
                    {"input_name": "video", "input_number": 0},
                    "media", "probe",
                ),
            ],
        }
        return validate_execution_plan(plan)

    def _submit_plan(self, operation, request, principal, plan):
        if (
                not isinstance(principal.tenant_id, str)
                or not principal.tenant_id.strip()
                or not principal.issuer
                or not principal.subject):
            raise AuthorizationError(
                "Tenant-bound caller identity is required"
            )
        if not self._durable_dispatch_enabled:
            raise RuntimeError(
                "MCP video submission requires WORKFLOW_DURABLE_DISPATCH_ENABLED"
            )
        if not self._quota_enforcement_enabled:
            raise RuntimeError(
                "MCP video submission requires TENANT_JOB_QUOTA_ENFORCEMENT_ENABLED"
            )
        job_id = plan["job_id"]
        # The request hash is stable across retries and independent of generated
        # Job IDs, so the caller's idempotency key safely deduplicates submits.
        request_payload = dict(request)
        caller_idempotency_key = request_payload.pop("idempotency_key")
        owner_scope = "\\0".join((
            principal.tenant_id, principal.issuer, principal.subject,
            caller_idempotency_key,
        ))
        scoped_idempotency_key = "mcp:" + hashlib.sha256(
            owner_scope.encode("utf-8")
        ).hexdigest()
        canonical = json.dumps(
            {"operation": operation, "request": request_payload},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")
        request_hash = hashlib.sha256(canonical).hexdigest()
        nodes = plan["nodes"]
        playlist_ids = [
            node["stage_key"] for node in nodes
            if node["operation"] == "video.output.upload_directory"
        ]
        thumbnail_ids = [
            node["stage_key"] for node in nodes
            if node["operation"] == "video.output.upload_file"
        ]
        result_details = {
            "reference_id": job_id,
            "total_checks": sum(
                node["operation"] in (
                    "video.input.check",
                    "video.output.bucket.check",
                    "video.output.key.check",
                ) for node in nodes
            ),
            "total_inputs": sum(
                node["operation"] == "video.input.download" for node in nodes
            ),
            "total_outputs": len(playlist_ids) + len(thumbnail_ids),
            "no_watermarked_playlists_ids": playlist_ids,
            "watermarked_playlists_ids": [],
            "no_watermarked_thumbnails_ids": thumbnail_ids,
            "watermarked_thumbnails_ids": [],
            "watermarked_outputs_ids": [],
        }
        result = self._submit.execute(SubmitJobCommand(
            job_id=job_id,
            idempotency_key=scoped_idempotency_key,
            request_hash=request_hash,
            reference_id=job_id,
            execution_plan=plan,
            execution_backend="durable_vod_v1",
            enqueue_dispatch=True,
            outbox_dedupe_key="mcp.workflow.dispatch:{}".format(job_id),
            principal=principal,
            project_id=None,
            project_revision_id=None,
            secret_envelopes=(),
            quota_enforcement_enabled=self._quota_enforcement_enabled,
            metadata={
                "mcp_operation": operation,
                "api_version": 1,
                "result_details": result_details,
            },
        ))
        return {
            "job_id": result.job_id,
            "submission_status": "accepted",
            "created": result.created,
            "operation": operation,
        }

    def probe_video(self, bucket, key, idempotency_key, principal):
        request = self._base_request(bucket, key, idempotency_key)
        job_id = str(uuid.uuid4())
        plan = self._probe_plan(job_id, request["bucket"], request["key"])
        return self._submit_plan("probe", request, principal, plan)

    def generate_thumbnail(
            self, bucket, key, output_bucket, output_key, thumbnail_time,
            idempotency_key, principal, scale_width=0, scale_height=0):
        request = self._base_request(bucket, key, idempotency_key)
        output_bucket = self._required_text(output_bucket, "output_bucket")
        output_key = self._required_text(output_key, "output_key", 1024)
        for field, value in (
                ("scale_width", scale_width), ("scale_height", scale_height)):
            if (
                    isinstance(value, bool) or not isinstance(value, int)
                    or value < 0 or value > 16384):
                raise ValueError("{} must be between 0 and 16384".format(field))
        request.update({
            "output_bucket": output_bucket,
            "output_key": output_key,
            "thumbnail_time": thumbnail_time,
            "scale_width": scale_width,
            "scale_height": scale_height,
        })
        from video_streaming.workflows.vod_plan import build_vod_execution_plan
        job_id = str(uuid.uuid4())
        plan = build_vod_execution_plan(
            job_id,
            {
                "video": {"bucket": request["bucket"], "key": request["key"]},
                "watermark": {},
                "playlists": [],
                "thumbnails": [{
                    "bucket": output_bucket,
                    "key": output_key,
                    "thumbnail_time": thumbnail_time,
                    "output_id": "mcp-thumbnail",
                    "scale_width": scale_width,
                    "scale_height": scale_height,
                    "use_watermark": False,
                    # MCP does not authorize destructive overwrites.
                    "dont_replace": True,
                    "create_bucket": False,
                }],
            },
        )
        return self._submit_plan("thumbnail", request, principal, plan)

    def transcode_video(
            self, bucket, key, output_bucket, output_key, protocol,
            idempotency_key, principal, quality_names=None,
            encode_format="h264"):
        request = self._base_request(bucket, key, idempotency_key)
        output_bucket = self._required_text(output_bucket, "output_bucket")
        output_key = self._required_text(output_key, "output_key", 1024)
        if isinstance(protocol, str):
            protocol = protocol.lower()
        if protocol not in ("hls", "dash"):
            raise ValueError("protocol must be hls or dash")
        quality_names = [] if quality_names is None else quality_names
        if (
                not isinstance(quality_names, (list, tuple))
                or len(quality_names) > 20
                or any(
                    not isinstance(item, str) or not item.strip()
                    or len(item) > 32
                    or any(ord(char) < 32 for char in item)
                    for item in (quality_names or [])
                )):
            raise ValueError("quality_names must be a list of at most 20 safe names")
        encode_format = self._required_text(
            encode_format, "encode_format", maximum=32
        ).lower()
        if encode_format not in ("h264", "hevc", "vp9"):
            raise ValueError("encode_format must be h264, hevc, or vp9")
        request.update({
            "output_bucket": output_bucket,
            "output_key": output_key,
            "protocol": protocol,
            "quality_names": list(quality_names or []),
            "encode_format": encode_format,
        })
        from video_streaming.workflows.vod_plan import build_vod_execution_plan
        job_id = str(uuid.uuid4())
        plan = build_vod_execution_plan(
            job_id,
            {
                "video": {"bucket": request["bucket"], "key": request["key"]},
                "watermark": {},
                "playlists": [{
                    "bucket": output_bucket,
                    "key": output_key,
                    "protocol": protocol,
                    "quality_names": list(quality_names or []),
                    "output_id": "mcp-transcode",
                    "encode_format": encode_format,
                    "video_codec": None,
                    "audio_codec": None,
                    "fragmented": False,
                    "custom_qualities": [],
                    "use_watermark": False,
                    # Existing objects are never overwritten by MCP.
                    "dont_replace": True,
                    "create_bucket": False,
                }],
                "thumbnails": [],
            },
        )
        return self._submit_plan("transcode", request, principal, plan)


@dataclass(frozen=True)
class CancelJobResult:
    job_id: str
    status: str
    requested: bool


class CancelJobService:
    """Request durable Job cancellation through the shared workflow port."""

    def __init__(self, repository):
        if repository is None:
            raise ValueError("workflow repository is required")
        self._repository = repository

    def execute(self, job_id, principal, reason="CANCELLED"):
        if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 36:
            raise ValueError("job_id must contain 1 to 36 characters")
        if (
                not isinstance(reason, str)
                or not reason.strip()
                or len(reason) > 500
                or any(ord(char) < 32 for char in reason)):
            raise ValueError("reason must contain 1 to 500 characters")
        require_scope(principal, "video.jobs:cancel")
        result = self._repository.request_job_cancellation(
            job_id, principal, reason=reason.strip()
        )
        return CancelJobResult(
            job_id=result["job_id"],
            status=result["status"],
            requested=result["requested"],
        )
