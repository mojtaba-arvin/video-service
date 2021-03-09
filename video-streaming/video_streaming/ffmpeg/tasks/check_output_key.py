from abc import ABC
from video_streaming.celery import celery_app
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from .base import BaseStreamingTask
from .mixins import BaseCheckMixin, CheckOutputKeyMixin


class CheckOutputKeyTask(
        CheckOutputKeyMixin,
        BaseCheckMixin,
        BaseStreamingTask,
        ABC
        ):
    pass


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

    # check if s3_output_key is already exist
    # and raise if s3_dont_replace is True
    self.check_output_key()

    self.incr_passed_checks()

