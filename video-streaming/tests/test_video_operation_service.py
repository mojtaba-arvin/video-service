"""Unit tests for typed application operations and MCP identity mapping."""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from video_streaming.security import AuthorizationError, RequestPrincipal
from video_streaming.workflows.application import (
    CancelJobService,
    VideoOperationService,
)
from video_streaming.workflows.domain import IdempotencyConflict
from video_streaming.mcp.server import _principal_from_access_token


class FakeRepository:
    def __init__(self):
        self.jobs = {}
        self.commands = []
        self.cancellations = []

    def find_idempotent_job(self, key, request_hash, owner_principal=None):
        existing = self.jobs.get((owner_principal.tenant_id, key))
        if existing is None:
            return None
        if existing["request_hash"] != request_hash:
            raise IdempotencyConflict("conflict")
        return existing["job_id"]

    def create_job(self, **kwargs):
        self.commands.append(kwargs)
        key = (kwargs["owner_principal"].tenant_id, kwargs["idempotency_key"])
        self.jobs[key] = {
            "job_id": kwargs["job_id"],
            "request_hash": kwargs["request_hash"],
        }
        return kwargs["job_id"], True

    def request_job_cancellation(self, job_id, principal, reason):
        self.cancellations.append((job_id, principal.subject, reason))
        return {"job_id": job_id, "status": "CANCEL_REQUESTED", "requested": True}


def principal(scopes=None):
    return RequestPrincipal(
        issuer="https://issuer.example.test/",
        subject="mcp-user",
        tenant_id="tenant-mcp",
        scopes=frozenset(scopes or ["video.jobs:create", "video.jobs:read"]),
        roles=frozenset(),
        authentication_method="bearer",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


class VideoOperationServiceTests(unittest.TestCase):
    def setUp(self):
        self.repository = FakeRepository()
        self.owner = principal()
        self.service = VideoOperationService(
            self.repository,
            durable_dispatch_enabled=True,
            quota_enforcement_enabled=True,
        )

    def test_probe_submits_a_typed_async_plan_with_stable_job_identity(self):
        result = self.service.probe_video(
            "input-bucket", "source/video.mp4", "probe-idempotency", self.owner
        )
        self.assertEqual(result["submission_status"], "accepted")
        self.assertTrue(result["created"])
        command = self.repository.commands[-1]
        plan = command["execution_plan"]
        self.assertEqual(plan["job_id"], command["job_id"])
        self.assertEqual(
            [node["operation"] for node in plan["nodes"]],
            [
                "video.input.check",
                "video.input.download",
                "video.input.analyze",
            ],
        )
        self.assertEqual(command["execution_backend"], "durable_vod_v1")
        self.assertTrue(command["enqueue_dispatch"])
        self.assertTrue(command["quota_enforcement_enabled"])
        self.assertNotIn("ffmpeg_args", str(plan))
        self.assertNotIn("provider_credentials", str(plan))

    def test_thumbnail_uses_existing_allowlisted_plan_operations(self):
        self.service.generate_thumbnail(
            "input-bucket", "source/video.mp4",
            "output-bucket", "thumbs/cover.jpg",
            "00:00:12", "thumbnail-idempotency", self.owner,
            scale_width=640, scale_height=360,
        )
        plan = self.repository.commands[-1]["execution_plan"]
        operations = [node["operation"] for node in plan["nodes"]]
        self.assertIn("video.thumbnail.generate", operations)
        self.assertIn("video.output.upload_file", operations)
        thumbnail = next(
            node for node in plan["nodes"]
            if node["operation"] == "video.thumbnail.generate"
        )
        self.assertEqual(thumbnail["parameters"]["thumbnail_time"], "00:00:12")
        self.assertEqual(thumbnail["parameters"]["scale_width"], 640)
        self.assertNotIn("command", thumbnail["parameters"])

    def test_transcode_accepts_only_typed_protocol_and_codec_choices(self):
        self.service.transcode_video(
            "input-bucket", "source/video.mp4",
            "output-bucket", "vod/master.m3u8",
            "hls", "transcode-idempotency", self.owner,
            quality_names=["360p", "720p"], encode_format="hevc",
        )
        plan = self.repository.commands[-1]["execution_plan"]
        package = next(
            node for node in plan["nodes"]
            if node["operation"] == "video.hls.package"
        )
        self.assertEqual(package["parameters"]["protocol"], "hls")
        self.assertEqual(package["parameters"]["encode_format"], "hevc")
        self.assertEqual(package["parameters"]["quality_names"], ["360p", "720p"])
        self.assertNotIn("ffmpeg_args", package["parameters"])

        with self.assertRaises(ValueError):
            self.service.transcode_video(
                "input-bucket", "source/video.mp4",
                "output-bucket", "out", "custom",
                "bad-protocol", self.owner,
            )
        with self.assertRaises(ValueError):
            self.service.transcode_video(
                "input-bucket", "source/video.mp4",
                "output-bucket", "out", "hls",
                "bad-codec", self.owner, encode_format="arbitrary-command",
            )

    def test_idempotent_replay_returns_original_job_without_second_dispatch(self):
        first = self.service.probe_video(
            "input-bucket", "source/video.mp4", "same-key", self.owner
        )
        second = self.service.probe_video(
            "input-bucket", "source/video.mp4", "same-key", self.owner
        )
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(len(self.repository.commands), 1)

    def test_submission_fails_closed_when_durable_dispatch_is_disabled(self):
        disabled = VideoOperationService(
            self.repository, durable_dispatch_enabled=False
        )
        with self.assertRaisesRegex(RuntimeError, "DURABLE_DISPATCH_ENABLED"):
            disabled.probe_video(
                "input-bucket", "source/video.mp4", "disabled-key", self.owner
            )
        self.assertFalse(self.repository.commands)

    def test_cancel_service_requires_scope_and_uses_shared_repository(self):
        service = CancelJobService(self.repository)
        result = service.execute("job-123", self.owner, "operator request")
        self.assertEqual(result.status, "CANCEL_REQUESTED")
        self.assertTrue(result.requested)
        self.assertEqual(
            self.repository.cancellations,
            [("job-123", "mcp-user", "operator request")],
        )
        with self.assertRaises(AuthorizationError):
            service.execute(
                "job-456", principal(["video.jobs:read"]), "unauthorized"
            )


class MCPIdentityTests(unittest.TestCase):
    def test_verified_mcp_claims_become_the_shared_request_principal(self):
        expiry = int((datetime.now(timezone.utc) + timedelta(minutes=2)).timestamp())
        token = SimpleNamespace(
            claims={
                "video_service_principal": {
                    "issuer": "https://issuer.example.test/",
                    "subject": "verified-subject",
                    "tenant_id": "verified-tenant",
                    "roles": ["operator"],
                }
            },
            scopes=["video.jobs:read", "video.jobs:cancel"],
            expires_at=expiry,
        )
        result = _principal_from_access_token(token)
        self.assertEqual(result.subject, "verified-subject")
        self.assertEqual(result.tenant_id, "verified-tenant")
        self.assertEqual(result.scopes, frozenset([
            "video.jobs:read", "video.jobs:cancel",
        ]))

    def test_missing_or_untrusted_mcp_identity_fails_closed(self):
        with self.assertRaises(AuthorizationError):
            _principal_from_access_token(None)
        with self.assertRaises(AuthorizationError):
            _principal_from_access_token(SimpleNamespace(
                claims={}, scopes=[], expires_at=9999999999,
            ))


if __name__ == "__main__":
    unittest.main()
