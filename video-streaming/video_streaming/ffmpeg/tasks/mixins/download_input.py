import os
from celery import Task
from video_streaming.core.tasks import BaseTask
from video_streaming.core.services import S3Service
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask
from video_streaming.ffmpeg.utils import S3DownloadCallback


class DownloadInputMixin(object):
    request: Task.request

    request_id: str  # grpc request tracking id
    input_number: int
    s3_input_key: str
    s3_input_bucket: str
    input_path: str
    object_details: str

    failed_reason: BaseStreamingTask.failed_reason
    error_messages: BaseStreamingTask.error_messages
    s3_service: BaseStreamingTask.s3_service
    get_inputs_root_directory_by_request_id: BaseStreamingTask.get_inputs_root_directory_by_request_id
    save_job_failed_reason: BaseStreamingTask.save_job_failed_reason

    raise_ignore: BaseTask.raise_ignore

    def set_input_path(self):
        """set self.input_path"""

        if self.request_id is None:
            self.save_job_failed_reason(
                self.failed_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.REQUEST_ID_IS_REQUIRED)

        if self.s3_input_key is None:
            self.save_job_failed_reason(
                self.failed_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.S3_INPUT_KEY_IS_REQUIRED)

        input_filename = self.s3_input_key.rpartition('/')[-1]

        # destination path of input on local machine
        self.input_path = os.path.join(
            self.get_inputs_root_directory_by_request_id(),
            str(self.input_number),
            input_filename)

    def download_video(self) -> bool:
        """download video to local input path

        1. get video size
        2. initial callback of downloader
        3. download video from s3 cloud

        returns False the input video is 404 or 403
        """

        if self.input_path is None:
            self.save_job_failed_reason(
                self.failed_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.INPUT_PATH_IS_REQUIRED)

        if self.object_details is None:
            self.save_job_failed_reason(
                self.failed_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.OBJECT_DETAILS_IS_REQUIRED)

        if self.request_id is None:
            self.save_job_failed_reason(
                self.failed_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.REQUEST_ID_IS_REQUIRED)

        if self.s3_input_key is None:
            self.save_job_failed_reason(
                self.failed_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.S3_INPUT_KEY_IS_REQUIRED)

        if self.s3_input_bucket is None:
            self.save_job_failed_reason(
                self.failed_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.S3_INPUT_BUCKET_IS_REQUIRED)

        # Size of the body in bytes.
        object_size = S3Service.get_object_size(self.object_details)

        # Initial callback
        download_callback = S3DownloadCallback(
            object_size,
            task=self,
            task_id=self.request.id.__str__()
        ).progress

        # Download the input video to the local input_path
        result = self.s3_service.download(
            self.s3_input_key,
            destination_path=self.input_path,
            bucket_name=self.s3_input_bucket,
            callback=download_callback
        )

        # check result same as destination_path
        # the _exception_handler of S3Service returns None
        # when it's 404 or 403
        return result == self.input_path
