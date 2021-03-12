from abc import ABC
from celery import states
from video_streaming.celery import celery_app
from video_streaming.core.constants import CacheKeysTemplates
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
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
            self.stop_reason.DOWNLOADING_FAILED,
            request_id
        )


@celery_app.task(name="analyze_input",
                 base=AnalyzeInputTask,
                 **TASK_DECORATOR_KWARGS)
def analyze_input(self,
                  *args,
                  input_path: str = None,
                  request_id: str = None,
                  input_number: int = None
                  ) -> dict:
    """analyze input with ffprobe

        to save video information on cache

       required parameters:
         - request_id
         - input_number
         - input_path
    """
    self.check_analyze_requirements(
        request_id=request_id,
        input_number=input_number,
        input_path=input_path)

    if self.is_forced_to_stop(request_id):
        raise self.raise_revoke(request_id)

    # save input status using input_number and request_id
    self.save_input_status(
        self.input_status.ANALYZING,
        input_number,
        request_id
    )

    ffprobe = self.analyze_input(input_path)

    """
    examples :
        ffprobe.format()
        ffprobe.all()
        ffprobe.streams().audio().get('bit_rate', 0)
    """

    self.cache.set(
        CacheKeysTemplates.INPUT_FFPROBE_DATA.format(
            request_id=request_id,
            input_number=input_number
        ),
        ffprobe.out)

    # save input status using input_number and request_id
    self.save_input_status(
        self.input_status.ANALYZING_FINISHED,
        input_number,
        request_id
    )

    return dict(input_path=input_path)
