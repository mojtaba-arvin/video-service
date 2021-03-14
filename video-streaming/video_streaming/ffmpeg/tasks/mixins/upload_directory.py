import urllib3
from celery import Task
from video_streaming.core.tasks import BaseTask
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask
from video_streaming.ffmpeg.utils import S3UploadDirectoryCallback
from .output import BaseOutputMixin


class UploadDirectoryMixin(BaseOutputMixin):

    stop_reason: BaseStreamingTask.stop_reason
    error_messages: BaseStreamingTask.error_messages
    s3_service: BaseStreamingTask.s3_service
    save_job_stop_reason: BaseStreamingTask.save_job_stop_reason

    raise_ignore: BaseTask.raise_ignore
    request: Task.request

    def check_upload_directory_requirements(
            self,
            request_id=None,
            output_number=None,
            directory=None,
            s3_output_key=None,
            s3_output_bucket=None):

        if request_id is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.REQUEST_ID_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if output_number is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.OUTPUT_NUMBER_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if directory is None:
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

    def upload_directory(self,
                         directory,
                         s3_output_key,
                         s3_output_bucket,
                         output_number,
                         request_id):
        """upload the directory of the output files
         to S3 object storage"""
        try:
            self.s3_service.upload_directory(
                s3_output_key,
                directory,
                bucket_name=s3_output_bucket,
                directory_callback=S3UploadDirectoryCallback(
                    task=self,
                    task_id=self.request.id.__str__(),
                    output_number=output_number,
                    request_id=request_id,
                ).progress
            )
        except urllib3.exceptions.HeaderParsingError as e:
            # MissingHeaderBodySeparatorDefect
            # TODO notify developer
            self.logger.error(e)
            self.save_output_status(
                self.output_status.OUTPUT_FAILED,
                output_number,
                request_id)
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.CAN_NOT_UPLOAD_DIRECTORY,
                request_kwargs=self.request.kwargs)
