from abc import ABC

from video_streaming.celery import celery_app
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from .base import BaseStreamingTask
from .mixins import CallWebhookMixin


class CallWebhookTask(
        CallWebhookMixin,
        BaseStreamingTask, ABC
        ):
    pass


@celery_app.task(
    name="call_webhook",
    base=CallWebhookTask,
    **TASK_DECORATOR_KWARGS
)
def call_webhook(
        self, *args, request_id: str = None, delivery_stage_key: str = None):
    """Deliver a callback after resolving its encrypted Job-scoped secret.

    Raw callback URLs are never read from Redis or accepted as task arguments.
    """
    if request_id is None:
        raise self.raise_ignore(
            message=self.error_messages.REQUEST_ID_IS_REQUIRED,
            request_kwargs=self.request.kwargs,
        )

    if self.is_forced_to_stop(request_id):
        raise self.raise_revoke(request_id)

    try:
        from video_streaming.workflows.service import get_workflow_repository
        from video_streaming.workflows.secret_runtime import (
            resolve_job_webhook_secret,
        )
        repository = get_workflow_repository()
        persisted_plan = repository.get_execution_plan(request_id)
        if persisted_plan is None:
            raise RuntimeError("persisted callback plan is unavailable")
        secret_refs = {
            node["parameters"].get("webhook_secret_ref")
            for node in persisted_plan["plan"]["nodes"]
            if node["operation"] == "video.webhook.deliver"
            and node["parameters"].get("enabled") is True
        }
        secret_refs.discard(None)
        if len(secret_refs) != 1:
            raise RuntimeError("persisted callback secret reference is missing or ambiguous")
        webhook_url = resolve_job_webhook_secret(
            request_id, secret_refs.pop(), repository=repository
        )
        signing_refs = {
            node["parameters"].get("webhook_signing_secret_ref")
            for node in persisted_plan["plan"]["nodes"]
            if node["operation"] == "video.webhook.deliver"
            and node["parameters"].get("enabled") is True
        }
        signing_refs.discard(None)
        if len(signing_refs) > 1:
            raise RuntimeError("persisted webhook signing secret reference is ambiguous")
        signing_secret = None
        if signing_refs:
            from video_streaming.workflows.secret_runtime import (
                resolve_job_webhook_signing_secret,
            )
            signing_secret = resolve_job_webhook_signing_secret(
                request_id, signing_refs.pop(), repository=repository
            )
        if signing_secret is not None and not delivery_stage_key:
            raise RuntimeError("signed callback requires a stable delivery stage key")
    except Exception as exc:
        # Never log exception strings: KMS/HTTP libraries may include secrets.
        self.logger.error(
            "Unable to resolve encrypted callback destination",
            extra={"request_id": request_id, "error_type": type(exc).__name__},
        )
        raise self.raise_ignore(
            message=self.error_messages.WEBHOOK_URL_IS_REQUIRED,
            request_kwargs=self.request.kwargs,
        )

    is_delivered = self.call_webhook(
        request_id,
        webhook_url=webhook_url,
        signing_secret=signing_secret,
        delivery_stage_key=delivery_stage_key,
    )
    # Drop the only application references immediately after the HTTP call.
    webhook_url = None
    signing_secret = None
    return dict(is_delivered=is_delivered)
