from celery import Task
from video_streaming.core.tasks import BaseTask
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask
from .check import BaseCheckMixin


class CheckOutputKeyMixin(BaseCheckMixin):

    primary_status: BaseStreamingTask.primary_status
    stop_reason: BaseStreamingTask.stop_reason
    error_messages: BaseStreamingTask.error_messages
    s3_service: BaseStreamingTask.s3_service
    save_job_stop_reason: BaseStreamingTask.save_job_stop_reason
    save_primary_status: BaseStreamingTask.save_primary_status

    raise_ignore: BaseTask.raise_ignore
    request: Task.request

    def check_output_key_requirements(
            self,
            request_id=None,
            s3_output_key=None,
            s3_output_bucket=None
            ):
        if request_id is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.REQUEST_ID_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if s3_output_key is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.S3_OUTPUT_KEY_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if s3_output_bucket is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.S3_OUTPUT_BUCKET_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

    def has_upload_risk(self,
                        s3_output_key,
                        s3_output_bucket) -> bool:
        """check if s3_output_key is already exist

        this check is for prevent replace the output
        by send head object request for key of output
        """
        return True if self.s3_service.head(
                key=s3_output_key,
                bucket_name=s3_output_bucket) else False
