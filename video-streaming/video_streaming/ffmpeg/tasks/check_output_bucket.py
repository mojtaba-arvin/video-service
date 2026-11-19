from abc import ABC
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from .base import BaseStreamingTask
from .mixins import CheckOutputBucketMixin


class CheckOutputBucketTask(
        CheckOutputBucketMixin,
        BaseStreamingTask,
        ABC
        ):

    # rewrite BaseCheckMixin.save_failed
    def save_failed(self, request_id):
        super().save_failed(request_id)
        # stop reason will only be set if there is no reason before.
        # set common reason for the task, it's can be connection error
        # after many retries or etc.
        self.save_job_stop_reason(
            self.stop_reason.FAILED_OUTPUT_BUCKET_CHECKING,
            request_id
        )


@celery_app.task(name="check_output_bucket",
                 base=CheckOutputBucketTask,
                 **TASK_DECORATOR_KWARGS)
def check_output_bucket(self,
                        *args,
                        s3_output_bucket: str = settings.S3_DEFAULT_OUTPUT_BUCKET_NAME,
                        s3_create_bucket: bool = settings.CREATE_OUTPUT_BUCKET,
                        request_id: str = None):
    """check output bucket or create if s3_create_bucket is True

       required parameters:
        - request_id
    """
    self.check_output_bucket_requirements(
        request_id=request_id,
        s3_output_bucket=s3_output_bucket
    )

    if self.is_forced_to_stop(request_id):
        raise self.raise_revoke(request_id)

    self.save_primary_status(
        self.primary_status.CHECKING,
        request_id)

    # check output bucket is exist
    # or create if s3_create_bucket is True

    bucket_details = self.get_output_bucket_details(s3_output_bucket)

    # bucket_details is None for 404 or 403 reason
    if not bucket_details:

        # check the task s3_create_bucket boolean param to create a
        #  output bucket when does not exist.
        if not s3_create_bucket:

            # ignore the task when s3_create_bucket is False/None and
            #  the output bucket does not exist.

            self.save_primary_status(
                self.primary_status.FAILED,
                request_id
            )
            self.save_job_stop_reason(
                self.stop_reason.OUTPUT_BUCKET_ON_S3_IS_404_OR_403,
                request_id
            )
            raise self.raise_ignore(
                message=self.error_messages.OUTPUT_BUCKET_404_OR_403,
                request_kwargs=self.request.kwargs)

        # try to create output bucket. (BucketAlreadyExist is handled)
        self.create_output_bucket(s3_output_bucket)

    self.incr_passed_checks(request_id)
