from ffmpeg_streaming import FFProbe
from video_streaming import settings
from video_streaming.core.tasks import BaseTask
from video_streaming.core.constants.cache_keys import CacheKeysTemplates
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask


class AnalyzeInputMixin(object):
    input_path: str
    request_id: str  # grpc request tracking id
    input_number: str

    stop_reason: BaseStreamingTask.stop_reason
    cache: BaseStreamingTask.cache
    error_messages: BaseStreamingTask.error_messages
    s3_service: BaseStreamingTask.s3_service
    save_job_stop_reason: BaseStreamingTask.save_job_stop_reason

    raise_ignore: BaseTask.raise_ignore

    def analyze_input(self):
        if self.input_path is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.INPUT_PATH_IS_REQUIRED)

        if self.request_id is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.REQUEST_ID_IS_REQUIRED)

        ffprobe = FFProbe(
            self.input_path,
            cmd=settings.FFPROBE_BIN_PATH)
        """
        examples :
            ffprobe.format()
            ffprobe.all()
            ffprobe.streams().audio().get('bit_rate', 0)
        """

        self.cache.set(
            CacheKeysTemplates.INPUT_FFPROBE_DATA.format(
                request_id=self.request_id,
                input_number=self.input_number
            ),
            ffprobe.out)
        return ffprobe
