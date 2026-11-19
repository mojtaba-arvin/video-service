from celery import Task
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask
from .output import BaseOutputMixin


class UploadFileMixin(BaseOutputMixin):

    stop_reason: BaseStreamingTask.stop_reason
    error_messages: BaseStreamingTask.error_messages
    s3_service: BaseStreamingTask.s3_service
    save_job_stop_reason: BaseStreamingTask.save_job_stop_reason

    request: Task.request

    def check_upload_file_requirements(
            self,
            request_id=None,
            output_id=None,
            file_path=None,
            s3_output_key=None,
            s3_output_bucket=None):

        if request_id is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.REQUEST_ID_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if output_id is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.OUTPUT_NUMBER_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if file_path is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.DIRECTORY_IS_REQUIRED,
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
                message=self.error_messages.
                S3_OUTPUT_BUCKET_IS_REQUIRED,
                request_kwargs=self.request.kwargs)


