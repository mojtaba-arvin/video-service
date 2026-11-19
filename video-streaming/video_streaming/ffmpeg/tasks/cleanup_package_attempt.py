"""Explicit, database-guarded cleanup for one abandoned package Attempt."""

import os

from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from video_streaming.ffmpeg.package_publication import (
    cleanup_abandoned_attempt_package,
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
    inventory_key = "{}/.{}.artifacts.json".format(
        attempt_prefix, os.path.basename(s3_output_key)
    )
    return cleanup_abandoned_attempt_package(
        storage=self.s3_service,
        bucket=s3_output_bucket,
        inventory_key=inventory_key,
        attempt_prefix=attempt_prefix,
        is_reclaimable=lambda candidate_id, bucket, prefix: (
            repository.is_attempt_reclaimable(
                candidate_id,
                bucket,
                prefix,
                minimum_age_seconds=(
                    settings.WORKFLOW_PACKAGE_ORPHAN_RETENTION_SECONDS
                ),
            )
        ),
    )
