from abc import ABC
from video_streaming.celery import celery_app
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from .base import BaseStreamingTask
from .mixins import BaseCheckMixin, CheckInputMixin


class CheckInputKeyTask(
        CheckInputMixin,
        BaseCheckMixin,
        BaseStreamingTask,
        ABC
        ):
    pass


@celery_app.task(name="check_input_key",
                 base=CheckInputKeyTask,
                 **TASK_DECORATOR_KWARGS)
def check_input_key(self,
                    *args,
                    s3_input_key: str = None,
                    s3_input_bucket: str = None,
                    request_id: str = None) -> dict:
    """check s3_input_key is exist on s3_input_bucket

       required parameters:
         - s3_input_key
         - s3_input_bucket
    """

    self._initial_params()
    self.save_primary_status(self.primary_status.CHECKING)

    # check s3_input_key on s3_input_bucket by head request
    object_details = self.check_input_video()

    self.incr_passed_checks()

    return dict(object_details=object_details)
