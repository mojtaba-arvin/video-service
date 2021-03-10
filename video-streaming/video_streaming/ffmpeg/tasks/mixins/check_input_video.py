from video_streaming.core.tasks import BaseTask
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask


class CheckInputMixin(object):
    s3_input_key: str
    s3_input_bucket: str

    primary_status: BaseStreamingTask.primary_status
    failed_reason: BaseStreamingTask.failed_reason
    error_messages: BaseStreamingTask.error_messages
    s3_service: BaseStreamingTask.s3_service
    save_job_failed_reason: BaseStreamingTask.save_job_failed_reason
    save_primary_status: BaseStreamingTask.save_primary_status

    raise_ignore: BaseTask.raise_ignore

    def get_object_details(self) -> None or dict:
        """get object details by s3_input_key on s3_input_bucket

        using self.s3_service to send head object request to S3
        and get object details

        object_details is None for 404 or 403 reason

        Returns:
          object_details
        """

        if self.s3_input_key is None:
            # TODO notify developer
            self.save_primary_status(self.primary_status.FAILED)
            self.save_job_failed_reason(
                self.failed_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.S3_INPUT_KEY_IS_REQUIRED)

        if self.s3_input_bucket is None:
            self.save_primary_status(self.primary_status.FAILED)
            self.save_job_failed_reason(
                self.failed_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.S3_INPUT_BUCKET_IS_REQUIRED)

        # get object details
        object_details = self.s3_service.head(
            key=self.s3_input_key,
            bucket_name=self.s3_input_bucket)

        # if not object_details:
        #     raise self.raise_ignore(
        #         message=self.error_messages.INPUT_VIDEO_404_OR_403)

        return object_details
