import os
from functools import partial
from celery import Task
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask
from video_streaming.ffmpeg.utils import S3UploadCallback
from video_streaming.ffmpeg.package_validation import (
    ordered_package_files,
    validate_streaming_package,
)
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

    @staticmethod
    def get_directory_size(
            directory: str,
            manifest_name: str) -> tuple[int, list[tuple[str, str, int]]]:
        files = ordered_package_files(directory, manifest_name)
        total_size = sum(file_size for _, _, file_size in files)
        return total_size, files

    def upload_directory(self,
                         directory,
                         s3_output_key,
                         s3_output_bucket,
                         output_id,
                         request_id,
                         protocol) -> int:
        """upload the directory of the output files to S3 object storage
         Returns directory size
         """

        manifest_name = os.path.basename(s3_output_key)
        manifest_path = os.path.join(directory, manifest_name)
        validate_streaming_package(directory, manifest_path, protocol)

        directory_callback = S3UploadCallback(
                task=self,
                task_id=self.request.id.__str__(),
                output_id=output_id,
                request_id=request_id,
            ).directory_progress

        total_size, files = self.get_directory_size(directory, manifest_name)
        total_files = len(files)
        for number, (file_path, relative_path, file_size) in \
                enumerate(files):
            partial_callback = partial(
                directory_callback,
                total_size,
                total_files,
                number)

            # Preserve nested package paths under the manifest's object prefix.
            s3_folder = s3_output_key.rpartition('/')[0]
            object_key = "/".join(
                part for part in (s3_folder, relative_path) if part
            )

            self.s3_service.upload_file_by_path(
                key=object_key,
                file_path=file_path,
                bucket_name=s3_output_bucket,
                callback=partial_callback
            )

        # return the directory size
        return total_size
