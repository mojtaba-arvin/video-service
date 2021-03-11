from abc import ABC

from celery import states

from video_streaming.celery import celery_app
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from .base import BaseStreamingTask
from .mixins import BaseCheckMixin, CheckInputMixin


class CheckInputKeyTask(
        CheckInputMixin,
        BaseCheckMixin,
        BaseStreamingTask,
        ABC
        ):

    def save_failed(self):
        self.save_primary_status(self.primary_status.FAILED)
        # stop reason will only be set if there is no reason before.
        # set common reason for the task, it's can be connection error
        # after many retries or etc.
        self.save_job_stop_reason(
            self.stop_reason.FAILED_INPUT_KEY_CHECKING)

    def on_failure(self, *args, **kwargs):
        self.save_failed()
        return super().on_failure(*args, **kwargs)

    def raise_ignore(self, message=None, state=states.FAILURE):
        if state == states.FAILURE:
            self.save_failed()
        super().raise_ignore(message=message, state=state)


@celery_app.task(name="check_input_key",
                 base=CheckInputKeyTask,
                 **TASK_DECORATOR_KWARGS)
def check_input_key(self,
                    *args,
                    s3_input_key: str = None,
                    s3_input_bucket: str = None,
                    request_id: str = None) -> dict:
    """check s3_input_key is exist on s3_input_bucket

       required parameters:
         - s3_input_key
         - s3_input_bucket
    """

    self._initial_params()
    self.save_primary_status(self.primary_status.CHECKING)

    # get object details by s3_input_key on s3_input_bucket
    object_details = self.get_object_details()

    # object_details is None for 404 or 403 reason
    if not object_details:
        self.save_primary_status(self.primary_status.FAILED)
        self.save_job_stop_reason(
            self.stop_reason.INPUT_VIDEO_ON_S3_IS_404_OR_403)
        raise self.raise_ignore(
            message=self.error_messages.INPUT_VIDEO_404_OR_403)

    self.incr_passed_checks()

    return dict(object_details=object_details)
