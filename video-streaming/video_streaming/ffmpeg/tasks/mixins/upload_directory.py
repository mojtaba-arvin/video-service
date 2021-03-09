from celery import Task
from video_streaming.core.tasks import BaseTask
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask
from video_streaming.ffmpeg.utils import S3UploadDirectoryCallback


class UploadDirectoryMixin(object):
    request: Task.request

    directory: str
    s3_output_key: str
    s3_output_bucket: str

    error_messages: BaseStreamingTask.error_messages
    s3_service: BaseStreamingTask.s3_service

    raise_ignore: BaseTask.raise_ignore

    def upload_directory(self):
        """upload the directory of the output files
         to S3 object storage
         """
        if self.directory is None:
            raise self.raise_ignore(
                message=self.error_messages.DIRECTORY_IS_REQUIRED)

        if self.s3_output_key is None:
            raise self.raise_ignore(
                message=self.error_messages.S3_OUTPUT_KEY_IS_REQUIRED)

        if self.s3_output_bucket is None:
            raise self.raise_ignore(
                message=self.error_messages.S3_OUTPUT_BUCKET_IS_REQUIRED)

        self.s3_service.upload_directory(
            self.s3_output_key,
            self.directory,
            bucket_name=self.s3_output_bucket,
            directory_callback=S3UploadDirectoryCallback(
                task=self,
                task_id=self.request.id.__str__()
            ).progress
        )
