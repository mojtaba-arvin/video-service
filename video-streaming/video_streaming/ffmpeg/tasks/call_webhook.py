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


@celery_app.task(name="call_webhook",
                 base=CallWebhookTask,
                 **TASK_DECORATOR_KWARGS)
def call_webhook(self,
                 *args,
                 request_id: str = None,
                 webhook_url: str = None
                 ):
    """notify the client that the outputs are ready
       required parameters:
         - request_id
         - webhook_url
    """
    if request_id is None:
        # TODO notify developer
        raise self.raise_ignore(
            message=self.error_messages.REQUEST_ID_IS_REQUIRED,
            request_kwargs=self.request.kwargs)

    if not webhook_url:
        # TODO notify developer
        raise self.raise_ignore(
            message=self.error_messages.WEBHOOK_URL_IS_REQUIRED,
            request_kwargs=self.request.kwargs)

    if self.is_forced_to_stop(request_id):
        raise self.raise_revoke(request_id)

    is_delivered = self.call_webhook(
        request_id,
        webhook_url=webhook_url)

    return dict(is_delivered=is_delivered)
