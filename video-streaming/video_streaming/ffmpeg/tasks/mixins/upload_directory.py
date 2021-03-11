import urllib3
from celery import Task
from video_streaming.core.tasks import BaseTask
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask
from video_streaming.ffmpeg.utils import S3UploadDirectoryCallback
from .output import BaseOutputMixin


class UploadDirectoryMixin(BaseOutputMixin):
    request: Task.request

    directory: str
    s3_output_key: str
    s3_output_bucket: str

    stop_reason: BaseStreamingTask.stop_reason
    error_messages: BaseStreamingTask.error_messages
    s3_service: BaseStreamingTask.s3_service
    save_job_stop_reason: BaseStreamingTask.save_job_stop_reason

    raise_ignore: BaseTask.raise_ignore

    def upload_directory(self):
        """upload the directory of the output files
         to S3 object storage
         """
        if self.directory is None:
            # TODO notify developer
            self.save_output_status(self.output_status.OUTPUT_FAILED)
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.DIRECTORY_IS_REQUIRED)

        if self.s3_output_key is None:
            # TODO notify developer
            self.save_output_status(self.output_status.OUTPUT_FAILED)
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.S3_OUTPUT_KEY_IS_REQUIRED)

        if self.s3_output_bucket is None:
            # TODO notify developer
            self.save_output_status(self.output_status.OUTPUT_FAILED)
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.S3_OUTPUT_BUCKET_IS_REQUIRED)

        try:
            self.s3_service.upload_directory(
                self.s3_output_key,
                self.directory,
                bucket_name=self.s3_output_bucket,
                directory_callback=S3UploadDirectoryCallback(
                    task=self,
                    task_id=self.request.id.__str__()
                ).progress
            )
        except urllib3.exceptions.HeaderParsingError as e:
            # MissingHeaderBodySeparatorDefect
            # TODO notify developer
            self.logger.error(e)
            self.save_output_status(self.output_status.OUTPUT_FAILED)
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.CAN_NOT_UPLOAD_DIRECTORY)
