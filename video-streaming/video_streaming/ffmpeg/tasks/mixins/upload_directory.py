import os
from functools import partial
from celery import Task
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask
from video_streaming.ffmpeg.utils import S3UploadCallback
from video_streaming.ffmpeg.package_publication import publish_package
from .output import BaseOutputMixin


class UploadDirectoryMixin(BaseOutputMixin):

    stop_reason: BaseStreamingTask.stop_reason
    error_messages: BaseStreamingTask.error_messages
    s3_service: BaseStreamingTask.s3_service
    save_job_stop_reason: BaseStreamingTask.save_job_stop_reason

    request: Task.request

    def check_upload_directory_requirements(
            self,
            request_id=None,
            output_id=None,
            directory=None,
            s3_output_key=None,
            s3_output_bucket=None):

        if request_id is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.REQUEST_ID_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if output_id is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.OUTPUT_NUMBER_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if directory is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.DIRECTORY_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if s3_output_key is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.S3_OUTPUT_KEY_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if s3_output_bucket is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.
                S3_OUTPUT_BUCKET_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

    def upload_directory(
            self,
            directory,
            s3_output_key,
            s3_output_bucket,
            output_id,
            request_id,
            protocol,
            expected_renditions=None) -> dict:
        """Validate and publish package artifacts with manifests last."""
        directory_callback = S3UploadCallback(
            task=self,
            task_id=self.request.id.__str__(),
            output_id=output_id,
            request_id=request_id,
        ).directory_progress

        manifest_path = os.path.join(
            directory, os.path.basename(s3_output_key)
        )
        publication = publish_package(
            directory=directory,
            manifest_path=manifest_path,
            protocol=protocol,
            expected_renditions=expected_renditions,
            s3_output_key=s3_output_key,
            s3_output_bucket=s3_output_bucket,
            storage=self.s3_service,
            callback_factory=lambda index, total_files, total_size: partial(
                directory_callback, total_size, total_files, index
            ),
        )
        return publication
