"""Celery relay for the transactional workflow dispatch outbox.

Publishing is intentionally at-least-once: a process can crash after the broker
accepts a message but before PostgreSQL records the acknowledgement. Durable
workflow idempotency keys and fenced Attempt claims protect state transitions;
consumers must keep external side effects idempotent.
"""
import logging

from celery import signature

from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.workflows.service import get_workflow_repository


logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="publish_workflow_outbox",
    ignore_result=True,
)
def publish_workflow_outbox(self, limit=None):
    """Publish a bounded batch of due dispatch intents and acknowledge them."""
    repository = get_workflow_repository()
    worker_name = getattr(self.request, "hostname", None) or "worker"
    owner = ("outbox-relay:" + worker_name)[:255]
    batch_size = limit or settings.WORKFLOW_OUTBOX_BATCH_SIZE
    rows = repository.claim_outbox_batch(
        owner=owner,
        lease_ttl_seconds=settings.WORKFLOW_OUTBOX_LEASE_TTL_SECONDS,
        limit=batch_size,
    )

    published = 0
    retried = 0
    for row in rows:
        try:
            if row["topic"] != "celery.canvas":
                raise ValueError("unsupported_outbox_topic")
            # The outbox UUID is also the root Celery task ID. It is a stable
            # correlation key, not a broker-level exactly-once guarantee.
            signature(row["payload"]).apply_async(task_id=row["id"])
        except Exception as exc:
            delay = min(
                settings.WORKFLOW_OUTBOX_MAX_RETRY_DELAY_SECONDS,
                2 ** min(row["attempts"], 8),
            )
            repository.retry_outbox(
                row["id"],
                row["lease_token"],
                type(exc).__name__,
                delay_seconds=delay,
                max_attempts=settings.WORKFLOW_OUTBOX_MAX_ATTEMPTS,
            )
            retried += 1
            logger.warning(
                "Workflow outbox delivery failed",
                extra={
                    "outbox_id": row["id"],
                    "aggregate_id": row["aggregate_id"],
                    "attempt": row["attempts"],
                    "error_type": type(exc).__name__,
                },
            )
            continue

        if repository.mark_outbox_published(row["id"], row["lease_token"]):
            published += 1
        else:
            # The broker may already have accepted the message. Let lease
            # expiry recover the row; a duplicate is possible by design.
            logger.warning(
                "Workflow outbox acknowledgement lost its lease",
                extra={"outbox_id": row["id"]},
            )

    return {"claimed": len(rows), "published": published, "retried": retried}
