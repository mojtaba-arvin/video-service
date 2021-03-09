from video_streaming.core.tasks import BaseTask
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask


class CheckOutputBucketMixin(object):
    s3_output_bucket: str
    s3_create_bucket: bool

    error_messages: BaseStreamingTask.error_messages
    s3_service: BaseStreamingTask.s3_service

    raise_ignore: BaseTask.raise_ignore

    def create_output_bucket(self):
        """create output bucket"""

        try:
            # create s3_output_bucket
            self.s3_service.create_bucket(
                bucket_name=self.s3_output_bucket)
        except self.s3_service.exceptions.BucketExist:
            pass

    def ensure_bucket_exist(self):
        """ensure bucket exist

        1. send head bucket request to S3 to check bucket exist or no
        2. check the task s3_create_bucket boolean param to create a
           output bucket when does not exist.
           ignore the task when s3_create_bucket is False/None and the
           output bucket does not exist.
        """

        if self.s3_output_bucket is None:
            raise self.raise_ignore(
                message=self.error_messages.S3_OUTPUT_BUCKET_IS_REQUIRED)

        # check output bucket is exist
        bucket_details = self.s3_service.head_bucket(
            bucket_name=self.s3_output_bucket)
        if not bucket_details:
            if not self.s3_create_bucket:
                raise self.raise_ignore(
                    message=self.error_messages.OUTPUT_BUCKET_404_OR_403)
            self.create_output_bucket()
