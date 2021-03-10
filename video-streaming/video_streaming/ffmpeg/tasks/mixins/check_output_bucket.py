from video_streaming.core.tasks import BaseTask
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask


class CheckOutputBucketMixin(object):
    s3_output_bucket: str
    s3_create_bucket: bool

    primary_status: BaseStreamingTask.primary_status
    failed_reason: BaseStreamingTask.failed_reason
    error_messages: BaseStreamingTask.error_messages
    s3_service: BaseStreamingTask.s3_service
    save_job_failed_reason: BaseStreamingTask.save_job_failed_reason
    save_primary_status: BaseStreamingTask.save_primary_status

    raise_ignore: BaseTask.raise_ignore

    def create_output_bucket(self):
        """create output bucket"""

        try:
            # create s3_output_bucket
            self.s3_service.create_bucket(
                bucket_name=self.s3_output_bucket)
        except self.s3_service.exceptions.BucketExist:
            pass

    def get_output_bucket_details(self):
        """get output bucket details by s3_output_bucket

        using self.s3_service to send head bucket request to S3
        and get bucket details

        bucket_details is None for 404 or 403 reason

        Returns:
          bucket_details
        """

        if self.s3_output_bucket is None:
            self.save_primary_status(self.primary_status.FAILED)
            self.save_job_failed_reason(
                self.failed_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.S3_OUTPUT_BUCKET_IS_REQUIRED)

        # get bucket details
        bucket_details = self.s3_service.head_bucket(
            bucket_name=self.s3_output_bucket)

        # if not bucket_details:
        #     if not self.s3_create_bucket:
        #         raise self.raise_ignore(
        #             message=self.error_messages.OUTPUT_BUCKET_404_OR_403)
        #     self.create_output_bucket()

        return bucket_details
