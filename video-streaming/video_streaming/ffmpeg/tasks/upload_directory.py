from abc import ABC
from video_streaming.celery import celery_app
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from .base import BaseStreamingTask
from .mixins import BaseOutputMixin, UploadDirectoryMixin


class UploadDirectoryTask(
        ChainCallbackMixin,
        UploadDirectoryMixin,
        BaseOutputMixin,
        BaseStreamingTask,
        ABC
        ):
    pass


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

    # It will be used to safely delete local outputs
    # after all outputs have been uploaded
    self.incr_ready_outputs()
