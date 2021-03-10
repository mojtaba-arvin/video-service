from abc import ABC
from video_streaming.celery import celery_app
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from .base import BaseStreamingTask
from .mixins import BaseCheckMixin, CheckOutputKeyMixin


class CheckOutputKeyTask(
        ChainCallbackMixin,
        CheckOutputKeyMixin,
        BaseCheckMixin,
        BaseStreamingTask,
        ABC
        ):

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        self.save_primary_status(self.primary_status.FAILED)

        # notice : failed reason will only be set if there is no reason
        #  before.

        # set common reason for the task, it's can be connection error
        # after many retries or etc.
        self.save_job_failed_reason(
            self.failed_reason.FAILED_OUTPUT_KEY_CHECKING)
        return super().on_failure(exc, task_id, args, kwargs, einfo)


@celery_app.task(name="check_output_key",
                 base=CheckOutputKeyTask,
                 **TASK_DECORATOR_KWARGS)
def check_output_key(self,
                     *args,
                     s3_output_key: str = None,
                     s3_output_bucket: str = None,
                     s3_dont_replace: bool = None,
                     request_id: str = None):
    """check if s3_output_key is already exist

       required parameters:
         - s3_output_key
         - s3_output_bucket
    """

    self._initial_params()
    self.save_primary_status(self.primary_status.CHECKING)

    # when s3_dont_replace param is True ,
    # check if s3_output_key is already exist and ignore the task
    if self.s3_dont_replace and self.has_upload_risk():
        self.save_primary_status(self.primary_status.FAILED)
        self.save_job_failed_reason(
            self.failed_reason.HAS_UPLOAD_RISK)
        raise self.raise_ignore(
            message=self.error_messages.OUTPUT_KEY_IS_ALREADY_EXIST)

    self.incr_passed_checks()

