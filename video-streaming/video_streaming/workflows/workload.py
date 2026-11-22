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
    OperationKind.PROBE: WorkloadClass.METADATA,
    OperationKind.VALIDATE_PACKAGE: WorkloadClass.METADATA,
    OperationKind.PUBLISH_PACKAGE: WorkloadClass.STORAGE_IO,
    OperationKind.HLS_PACKAGE_AND_PUBLISH: WorkloadClass.PACKAGING,
    OperationKind.DASH_PACKAGE_AND_PUBLISH: WorkloadClass.PACKAGING,
    OperationKind.THUMBNAIL_AND_PUBLISH: WorkloadClass.MEDIA_TRANSFORM,
    OperationKind.WATERMARK_AND_PUBLISH: WorkloadClass.MEDIA_TRANSFORM,
    OperationKind.TRIM: WorkloadClass.MEDIA_TRANSFORM,
    OperationKind.TRANSCODE: WorkloadClass.MEDIA_TRANSFORM,
}


def classify_operation(operation):
    """Return the explicit workload family for a typed operation.

    Strings are accepted only when they exactly match a known OperationKind.
    Unknown operations fail closed so new operations cannot silently inherit
    an inappropriate scheduling policy.
    """
    if not isinstance(operation, OperationKind):
        try:
            operation = OperationKind(operation)
        except (TypeError, ValueError):
            raise ValueError("unsupported workflow operation: {!r}".format(
                operation
            ))

    try:
        return _OPERATION_CLASSES[operation]
    except KeyError:
        raise ValueError("operation has no workload classification: {}".format(
            operation.value
        ))
