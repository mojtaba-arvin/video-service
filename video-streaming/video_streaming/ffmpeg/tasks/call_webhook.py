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
                 request_id: str = None
                 ):
    """notify the client that the outputs are ready
       required parameters:
         - request_id
    """

    self._initial_params()

    is_delivered = self.call_webhook()

    return dict(is_delivered=is_delivered)
