from abc import ABC
from video_streaming.celery import celery_app
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from .base import BaseStreamingTask
from .mixins import AnalyzeInputMixin, BaseInputMixin


class AnalyzeInputTask(
        ChainCallbackMixin,
        AnalyzeInputMixin,
        BaseInputMixin,
        BaseStreamingTask,
        ABC
        ):
    pass


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

       required parameters:
         - input_path
         - request_id
    """

    self._initial_params()

    # TODO : change status

    self.analyze_input()

    return dict(input_path=self.input_path)
