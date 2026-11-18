"""Vendor-neutral workflow state primitives.

Celery task IDs are execution references only. Job, workflow, stage, attempt and
artifact identities live in the relational store and remain valid when the
execution backend changes.
"""

from enum import Enum


class JobState(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class WorkflowState(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StageState(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    VALIDATED = "VALIDATED"
    PUBLISHING = "PUBLISHING"
    RETRY_WAIT = "RETRY_WAIT"
    FINISHED = "FINISHED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AttemptState(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRYABLE = "RETRYABLE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ALLOWED_STAGE_TRANSITIONS = {
    StageState.PENDING: {StageState.PROCESSING, StageState.CANCELLED},
    StageState.PROCESSING: {
        StageState.VALIDATED, StageState.RETRY_WAIT,
        StageState.FAILED, StageState.CANCELLED,
    },
    StageState.VALIDATED: {
        StageState.PUBLISHING, StageState.RETRY_WAIT,
        StageState.FAILED, StageState.CANCELLED,
    },
    StageState.PUBLISHING: {
        StageState.FINISHED, StageState.RETRY_WAIT,
        StageState.FAILED, StageState.CANCELLED,
    },
    StageState.RETRY_WAIT: {StageState.PROCESSING, StageState.CANCELLED},
    StageState.FINISHED: set(),
    StageState.FAILED: set(),
    StageState.CANCELLED: set(),
}


class InvalidStateTransition(RuntimeError):
    """Raised when a workflow stage attempts a forbidden transition."""


class IdempotencyConflict(RuntimeError):
    """Raised when one idempotency key is reused for a different request."""


class StageAlreadyRunning(RuntimeError):
    """Raised when another execution attempt currently owns a workflow stage."""
