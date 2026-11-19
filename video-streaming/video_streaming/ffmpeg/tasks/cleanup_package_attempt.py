"""Explicit, database-guarded cleanup for one abandoned package Attempt."""

from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from video_streaming.ffmpeg.package_publication import (
    cleanup_abandoned_attempt_package,
    cleanup_abandoned_single_file,
)
from video_streaming.workflows.artifacts import attempt_scoped_object_key
from video_streaming.workflows.service import get_workflow_repository

from .base import BaseStreamingTask


@celery_app.task(
    name="cleanup_package_attempt",
    base=BaseStreamingTask,
    **TASK_DECORATOR_KWARGS
)
def cleanup_package_attempt(
        self,
        attempt_id,
        s3_output_key,
        s3_output_bucket=settings.S3_DEFAULT_OUTPUT_BUCKET_NAME):
    """Clean one Attempt namespace only after PostgreSQL proves it reclaimable.

    This is intentionally an explicit maintenance task. It never lists or
    deletes a shared bucket prefix and returns deleted=False while the Attempt
    is still eligible for retry or has an AVAILABLE Artifact reference.
    """
    repository = get_workflow_repository(required=True)
    attempt_manifest_key = attempt_scoped_object_key(
        s3_output_key, attempt_id
    )
    attempt_prefix = attempt_manifest_key.rsplit("/", 1)[0]
    namespace = repository.get_attempt_publication_namespace(attempt_id)
    if not namespace or namespace.get("prefix") != attempt_prefix:
        raise ValueError("Attempt namespace does not match the supplied key")
    reclaimable = lambda candidate_id, bucket, prefix: (
        repository.is_attempt_reclaimable(
            candidate_id,
            bucket,
            prefix,
            minimum_age_seconds=(
                settings.WORKFLOW_PACKAGE_ORPHAN_RETENTION_SECONDS
            ),
        )
    )
    if namespace.get("kind") == "single_file":
        result = cleanup_abandoned_single_file(
            storage=self.s3_service,
            bucket=s3_output_bucket,
            object_key=namespace["object_key"],
            attempt_id=attempt_id,
            attempt_prefix=attempt_prefix,
            is_reclaimable=reclaimable,
        )
    else:
        result = cleanup_abandoned_attempt_package(
            storage=self.s3_service,
            bucket=s3_output_bucket,
            inventory_key=namespace["inventory_key"],
            attempt_prefix=attempt_prefix,
            is_reclaimable=reclaimable,
        )
    if result["deleted"]:
        repository.mark_package_attempt_cleaned(
            attempt_id, s3_output_bucket, attempt_prefix
        )
    return result


@celery_app.task(
    name="cleanup_abandoned_packages",
    base=BaseStreamingTask,
    **TASK_DECORATOR_KWARGS
)
def cleanup_abandoned_packages(self, limit=100):
    """Periodically reclaim old package namespaces approved by PostgreSQL."""
    repository = get_workflow_repository(required=True)
    candidates = repository.list_reclaimable_package_attempts(
        minimum_age_seconds=(
            settings.WORKFLOW_PACKAGE_ORPHAN_RETENTION_SECONDS
        ),
        limit=limit,
    )
    results = []
    for item in candidates:
        reclaimable = lambda candidate_id, bucket, prefix: (
            repository.is_attempt_reclaimable(
                candidate_id,
                bucket,
                prefix,
                minimum_age_seconds=(
                    settings.WORKFLOW_PACKAGE_ORPHAN_RETENTION_SECONDS
                ),
            )
        )
        if item["kind"] == "single_file":
            result = cleanup_abandoned_single_file(
                storage=self.s3_service,
                bucket=item["bucket"],
                object_key=item["object_key"],
                attempt_id=item["attempt_id"],
                attempt_prefix=item["prefix"],
                is_reclaimable=reclaimable,
            )
        else:
            result = cleanup_abandoned_attempt_package(
                storage=self.s3_service,
                bucket=item["bucket"],
                inventory_key=item["inventory_key"],
                attempt_prefix=item["prefix"],
                is_reclaimable=reclaimable,
            )
        results.append(result)
        if result["deleted"]:
            repository.mark_package_attempt_cleaned(
                item["attempt_id"], item["bucket"], item["prefix"]
            )
    return {
        "candidates": len(candidates),
        "deleted": sum(1 for result in results if result["deleted"]),
        "skipped": sum(1 for result in results if not result["deleted"]),
    }
