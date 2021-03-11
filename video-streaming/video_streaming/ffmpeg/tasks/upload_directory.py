from abc import ABC
from celery import states
from video_streaming.celery import celery_app
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from .base import BaseStreamingTask
from .mixins import UploadDirectoryMixin


class UploadDirectoryTask(
        ChainCallbackMixin,
        UploadDirectoryMixin,
        BaseStreamingTask,
        ABC
        ):

    def save_failed(self):
        self.save_output_status(self.output_status.OUTPUT_FAILED)
        # stop reason will only be set if there is no reason before.
        # set common reason for the task after many retries or etc.
        self.save_job_stop_reason(
            self.stop_reason.FAILED_UPLOAD_DIRECTORY)

    def on_failure(self, *args, **kwargs):
        self.save_failed()
        return super().on_failure(*args, **kwargs)

    def raise_ignore(self, message=None, state=states.FAILURE):
        if state == states.FAILURE:
            self.save_failed()
        elif state == states.REVOKED:
            self.save_output_status(self.output_status.OUTPUT_REVOKED)
        super().raise_ignore(message=message, state=state)


@celery_app.task(name="upload_directory",
                 base=UploadDirectoryTask,
                 **TASK_DECORATOR_KWARGS)
def upload_directory(self,
                     *args,
                     directory: str = None,
                     s3_output_key: str = None,
                     s3_output_bucket: str = None,
                     request_id: str = None,
                     output_number: int = None):
    """upload the directory of the output files to S3 object storage

       required parameters:
         - directory
         - s3_input_key
         - s3_input_bucket
    """

    self._initial_params()

    # save output status using output_number and request_id
    self.save_output_status(self.output_status.PLAYLIST_UPLOADING)

    self.upload_directory()

    self.save_output_status(self.output_status.UPLOADING_FINISHED)
