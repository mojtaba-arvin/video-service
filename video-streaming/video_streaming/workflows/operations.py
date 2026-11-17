"""Typed internal operation contracts shared by API, workers and future MCP adapters.

The operation layer has no Celery imports: executors are replaceable adapters.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class OperationKind(str, Enum):
    PROBE = "video.probe"
    VALIDATE_PACKAGE = "video.package.validate"
    PUBLISH_PACKAGE = "video.package.publish"
    HLS_PACKAGE_AND_PUBLISH = "video.hls.package_and_publish"
    DASH_PACKAGE_AND_PUBLISH = "video.dash.package_and_publish"
    THUMBNAIL_AND_PUBLISH = "video.thumbnail_and_publish"
    WATERMARK_AND_PUBLISH = "video.watermark_and_publish"
    TRIM = "video.trim"
    TRANSCODE = "video.transcode"


@dataclass(frozen=True)
class OperationArtifact:
    artifact_type: str
    logical_name: str
    uri: str
    media_type: Optional[str] = None
    size_bytes: Optional[int] = None
    checksum_sha256: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OperationRequest:
    job_id: str
    stage_key: str
    operation: OperationKind
    parameters: Dict[str, Any] = field(default_factory=dict)
    input_artifacts: List[str] = field(default_factory=list)
    idempotency_key: Optional[str] = None

    def __post_init__(self):
        if not self.job_id or not self.stage_key:
            raise ValueError("job_id and stage_key are required")
        if not isinstance(self.operation, OperationKind):
            raise TypeError("operation must be an OperationKind")


@dataclass(frozen=True)
class OperationError:
    code: str
    message: str
    location: Optional[str] = None
    retryable: bool = False


@dataclass(frozen=True)
class OperationResult:
    job_id: str
    stage_key: str
    status: str
    artifacts: List[OperationArtifact] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    errors: List[OperationError] = field(default_factory=list)
