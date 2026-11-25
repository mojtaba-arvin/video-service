"""Declarative contracts for the durable VOD operation boundary.

The catalogue distinguishes node parameters from dependency Artifact inputs.
required_artifacts and optional_artifacts are conjunctive; exactly one member
of one_of_artifacts is selected by the node parameters. Names may contain
placeholders such as {input_name}, {bucket}, or {node_id}.

This is descriptive metadata, not a substitute for adapter-level validation.
The WorkflowNodeExecutor remains the sole owner of Stage/Attempt leases.
"""
from dataclasses import dataclass
from typing import FrozenSet


@dataclass(frozen=True)
class OperationContract:
    operation: str
    required_parameters: FrozenSet[str]
    optional_parameters: FrozenSet[str]
    required_artifacts: FrozenSet[str]
    optional_artifacts: FrozenSet[str]
    one_of_artifacts: FrozenSet[str]
    outputs: FrozenSet[str]
    retry_class: str
    cancellation: str
    idempotency: str
    attempt_owner: str = "workflow_executor"
    legacy_task_may_claim_attempt: bool = False
    outputs_must_be_durable_artifacts: bool = True

    @property
    def inputs(self):
        """Compatibility view of all accepted dependency Artifact names."""
        return (
            self.required_artifacts
            | self.optional_artifacts
            | self.one_of_artifacts
        )


_CONTRACTS = (
    OperationContract(
        "video.input.check",
        frozenset({"input_name", "bucket", "key"}),
        frozenset({"input_number"}),
        frozenset(), frozenset(), frozenset(),
        frozenset({"input.{input_name}.object_ref"}),
        "transient_io", "before_and_after_io", "read_only",
    ),
    OperationContract(
        "video.input.download",
        frozenset({"input_name", "bucket", "key"}),
        frozenset({"input_number"}),
        frozenset({"input.{input_name}.object_ref"}),
        frozenset(), frozenset(),
        frozenset({"input.{input_name}.file_artifact"}),
        "transient_io", "before_and_during_transfer",
        "content_addressed_artifact",
    ),
    OperationContract(
        "video.input.analyze",
        frozenset({"input_name"}),
        frozenset({"input_number"}),
        frozenset({"input.{input_name}.file_artifact"}),
        frozenset(), frozenset(),
        frozenset({
            "input.{input_name}.file_artifact",
            "input.{input_name}.probe_metadata",
        }),
        "transient_io", "before_and_after_probe", "deterministic_probe",
    ),
    OperationContract(
        "video.inputs.funnel",
        frozenset(), frozenset(),
        frozenset({
            "input.video.file_artifact",
            "input.video.probe_metadata",
        }),
        frozenset({
            "input.watermark.file_artifact",
            "input.watermark.probe_metadata",
        }),
        frozenset(),
        frozenset({"inputs.manifest"}),
        "never", "before_commit", "deterministic_manifest",
    ),
    OperationContract(
        "video.output.bucket.check",
        frozenset({"bucket"}),
        frozenset({"create_bucket"}),
        frozenset(), frozenset(), frozenset(),
        frozenset({"output.bucket_ready.{bucket}"}),
        "transient_io", "before_and_after_io", "idempotent_bucket_upsert",
    ),
    OperationContract(
        "video.output.key.check",
        frozenset({"bucket", "key"}),
        frozenset({"dont_replace"}),
        frozenset(), frozenset(), frozenset(),
        frozenset({"output.key_available.{node_id}"}),
        "transient_io", "before_and_after_io", "read_only",
    ),
    OperationContract(
        "video.hls.package",
        frozenset({"protocol", "key"}),
        frozenset({
            "encode_format", "video_codec", "audio_codec", "fragmented",
            "quality_names", "custom_qualities", "use_watermark", "bucket",
        }),
        frozenset(), frozenset(),
        frozenset({"inputs.manifest", "watermark.video"}),
        frozenset({"package.hls_manifest", "package.directory_manifest"}),
        "transient_media", "process_group_termination", "attempt_scoped_package",
    ),
    OperationContract(
        "video.dash.package",
        frozenset({"protocol", "key"}),
        frozenset({
            "encode_format", "video_codec", "audio_codec", "fragmented",
            "quality_names", "custom_qualities", "use_watermark", "bucket",
        }),
        frozenset(), frozenset(),
        frozenset({"inputs.manifest", "watermark.video"}),
        frozenset({"package.dash_manifest", "package.directory_manifest"}),
        "transient_media", "process_group_termination", "attempt_scoped_package",
    ),
    OperationContract(
        "video.thumbnail.generate",
        frozenset({"thumbnail_time"}),
        frozenset({"scale_width", "scale_height", "use_watermark", "bucket", "key"}),
        frozenset(), frozenset(),
        frozenset({"inputs.manifest", "watermark.video"}),
        frozenset({"thumbnail.image"}),
        "transient_media", "process_group_termination", "attempt_scoped_output",
    ),
    OperationContract(
        "video.watermark.apply",
        frozenset(), frozenset({
            "watermark_input_number", "watermark_output_key",
            "watermark_output_bucket",
        }),
        frozenset({"inputs.manifest"}),
        frozenset(), frozenset(),
        frozenset({"watermark.video"}),
        "transient_media", "process_group_termination", "attempt_scoped_output",
    ),
    OperationContract(
        "video.output.upload_file",
        frozenset({"bucket", "key"}),
        frozenset({"dont_replace", "output_id"}),
        frozenset(), frozenset(),
        frozenset({"thumbnail.image", "watermark.video"}),
        frozenset({"output.published_object"}),
        "transient_io", "multipart_abort_or_reconcile", "attempt_scoped_publish",
    ),
    OperationContract(
        "video.output.upload_directory",
        frozenset({"bucket", "key", "protocol"}),
        frozenset({"dont_replace", "output_id"}),
        frozenset({"package.directory_manifest"}),
        frozenset(), frozenset(),
        frozenset({"output.published_manifest"}),
        "transient_io", "multipart_abort_or_reconcile", "attempt_scoped_publish",
    ),
    OperationContract(
        "video.webhook.deliver",
        frozenset({"enabled"}),
        frozenset({"webhook_secret_ref", "webhook_signing_secret_ref"}),
        frozenset(), frozenset(), frozenset(),
        frozenset({"webhook.delivery_receipt"}),
        "transient_remote", "between_requests", "stable_delivery_id",
    ),
)

OPERATION_CONTRACTS = {
    contract.operation: contract for contract in _CONTRACTS
}

if len(OPERATION_CONTRACTS) != len(_CONTRACTS):
    raise RuntimeError("duplicate durable VOD operation contract")
