"""Typed failure classification shared by durable VOD operation handlers."""


class WorkflowOperationError(RuntimeError):
    """Base handler failure with an explicit retryability decision."""

    retryable = False


class RetryableOperationError(WorkflowOperationError):
    """A transient dependency/provider failure safe to retry idempotently."""

    retryable = True


class PermanentOperationError(WorkflowOperationError):
    """A deterministic input/policy/validation failure that must not retry."""

    retryable = False
