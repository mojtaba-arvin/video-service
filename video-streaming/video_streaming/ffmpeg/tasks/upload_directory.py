from abc import ABC
from celery import states
from video_streaming import settings
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

    def save_failed(self, request_id, output_number):
        self.save_output_status(
            self.output_status.OUTPUT_FAILED,
            output_number,
            request_id
        )
        # stop reason will only be set if there is no reason before.
        # set common reason for the task after many retries or etc.
        self.save_job_stop_reason(
            self.stop_reason.FAILED_UPLOAD_DIRECTORY,
            request_id
        )

    def on_failure(self, *request_args, **request_kwargs):
        request_id = request_kwargs.get('request_id', None)
        output_number = request_kwargs.get('output_number', None)
        if request_id is not None and output_number is not None:
            self.save_failed(
                request_id,
                output_number
            )
        return super().on_failure(*request_args, **request_kwargs)

    def raise_ignore(self,
                     message=None,
                     state=states.FAILURE,
                     request_kwargs: dict = None):
        if request_kwargs:
            request_id = request_kwargs.get('request_id', None)
            output_number = request_kwargs.get('output_number', None)
            if request_id is not None and output_number is not None:
                if state == states.FAILURE:
                    self.save_failed(
                        request_id,
                        output_number
                    )
                elif state == states.REVOKED:
                    self.save_output_status(
                        self.output_status.OUTPUT_REVOKED,
                        output_number,
                        request_id
                    )
        super().raise_ignore(
            message=message,
            state=state,
            request_kwargs=request_kwargs)


@celery_app.task(name="upload_directory",
                 base=UploadDirectoryTask,
                 **TASK_DECORATOR_KWARGS)
def upload_directory(self,
                     *args,
                     directory: str = None,
                     s3_output_key: str = None,
                     s3_output_bucket: str = settings.S3_DEFAULT_INPUT_BUCKET_NAME,
                     request_id: str = None,
                     output_number: int = None):
    """upload the directory of the output files to S3 object storage

        Kwargs:
         directory:
            The response of S3 head object request for input video

       required parameters:
         - request_id
         - output_number
         - directory
         - s3_input_key
    """

    self.check_upload_directory_requirements(
        request_id=request_id,
        output_number=output_number,
        directory=directory,
        s3_output_key=s3_output_key,
        s3_output_bucket=s3_output_bucket)

    if self.is_forced_to_stop(request_id):
        raise self.raise_revoke(request_id)

    # save output status using output_number and request_id
    self.save_output_status(
        self.output_status.PLAYLIST_UPLOADING,
        output_number,
        request_id
    )

    self.upload_directory(
        directory,
        s3_output_key,
        s3_output_bucket,
        output_number,
        request_id)

    self.save_output_status(
        self.output_status.UPLOADING_FINISHED,
        output_number,
        request_id)
