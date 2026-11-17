"""Application-level access to the configured workflow repository."""

from functools import lru_cache

from video_streaming import settings

from .repository import WorkflowRepository


@lru_cache(maxsize=1)
def get_workflow_repository():
    if not settings.WORKFLOW_DATABASE_URL:
        raise RuntimeError(
            "WORKFLOW_DATABASE_URL is required for durable job state"
        )
    return WorkflowRepository(
        settings.WORKFLOW_DATABASE_URL,
        pool_size=settings.WORKFLOW_DATABASE_POOL_SIZE,
    )
