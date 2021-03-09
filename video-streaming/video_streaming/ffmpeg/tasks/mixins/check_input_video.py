from video_streaming.core.tasks import BaseTask
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask


class CheckInputMixin(object):
    s3_input_key: str
    s3_input_bucket: str

    error_messages: BaseStreamingTask.error_messages
    s3_service: BaseStreamingTask.s3_service

    raise_ignore: BaseTask.raise_ignore

    def check_input_video(self) -> dict:
        """check s3_input_key on s3_input_bucket

        1. using self.s3_service to send head object request to S3
            and get object details
        2. ignore the task, when object_details is None for 404 or
            403 reason

        Returns:
          object_details
        """

        if self.s3_input_key is None:
            raise self.raise_ignore(
                message=self.error_messages.S3_INPUT_KEY_IS_REQUIRED)

        if self.s3_input_bucket is None:
            raise self.raise_ignore(
                message=self.error_messages.S3_INPUT_BUCKET_IS_REQUIRED)

        # check s3_input_key on s3_input_bucket
        object_details = self.s3_service.head(
            key=self.s3_input_key,
            bucket_name=self.s3_input_bucket)
        if not object_details:
            raise self.raise_ignore(
                message=self.error_messages.INPUT_VIDEO_404_OR_403)

        return object_details
