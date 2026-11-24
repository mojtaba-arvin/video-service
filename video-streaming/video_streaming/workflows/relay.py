"""Leased transactional-outbox relay for immutable workflow plans.

The relay deliberately publishes only a small, versioned reference. It never
serializes user input, secret material, task paths, or the execution plan into
broker messages. Delivery is at-least-once: a crash after broker acceptance and
before the database acknowledgement can publish the same dispatch again.
"""

import logging
import re

from video_streaming.workflows.execution_plan import execution_plan_sha256


logger = logging.getLogger(__name__)

DISPATCH_TASK_NAME = "video_streaming.workflows.dispatch_plan"
DISPATCH_EVENT_TYPE = "WORKFLOW_PLAN_DISPATCH"


class WorkflowOutboxRelay:
    """Publish durable plan references through a Celery-compatible producer."""

    def __init__(
            self,
            repository,
            publisher,
            owner,
            queue=None,
            lease_ttl_seconds=30,
            batch_size=25,
            max_attempts=20,
            retry_base_seconds=2,
            retry_max_seconds=300):
        if repository is None or publisher is None:
            raise ValueError("repository and publisher are required")
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 255:
            raise ValueError("owner must contain 1 to 255 characters")
        if retry_base_seconds < 1 or retry_max_seconds < retry_base_seconds:
            raise ValueError("invalid retry delay bounds")
        self.repository = repository
        self.publisher = publisher
        self.owner = owner.strip()
        self.queue = queue
        self.lease_ttl_seconds = lease_ttl_seconds
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds

    @staticmethod
    def _safe_error_code(exc):
        # Do not persist exception strings: broker errors can contain URLs,
        # credentials, or provider response bodies.
        name = type(exc).__name__
        return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:96] or "dispatch_error"

    def _retry_delay(self, attempts):
        return min(
            self.retry_max_seconds,
            self.retry_base_seconds * (2 ** max(0, attempts - 1)),
        )

    def run_once(self):
        """Claim and publish one bounded batch; return aggregate outcomes."""
        claimed = self.repository.claim_outbox_batch(
            owner=self.owner,
            lease_ttl_seconds=self.lease_ttl_seconds,
            limit=self.batch_size,
            max_attempts=self.max_attempts,
        )
        result = {
            "claimed": len(claimed),
            "published": 0,
            "retried": 0,
            "dead": 0,
            "lease_lost": 0,
        }
        for item in claimed:
            outbox_id = item["id"]
            token = item["lease_token"]
            try:
                if item["event_type"] != DISPATCH_EVENT_TYPE:
                    raise ValueError("unsupported_outbox_event")
                snapshot = self.repository.get_execution_plan(item["job_id"])
                if snapshot is None:
                    raise ValueError("execution_plan_missing")
                plan = snapshot["plan"]
                if (
                        snapshot["workflow_id"] != item["workflow_id"]
                        or snapshot["workflow_version"] != item["plan_version"]
                        or snapshot["fingerprint"] != item["plan_fingerprint"]
                        or item["payload"].get("job_id") != item["job_id"]
                        or item["payload"].get("plan_fingerprint")
                        != item["plan_fingerprint"]
                        or execution_plan_sha256(plan) != item["plan_fingerprint"]):
                    raise ValueError("execution_plan_identity_mismatch")

                kwargs = {
                    "args": [
                        item["job_id"],
                        item["workflow_id"],
                        item["plan_version"],
                        item["plan_fingerprint"],
                    ],
                    # A deterministic task ID helps operators correlate repeat
                    # publishes; it is not an exactly-once or deduplication gate.
                    "task_id": "workflow-plan-{}".format(outbox_id),
                }
                if self.queue:
                    kwargs["queue"] = self.queue
                self.publisher.send_task(DISPATCH_TASK_NAME, **kwargs)
                if self.repository.mark_outbox_published(outbox_id, token):
                    result["published"] += 1
                    logger.info(
                        "workflow_outbox_published",
                        extra={
                            "outbox_id": outbox_id,
                            "job_id": item["job_id"],
                            "workflow_id": item["workflow_id"],
                            "attempt": item["attempts"],
                        },
                    )
                else:
                    result["lease_lost"] += 1
            except Exception as exc:
                error_code = self._safe_error_code(exc)
                retry_scheduled = self.repository.retry_outbox(
                    outbox_id,
                    token,
                    error_code=error_code,
                    delay_seconds=self._retry_delay(item["attempts"]),
                    max_attempts=self.max_attempts,
                )
                if retry_scheduled:
                    result["retried"] += 1
                else:
                    result["dead"] += 1
                logger.warning(
                    "workflow_outbox_publish_failed",
                    extra={
                        "outbox_id": outbox_id,
                        "job_id": item["job_id"],
                        "attempt": item["attempts"],
                        "error_code": error_code,
                        "retry_scheduled": retry_scheduled,
                    },
                )
        return result
