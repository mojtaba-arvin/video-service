"""Provider-neutral workload classification for scheduling and cost attribution.

Classification is deliberately based on the typed operation contract, not on
untrusted request labels or assumptions about a particular worker provider.
It is descriptive metadata; it does not itself select or provision a worker.
"""

from enum import Enum

from .operations import OperationKind


class WorkloadClass(str, Enum):
    """Stable workload families used by admission and scheduling policies."""

    METADATA = "metadata"
    MEDIA_TRANSFORM = "media_transform"
    PACKAGING = "packaging"
    STORAGE_IO = "storage_io"


_OPERATION_CLASSES = {
    # Typed application-operation contract.
    OperationKind.PROBE.value: WorkloadClass.METADATA,
    OperationKind.VALIDATE_PACKAGE.value: WorkloadClass.METADATA,
    OperationKind.PUBLISH_PACKAGE.value: WorkloadClass.STORAGE_IO,
    OperationKind.HLS_PACKAGE_AND_PUBLISH.value: WorkloadClass.PACKAGING,
    OperationKind.DASH_PACKAGE_AND_PUBLISH.value: WorkloadClass.PACKAGING,
    OperationKind.THUMBNAIL_AND_PUBLISH.value: WorkloadClass.MEDIA_TRANSFORM,
    OperationKind.WATERMARK_AND_PUBLISH.value: WorkloadClass.MEDIA_TRANSFORM,
    OperationKind.TRIM.value: WorkloadClass.MEDIA_TRANSFORM,
    OperationKind.TRANSCODE.value: WorkloadClass.MEDIA_TRANSFORM,

    # Durable 13-operation VOD execution-plan contract.
    "video.input.check": WorkloadClass.METADATA,
    "video.input.download": WorkloadClass.STORAGE_IO,
    "video.input.analyze": WorkloadClass.METADATA,
    "video.inputs.funnel": WorkloadClass.METADATA,
    "video.output.bucket.check": WorkloadClass.METADATA,
    "video.output.key.check": WorkloadClass.METADATA,
    "video.hls.package": WorkloadClass.PACKAGING,
    "video.dash.package": WorkloadClass.PACKAGING,
    "video.thumbnail.generate": WorkloadClass.MEDIA_TRANSFORM,
    "video.watermark.apply": WorkloadClass.MEDIA_TRANSFORM,
    "video.output.upload_file": WorkloadClass.STORAGE_IO,
    "video.output.upload_directory": WorkloadClass.STORAGE_IO,
    "video.webhook.deliver": WorkloadClass.STORAGE_IO,
}


def classify_operation(operation):
    """Return the explicit workload family for a typed operation.

    Strings are accepted only when they exactly match a known OperationKind.
    Unknown operations fail closed so new operations cannot silently inherit
    an inappropriate scheduling policy.
    """
    operation_value = (
        operation.value if isinstance(operation, OperationKind) else operation
    )
    if not isinstance(operation_value, str):
        raise ValueError("unsupported workflow operation: {!r}".format(
            operation
        ))

    try:
        return _OPERATION_CLASSES[operation_value]
    except KeyError:
        raise ValueError("operation has no workload classification: {}".format(
            operation.value
        ))
