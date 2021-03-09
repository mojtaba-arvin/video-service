from abc import ABC
from video_streaming.celery import celery_app
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from .base import BaseStreamingTask
from .mixins import BaseCheckMixin, CheckOutputBucketMixin


class CheckOutputBucketTask(
        CheckOutputBucketMixin,
        BaseCheckMixin,
        BaseStreamingTask,
        ABC
        ):
    pass


@celery_app.task(name="check_output_bucket",
                 base=CheckOutputBucketTask,
                 **TASK_DECORATOR_KWARGS)
def check_output_bucket(self,
                        *args,
                        s3_output_bucket: str = None,
                        s3_create_bucket: bool = None,
                        request_id: str = None):
    """check output bucket or create if s3_create_bucket is True

       required parameters:
         - s3_output_bucket
    """

    self._initial_params()

    self.save_primary_status(self.primary_status.CHECKING)

    # check output bucket is exist
    # or create if s3_create_bucket is True
    self.ensure_bucket_exist()

    self.incr_passed_checks()
