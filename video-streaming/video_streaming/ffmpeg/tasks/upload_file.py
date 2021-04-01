import os
import urllib3
from functools import partial
from abc import ABC
from botocore import exceptions as botocore_exceptions
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.core.constants import CacheKeysTemplates
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from video_streaming.ffmpeg.utils import S3UploadCallback
from .base import BaseStreamingTask
from .mixins import UploadFileMixin


class UploadFileTask(
        ChainCallbackMixin,
        UploadFileMixin,
        BaseStreamingTask,
        ABC
        ):

    # rewrite BaseOutputMixin.save_failed
    def save_failed(self, request_id, output_id):
        super().save_failed(request_id, output_id)
        # stop reason will only be set if there is no reason before.
        # set common reason for the task after many retries or etc.
        self.save_job_stop_reason(
            self.stop_reason.FAILED_UPLOAD_FILE,
            request_id
        )


@celery_app.task(name="upload_file",
                 base=UploadFileTask,
                 **TASK_DECORATOR_KWARGS)
def upload_file(self,
                *args,
                file_path: str = None,
                s3_output_key: str = None,
                s3_output_bucket: str = settings.S3_DEFAULT_OUTPUT_BUCKET_NAME,
                request_id: str = None,
                output_id: str = None,
                **kwargs
                ):
    """upload the output file to the S3 object storage

    Args:
        self ():
        *args ():
        file_path ():
        s3_output_key ():
        s3_output_bucket ():
        request_id ():
        output_id ():
        **kwargs ():

    Returns:

    """

    self.check_upload_file_requirements(
        request_id=request_id,
        output_id=output_id,
        file_path=file_path,
        s3_output_key=s3_output_key,
        s3_output_bucket=s3_output_bucket)

    if self.is_forced_to_stop(request_id):
        raise self.raise_revoke(request_id)

    if self.is_output_forced_to_stop(request_id, output_id):
        raise self.raise_revoke_output(request_id, output_id)

    # try
    file_size = os.stat(file_path).st_size
    if file_size == 0:
        self.save_job_stop_reason(
            self.stop_reason.INTERNAL_ERROR,
            request_id)
        raise self.raise_ignore(
            message=self.error_messages.CAN_NOT_UPLOAD_EMPTY_FILE,
            request_kwargs=self.request.kwargs)

    # save output status using output_id and request_id
    self.save_output_status(
        self.output_status.UPLOADING,
        output_id,
        request_id
    )

    file_progress_callback = S3UploadCallback(
            task=self,
            task_id=self.request.id.__str__(),
            output_id=output_id,
            request_id=request_id,
        ).file_progress

    try:
        self.s3_service.upload_file_by_path(
            key=s3_output_key,
            file_path=file_path,
            bucket_name=s3_output_bucket,
            callback=partial(
                file_progress_callback,
                file_size)
        )
    # except urllib3.exceptions.HeaderParsingError as e:
    #     # TODO notify developer
    #     self.logger.error(e)
    #     self.save_job_stop_reason(
    #         self.stop_reason.INTERNAL_ERROR,
    #         request_id)
    #     raise self.raise_ignore(
    #         message=self.error_messages.CAN_NOT_UPLOAD_FILE,
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


    # save output file size
    self.cache.set(
        CacheKeysTemplates.OUTPUT_SIZE.format(
            request_id=request_id,
            output_id=output_id),
        file_size)

    self.save_output_status(
        self.output_status.UPLOADING_FINISHED,
        output_id,
        request_id)
