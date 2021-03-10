import os
from abc import ABC
from video_streaming.celery import celery_app
from video_streaming.core.tasks import ChordCallbackMixin
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from .base import BaseStreamingTask
from .mixins import DownloadInputMixin, BaseInputMixin


class DownloadInputTask(
        ChordCallbackMixin,
        DownloadInputMixin,
        BaseInputMixin,
        BaseStreamingTask,
        ABC
        ):

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        self.save_primary_status(self.primary_status.FAILED)
        self.save_input_status(self.input_status.INPUT_FAILED)

        # notice : failed reason will only be set if there is no reason
        #  before.

        # set common reason for the task, it's can be connection error
        # after many retries or etc.
        self.save_job_failed_reason(
            self.failed_reason.DOWNLOADING_FAILED)
        return super().on_failure(exc, task_id, args, kwargs, einfo)


@celery_app.task(name="download_input",
                 base=DownloadInputTask,
                 **TASK_DECORATOR_KWARGS)
def download_input(self,
                   *args,
                   object_details: dict = None,
                   request_id: str = None,
                   s3_input_key: str = None,
                   s3_input_bucket: str = None,
                   input_number: int = None
                   ) -> dict:
    """download video to local input path

       required parameters:
         - object_details
         - request_id
         - s3_input_key
         - s3_input_bucket
    """

    self._initial_params()

    # save primary status using request_id
    self.save_primary_status(self.primary_status.INPUTS_DOWNLOADING)

    # save input status using input_number and request_id
    self.save_input_status(self.input_status.PREPARATION_DOWNLOAD)

    # set self.input_path
    self.set_input_path()

    downloaded = os.path.exists(self.input_path)
    if not downloaded:
        if not self.download_video():
            # the input video is 404 or 403
            # set primary status
            self.save_primary_status(self.primary_status.FAILED)
            # set input status
            self.save_input_status(self.input_status.INPUT_FAILED)
            # set failed reason
            self.save_job_failed_reason(
                self.failed_reason.INPUT_VIDEO_ON_S3_IS_404_OR_403)
            # ignore task
            raise self.raise_ignore(
                message=self.error_messages.INPUT_VIDEO_404_OR_403)

    # save input status using input_number and request_id
    self.save_input_status(self.input_status.DOWNLOADING_FINISHED)

    self.incr_ready_inputs()

    return dict(input_path=self.input_path)
