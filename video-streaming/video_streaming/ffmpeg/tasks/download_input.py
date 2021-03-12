import os
from abc import ABC
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.core.tasks import ChordCallbackMixin
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from .base import BaseStreamingTask
from .mixins import DownloadInputMixin, BaseInputMixin


class DownloadInputTask(
        ChordCallbackMixin,
        DownloadInputMixin,
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


@celery_app.task(name="download_input",
                 base=DownloadInputTask,
                 **TASK_DECORATOR_KWARGS)
def download_input(self,
                   *args,
                   object_details: dict = None,
                   request_id: str = None,
                   s3_input_key: str = None,
                   s3_input_bucket: str = settings.S3_DEFAULT_INPUT_BUCKET_NAME,
                   input_number: int = None
                   ) -> dict:
    """download video to local input path

        Kwargs:
          object_details:
            The local output directory
          input_number:
            input_number is using in redis key, to save progress of
            every input also, it's using to create different path
            for inputs

       required parameters:
         - object_details
         - request_id
         - s3_input_key
    """

    self.check_download_requirements(
        request_id=request_id,
        input_number=input_number,
        object_details=object_details,
        s3_input_key=s3_input_key,
        s3_input_bucket=s3_input_bucket)

    if self.is_forced_to_stop(request_id):
        raise self.raise_revoke(request_id)

    # save primary status using request_id
    self.save_primary_status(
        self.primary_status.INPUTS_DOWNLOADING,
        request_id)

    # save input status using input_number and request_id
    self.save_input_status(
        self.input_status.PREPARATION_DOWNLOAD,
        input_number,
        request_id
    )

    # generate input_path
    input_path = self.generate_input_path(
        s3_input_key,
        request_id,
        input_number)

    downloaded = os.path.exists(input_path)
    if not downloaded:
        downloaded_successfully = self.download_video(
            input_path,
            object_details,
            s3_input_key,
            s3_input_bucket,
            input_number,
            request_id)

        # downloaded_successfully is False when the input is 404 or 403
        if not downloaded_successfully:
            # set primary status
            self.save_primary_status(
                self.primary_status.FAILED,
                request_id
            )
            # set input status
            self.save_input_status(
                self.input_status.INPUT_FAILED,
                input_number,
                request_id
            )
            # set stop reason
            self.save_job_stop_reason(
                self.stop_reason.INPUT_VIDEO_ON_S3_IS_404_OR_403,
                request_id
            )
            # ignore task
            raise self.raise_ignore(
                message=self.error_messages.INPUT_VIDEO_404_OR_403,
                request_kwargs=self.request.kwargs)

    # save input status using input_number and request_id
    self.save_input_status(self.input_status.DOWNLOADING_FINISHED,
                           input_number,
                           request_id)

    self.incr_ready_inputs(request_id)

    return dict(input_path=input_path)
