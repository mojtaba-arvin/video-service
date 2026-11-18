"""Application-level access to the configured workflow repository."""

from functools import lru_cache

from video_streaming import settings

from .repository import WorkflowRepository


@lru_cache(maxsize=2)
def get_workflow_repository(required=True):
    if not settings.WORKFLOW_DATABASE_URL:
        if required:
            raise RuntimeError(
                "WORKFLOW_DATABASE_URL is required for durable job state"
            )
        return None
    return WorkflowRepository(
        settings.WORKFLOW_DATABASE_URL,
        pool_size=settings.WORKFLOW_DATABASE_POOL_SIZE,
        max_overflow=settings.WORKFLOW_DATABASE_MAX_OVERFLOW,
    )
