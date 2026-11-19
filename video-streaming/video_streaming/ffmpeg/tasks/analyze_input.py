from abc import ABC
from video_streaming.celery import celery_app
from video_streaming.core.constants import CacheKeysTemplates
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS, \
    InputType
from .base import BaseStreamingTask
from .mixins import AnalyzeInputMixin


class AnalyzeInputTask(
        ChainCallbackMixin,
        AnalyzeInputMixin,
        BaseStreamingTask,
        ABC
        ):

    # rewrite BaseInputMixin.save_failed
    def save_failed(self, request_id, input_number):
        super().save_failed(request_id, input_number)
        # stop reason will only be set if there is no reason before.
        # set common reason for the task after many retries or etc.
        self.save_job_stop_reason(
            self.stop_reason.FAILED_ANALYZE_INPUT,
            request_id
        )


@celery_app.task(name="analyze_input",
                 base=AnalyzeInputTask,
                 **TASK_DECORATOR_KWARGS)
def analyze_input(self,
                  *args,
                  video_path: str = None,
                  watermark_path: str = None,
                  request_id: str = None,
                  input_number: int = None,
                  input_type: str = InputType.VIDEO_INPUT
                  ) -> dict:
    """analyze input with ffprobe/.. to save input information on the cache

    one of video_details or watermark_details is required according
    to input_type

    Args:
        self ():
        *args ():
        video_path (): the local path of downloaded video
        watermark_path (): the local path of downloaded watermark
        request_id ():
        input_number ():
        input_type ():

    Returns:

    """

    self.check_analyze_requirements(
        request_id=request_id,
        input_number=input_number,
        video_path=video_path,
        watermark_path=watermark_path,
        input_type=input_type)

    if self.is_forced_to_stop(request_id):
        raise self.raise_revoke(request_id)

    # save input status using input_number and request_id
    self.save_input_status(
        self.input_status.ANALYZING,
        input_number,
        request_id
    )

    if input_type == InputType.VIDEO_INPUT:
        input_path = video_path
        ffprobe = self.analyze_video(input_path)
        """
        examples :
            ffprobe.format()
            ffprobe.all()
            ffprobe.streams().audio().get('bit_rate', 0)
        """

        if ffprobe.streams().video()['codec_type'] != 'video':
            self.save_job_stop_reason(
                self.stop_reason.INPUT_VIDEO_CODEC_TYPE_IN_NOT_VIDEO,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.INPUT_CODEC_TYPE_IN_NOT_VIDEO,
                request_kwargs=self.request.kwargs)

        self.cache.set(
            CacheKeysTemplates.INPUT_FFPROBE_DATA.format(
                request_id=request_id,
                input_number=input_number
            ),
            ffprobe.out)

    else:
        input_path = watermark_path
        # TODO analyze watermark
        # TODO convert svg to png

    # save input status using input_number and request_id
    self.save_input_status(
        self.input_status.ANALYZING_FINISHED,
        input_number,
        request_id
    )

    return {input_type + "_path": input_path}
