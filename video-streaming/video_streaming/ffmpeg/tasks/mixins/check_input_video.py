from celery import Task
from video_streaming.core.tasks import BaseTask
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask
from .check import BaseCheckMixin


class CheckInputMixin(BaseCheckMixin):

    primary_status: BaseStreamingTask.primary_status
    stop_reason: BaseStreamingTask.stop_reason
    error_messages: BaseStreamingTask.error_messages
    s3_service: BaseStreamingTask.s3_service
    save_job_stop_reason: BaseStreamingTask.save_job_stop_reason
    save_primary_status: BaseStreamingTask.save_primary_status

    raise_ignore: BaseTask.raise_ignore
    request: Task.request

    def check_input_key_requirements(
            self,
            request_id=None,
            s3_input_key=None,
            s3_input_bucket=None
            ):
        if request_id is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.REQUEST_ID_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if s3_input_key is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.S3_INPUT_KEY_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if s3_input_bucket is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.S3_INPUT_BUCKET_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

    def get_object_details(self,
                           s3_input_key,
                           s3_input_bucket) -> None or dict:
        """get object details by s3_input_key on s3_input_bucket

        using self.s3_service to send head object request to S3
        and get object details

        object_details is None for 404 or 403 reason

        Returns:
          object_details
        """

        # get object details
        object_details = self.s3_service.head(
            key=s3_input_key,
            bucket_name=s3_input_bucket)

        # if not object_details:
        #     raise self.raise_ignore(
        #         message=self.error_messages.INPUT_VIDEO_404_OR_403)

        return object_details
