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

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        self.save_primary_status(self.primary_status.FAILED)

        # notice : failed reason will only be set if there is no reason
        #  before.

        # set common reason for the task, it's can be connection error
        # after many retries or etc.
        self.save_job_failed_reason(
            self.failed_reason.FAILED_OUTPUT_BUCKET_CHECKING)

        return super().on_failure(exc, task_id, args, kwargs, einfo)


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

    bucket_details = self.get_output_bucket_details()

    # bucket_details is None for 404 or 403 reason
    if not bucket_details:

        # check the task s3_create_bucket boolean param to create a
        #  output bucket when does not exist.
        if not self.s3_create_bucket:

            # ignore the task when s3_create_bucket is False/None and
            #  the output bucket does not exist.

            self.save_primary_status(self.primary_status.FAILED)
            self.save_job_failed_reason(
                self.failed_reason.OUTPUT_BUCKET_ON_S3_IS_404_OR_403)

            raise self.raise_ignore(
                message=self.error_messages.OUTPUT_BUCKET_404_OR_403)

        # try to create output bucket. (BucketAlreadyExist is handled)
        self.create_output_bucket()

    self.incr_passed_checks()
