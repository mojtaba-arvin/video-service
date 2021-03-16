from celery import Task
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask
from .output import BaseOutputMixin


class GenerateThumbnailMixin(BaseOutputMixin):

    stop_reason: BaseStreamingTask.stop_reason
    error_messages: BaseStreamingTask.error_messages
    get_outputs_root_directory: BaseStreamingTask.\
        get_outputs_root_directory
    save_job_stop_reason: BaseStreamingTask.save_job_stop_reason

    request = Task.request
    retry: Task.retry

    def check_generate_thumbnail_requirements(
            self,
            request_id=None,
            output_number=None,
            input_path=None,
            output_path=None,
            s3_output_key=None):

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

        if input_path is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.INPUT_PATH_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if output_path is None and s3_output_key is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.
                OUTPUT_PATH_OR_S3_OUTPUT_KEY_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

