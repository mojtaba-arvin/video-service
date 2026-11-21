"""Executable contract catalogue for the durable VOD operation boundary.

The workflow executor is the sole owner of Stage/Attempt leases. Operation
adapters receive the executor-created NodeExecutionContext and MUST NOT claim
or renew a second lease through legacy Celery task internals.

This catalogue is intentionally separate from Celery task registration: it
describes the required contract, but does not pretend that an unimplemented
adapter is production-ready.
"""
from dataclasses import dataclass
from typing import FrozenSet


@dataclass(frozen=True)
class OperationContract:
    operation: str
    inputs: FrozenSet[str]
    outputs: FrozenSet[str]
    retry_class: str
    cancellation: str
    idempotency: str
    attempt_owner: str = "workflow_executor"
    legacy_task_may_claim_attempt: bool = False
    outputs_must_be_durable_artifacts: bool = True


# Input/output names describe logical artifacts or validated metadata, never
# worker-local paths. Retry classes are interpreted by the persisted node
# retry_policy; adapters classify errors but do not schedule their own retry.
_CONTRACTS = (
    OperationContract("video.input.check", frozenset({"input.bucket", "input.key"}), frozenset({"input.object_ref"}), "transient_io", "before_and_after_io", "read_only"),
    OperationContract("video.input.download", frozenset({"input.object_ref"}), frozenset({"input.file_artifact"}), "transient_io", "before_and_during_transfer", "content_addressed_artifact"),
    OperationContract("video.input.analyze", frozenset({"input.file_artifact"}), frozenset({"input.probe_metadata"}), "transient_io", "before_and_after_probe", "deterministic_probe"),
    OperationContract("video.inputs.funnel", frozenset({"input.probe_metadata"}), frozenset({"inputs.manifest"}), "never", "before_commit", "deterministic_manifest"),
    OperationContract("video.output.bucket.check", frozenset({"output.bucket"}), frozenset({"output.bucket_ready"}), "transient_io", "before_and_after_io", "idempotent_bucket_upsert"),
    OperationContract("video.output.key.check", frozenset({"output.bucket", "output.key"}), frozenset({"output.key_available"}), "transient_io", "before_and_after_io", "read_only"),
    OperationContract("video.hls.package", frozenset({"inputs.manifest"}), frozenset({"package.hls_manifest", "package.directory_manifest"}), "transient_media", "process_group_termination", "attempt_scoped_package"),
    OperationContract("video.dash.package", frozenset({"inputs.manifest"}), frozenset({"package.dash_manifest", "package.directory_manifest"}), "transient_media", "process_group_termination", "attempt_scoped_package"),
    OperationContract("video.thumbnail.generate", frozenset({"inputs.manifest"}), frozenset({"thumbnail.image"}), "transient_media", "process_group_termination", "attempt_scoped_output"),
    OperationContract("video.watermark.apply", frozenset({"inputs.manifest", "watermark.image"}), frozenset({"watermark.video"}), "transient_media", "process_group_termination", "attempt_scoped_output"),
    OperationContract("video.output.upload_file", frozenset({"output.file_artifact"}), frozenset({"output.published_object"}), "transient_io", "multipart_abort_or_reconcile", "attempt_scoped_publish"),
    OperationContract("video.output.upload_directory", frozenset({"package.directory_manifest"}), frozenset({"output.published_manifest"}), "transient_io", "multipart_abort_or_reconcile", "attempt_scoped_publish"),
    OperationContract("video.webhook.deliver", frozenset({"webhook.secret_ref", "job.completion_event"}), frozenset({"webhook.delivery_receipt"}), "transient_remote", "between_requests", "stable_delivery_id"),
)

OPERATION_CONTRACTS = {contract.operation: contract for contract in _CONTRACTS}

if len(OPERATION_CONTRACTS) != len(_CONTRACTS):
    raise RuntimeError("duplicate durable VOD operation contract")
