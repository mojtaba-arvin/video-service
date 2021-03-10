from video_streaming.core.tasks import BaseTask
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask


class CheckOutputKeyMixin(object):
    s3_output_key: str
    s3_output_bucket: str
    s3_dont_replace: bool

    primary_status: BaseStreamingTask.primary_status
    failed_reason: BaseStreamingTask.failed_reason
    error_messages: BaseStreamingTask.error_messages
    s3_service: BaseStreamingTask.s3_service
    save_job_failed_reason: BaseStreamingTask.save_job_failed_reason
    save_primary_status: BaseStreamingTask.save_primary_status

    raise_ignore: BaseTask.raise_ignore

    def has_upload_risk(self) -> bool:
        """check if s3_output_key is already exist

        this check is for prevent replace the output
        by send head object request for key of output
        """

        if self.s3_output_key is None:
            self.save_primary_status(self.primary_status.FAILED)
            self.save_job_failed_reason(
                self.failed_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.S3_OUTPUT_KEY_IS_REQUIRED)

        if self.s3_output_bucket is None:
            self.save_primary_status(self.primary_status.FAILED)
            self.save_job_failed_reason(
                self.failed_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.S3_OUTPUT_BUCKET_IS_REQUIRED)

        return True if self.s3_service.head(
                key=self.s3_output_key,
                bucket_name=self.s3_output_bucket) else False
