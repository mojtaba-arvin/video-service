from abc import ABC
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from .base import BaseStreamingTask
from .mixins import CheckOutputKeyMixin


class CheckOutputKeyTask(
        ChainCallbackMixin,
        CheckOutputKeyMixin,
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
            self.stop_reason.FAILED_OUTPUT_KEY_CHECKING,
            request_id
        )


@celery_app.task(name="check_output_key",
                 base=CheckOutputKeyTask,
                 **TASK_DECORATOR_KWARGS)
def check_output_key(self,
                     *args,
                     s3_output_key: str = None,
                     s3_output_bucket: str = settings.S3_DEFAULT_OUTPUT_BUCKET_NAME,
                     s3_dont_replace: bool = settings.DONT_REPLACE_OUTPUT,
                     request_id: str = None):
    """check if s3_output_key is already exist

       Kwargs:
          s3_output_key:
            The S3 key to save m3u8 or mpd file.
            e.g. "/foo/bar/example.m3u8"

       required parameters:
         - request_id
         - s3_output_key
         - s3_output_bucket
    """
    self.check_output_key_requirements(
        request_id=request_id,
        s3_output_key=s3_output_key,
        s3_output_bucket=s3_output_bucket
    )

    if self.is_forced_to_stop(request_id):
        raise self.raise_revoke(request_id)

    self.save_primary_status(
        self.primary_status.CHECKING,
        request_id)

    # when s3_dont_replace param is True ,
    # check if s3_output_key is already exist and ignore the task
    if s3_dont_replace and self.has_upload_risk(
            s3_output_key,
            s3_output_bucket
            ):
        self.save_primary_status(
            self.primary_status.FAILED,
            request_id
        )
        self.save_job_stop_reason(
            self.stop_reason.HAS_UPLOAD_RISK,
            request_id
        )
        raise self.raise_ignore(
            message=self.error_messages.OUTPUT_KEY_IS_ALREADY_EXIST,
            request_kwargs=self.request.kwargs)

    self.incr_passed_checks(request_id)

