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

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        self.save_primary_status(self.primary_status.FAILED)
        self.save_input_status(self.input_status.INPUT_FAILED)
        self.save_job_failed_reason(
            self.failed_reason.FAILED_ANALYZE_INPUT)
        return super().on_failure(exc, task_id, args, kwargs, einfo)


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

    # save input status using input_number and request_id
    self.save_input_status(self.input_status.ANALYZING)

    try:
        self.analyze_input()
    except RuntimeError as e:
        # TODO capture error and notify developer
        raise self.retry(exc=e)
    except Exception as e:
        # FileNotFoundError: [Errno 2] No such file or directory: 'ffprobe'
        # TODO capture error and notify developer
        raise self.retry(exc=e)

    # save input status using input_number and request_id
    self.save_input_status(self.input_status.ANALYZING_FINISHED)

    return dict(input_path=self.input_path)
