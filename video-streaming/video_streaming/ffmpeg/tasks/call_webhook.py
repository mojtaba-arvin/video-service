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
def call_webhook(self, *args, request_id: str = None):
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
        from video_streaming.workflows.secret_runtime import (
            resolve_job_webhook_secret,
        )
        webhook_url = resolve_job_webhook_secret(request_id)
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

    is_delivered = self.call_webhook(request_id, webhook_url=webhook_url)
    # Drop the only application reference immediately after the HTTP call.
    webhook_url = None
    return dict(is_delivered=is_delivered)
