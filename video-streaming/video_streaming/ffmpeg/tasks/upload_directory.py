import urllib3
from abc import ABC
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.core.constants import CacheKeysTemplates
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from .base import BaseStreamingTask
from .mixins import UploadDirectoryMixin


class UploadDirectoryTask(
        ChainCallbackMixin,
        UploadDirectoryMixin,
        BaseStreamingTask,
        ABC
        ):

    # rewrite BaseOutputMixin.save_failed
    def save_failed(self, request_id, output_id):
        super().save_failed(request_id, output_id)
        # stop reason will only be set if there is no reason before.
        # set common reason for the task after many retries or etc.
        self.save_job_stop_reason(
            self.stop_reason.FAILED_UPLOAD_DIRECTORY,
            request_id
        )


@celery_app.task(name="upload_directory",
                 base=UploadDirectoryTask,
                 **TASK_DECORATOR_KWARGS)
def upload_directory(self,
                     *args,
                     directory: str = None,
                     s3_output_key: str = None,
                     s3_output_bucket: str = settings.S3_DEFAULT_OUTPUT_BUCKET_NAME,
                     request_id: str = None,
                     output_id: str = None,
                     **kwargs
                     ):
    """upload the directory of the output files to S3 object storage

    Args:
        self ():
        *args ():
        directory (): The response of S3 head object request for input video
        s3_output_key ():
        s3_output_bucket ():
        request_id ():
        output_id ():
        **kwargs ():

    Returns:

    """

    self.check_upload_directory_requirements(
        request_id=request_id,
        output_id=output_id,
        directory=directory,
        s3_output_key=s3_output_key,
        s3_output_bucket=s3_output_bucket)

    if self.is_forced_to_stop(request_id):
        raise self.raise_revoke(request_id)

    if self.is_output_forced_to_stop(request_id, output_id):
        raise self.raise_revoke_output(request_id, output_id)

    # save output status using output_id and request_id
    self.save_output_status(
        self.output_status.UPLOADING,
        output_id,
        request_id
    )

    try:
        directory_size = self.upload_directory(
            directory,
            s3_output_key,
            s3_output_bucket,
            output_id,
            request_id)
    # except urllib3.exceptions.HeaderParsingError as e:
    #     # MissingHeaderBodySeparatorDefect
    #     # TODO notify developer
    #     self.logger.error(e)
    #     self.save_job_stop_reason(
    #         self.stop_reason.INTERNAL_ERROR,
    #         request_id)
    #     raise self.raise_ignore(
    #         message=self.error_messages.CAN_NOT_UPLOAD_DIRECTORY,
    #         request_kwargs=self.request.kwargs)
    except Exception as e:
        # botocore_exceptions.ParamValidationError
        print(e)
        self.save_job_stop_reason(
            self.stop_reason.INTERNAL_ERROR,
            request_id)
        raise self.raise_ignore(
            message=self.error_messages.CAN_NOT_UPLOAD_FILE,
            request_kwargs=self.request.kwargs)

    # save output directory size
    self.cache.set(
        CacheKeysTemplates.OUTPUT_SIZE.format(
            request_id=request_id,
            output_id=output_id),
        directory_size)

    self.save_output_status(
        self.output_status.UPLOADING_FINISHED,
        output_id,
        request_id)
