from celery import Task
from ffmpeg_streaming import FFProbe
from video_streaming import settings
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask
from video_streaming.ffmpeg.constants import InputType
from .input import BaseInputMixin


class AnalyzeInputMixin(BaseInputMixin):

    input_status: BaseStreamingTask.input_status
    stop_reason: BaseStreamingTask.stop_reason
    cache: BaseStreamingTask.cache
    error_messages: BaseStreamingTask.error_messages
    s3_service: BaseStreamingTask.s3_service
    save_job_stop_reason: BaseStreamingTask.save_job_stop_reason

    request = Task.request
    retry: Task.retry

    def check_analyze_requirements(
            self,
            request_id=None,
            input_number=None,
            video_path=None,
            watermark_path=None,
            input_type=None
            ):

        if request_id is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.REQUEST_ID_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if input_number is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.INPUT_NUMBER_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if input_type is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.INPUT_TYPE_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if (input_type == InputType.WATERMARK_INPUT
            and watermark_path is None
            ) or (
                input_type == InputType.VIDEO_INPUT
                and video_path is None):
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.INPUT_PATH_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

    def analyze_video(self, input_path) -> FFProbe:

        try:
            return FFProbe(
                input_path,
                cmd=settings.FFPROBE_BIN_PATH)
        except RuntimeError as e:
            # TODO ignore if RuntimeError('It could not determine the value of width/height')
            # TODO capture error and notify developer
            print(e)
            raise self.retry(exc=e)
        except Exception as e:
            # FileNotFoundError:
            # [Errno 2] No such file or directory: 'ffprobe'
            # TODO capture error and notify developer
            print(e)
            raise self.retry(exc=e)
