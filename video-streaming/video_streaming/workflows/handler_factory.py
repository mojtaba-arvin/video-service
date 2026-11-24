"""Production registry factory for every durable VOD operation."""
from video_streaming import settings
from video_streaming.workflows.execution_plan import PLAN_OPERATIONS
from video_streaming.workflows.input_handlers import build_input_handlers
from video_streaming.workflows.media_handlers import build_media_handlers
from video_streaming.workflows.output_preflight_handlers import (
    build_output_preflight_handlers,
)
from video_streaming.workflows.publication_handlers import (
    build_publication_handlers,
)


def build_workflow_handlers(repository, artifact_store):
    """Compose the complete 13-operation allow-listed handler registry.

    This factory is intentionally side-effect-free at import time. Runtime
    clients are injected by the durable task consumer; KMS resolution remains
    lazy and fail-closed until a webhook-enabled node actually executes.
    """
    if repository is None or artifact_store is None:
        raise ValueError("repository and Artifact Store are required")
    s3_client = artifact_store.client
    handlers = {}
    for builder in (
        build_input_handlers(s3_client, artifact_store),
        build_output_preflight_handlers(
            s3_client, artifact_store, region_name=settings.S3_REGION_NAME
        ),
        build_media_handlers(artifact_store),
        build_publication_handlers(
            s3_client, artifact_store, repository
        ),
    ):
        overlap = set(handlers).intersection(builder)
        if overlap:
            raise RuntimeError(
                "duplicate durable workflow handlers: {}".format(
                    ", ".join(sorted(overlap))
                )
            )
        handlers.update(builder)
    if set(handlers) != set(PLAN_OPERATIONS):
        missing = sorted(set(PLAN_OPERATIONS) - set(handlers))
        extra = sorted(set(handlers) - set(PLAN_OPERATIONS))
        raise RuntimeError(
            "durable VOD registry is incomplete; missing={}, extra={}".format(
                missing, extra
            )
        )
    return handlers
