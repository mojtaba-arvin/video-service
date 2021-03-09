from video_streaming.core.tasks import BaseTask
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask


class CheckOutputKeyMixin(object):
    s3_output_key: str
    s3_output_bucket: str
    s3_dont_replace: bool

    error_messages: BaseStreamingTask.error_messages
    s3_service: BaseStreamingTask.s3_service

    raise_ignore: BaseTask.raise_ignore

    def check_output_key(self):
        """check if s3_output_key is already exist

        this check is for prevent replace the output

        1. send head object request for key of output
        2. if the s3_dont_replace boolean param of the task is True,
           ignore the task.
        """

        if self.s3_output_key is None:
            raise self.raise_ignore(
                message=self.error_messages.S3_OUTPUT_KEY_IS_REQUIRED)

        if self.s3_output_bucket is None:
            raise self.raise_ignore(
                message=self.error_messages.S3_OUTPUT_BUCKET_IS_REQUIRED)

        if self.s3_service.head(
                key=self.s3_output_key,
                bucket_name=self.s3_output_bucket):
            if self.s3_dont_replace:
                raise self.raise_ignore(
                    message=self.error_messages.OUTPUT_KEY_IS_ALREADY_EXIST)
