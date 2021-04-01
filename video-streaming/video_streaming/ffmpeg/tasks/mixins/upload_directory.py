import os
from functools import partial
from celery import Task
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask
from video_streaming.ffmpeg.utils import S3UploadCallback
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
            directory: str) -> tuple[int, list[tuple[str, str, int]]]:
        total_size = 0
        files = []
        for entry in os.scandir(directory):
            if entry.is_file():
                entry_size = entry.stat().st_size
                files.append(
                    (entry.path, entry.name, entry_size)
                )
                total_size += entry_size

        return total_size, files

    def upload_directory(self,
                         directory,
                         s3_output_key,
                         s3_output_bucket,
                         output_id,
                         request_id) -> int:
        """upload the directory of the output files to S3 object storage
         Returns directory size
         """

        directory_callback = S3UploadCallback(
                task=self,
                task_id=self.request.id.__str__(),
                output_id=output_id,
                request_id=request_id,
            ).directory_progress

        total_size, files = self.get_directory_size(directory)
        total_files = len(files)
        for number, (file_path, file_name, file_size) in \
                enumerate(files):
            partial_callback = partial(
                directory_callback,
                total_size,
                total_files,
                number)

            # s3_output_key as key can be something like : "folder1/folder2/example.m3u8"
            # file names are something like "example_480p_0003.m4s" ,...
            # this code will generate key for every file like : "folder1/folder2/example_480p_0003.m4s"
            # to prevent upload all files with the same key
            s3_folder = s3_output_key.rpartition('/')[0] + "/"

            self.s3_service.upload_file_by_path(
                key=s3_folder + file_name,
                file_path=file_path,
                bucket_name=s3_output_bucket,
                callback=partial_callback
            )

        # return the directory size
        return total_size
