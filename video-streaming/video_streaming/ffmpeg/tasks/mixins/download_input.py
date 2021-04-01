import os
from celery import Task
from video_streaming.core.services import S3Service
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask
from video_streaming.ffmpeg.utils import S3DownloadCallback
from video_streaming.ffmpeg.constants import InputType
from .input import BaseInputMixin


class DownloadInputMixin(BaseInputMixin):

    stop_reason: BaseStreamingTask.stop_reason
    error_messages: BaseStreamingTask.error_messages
    s3_service: BaseStreamingTask.s3_service
    get_inputs_root_directory: BaseStreamingTask.get_inputs_root_directory
    save_job_stop_reason: BaseStreamingTask.save_job_stop_reason

    request: Task.request

    def check_download_requirements(self,
                                    request_id=None,
                                    input_number=None,
                                    video_details=None,
                                    watermark_details=None,
                                    input_type=None,
                                    s3_input_key=None,
                                    s3_input_bucket=None):

        if request_id is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.REQUEST_ID_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if input_number is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.INPUT_NUMBER_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if s3_input_key is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.S3_INPUT_KEY_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if s3_input_bucket is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.S3_INPUT_BUCKET_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if input_type is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.INPUT_TYPE_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if (input_type == InputType.WATERMARK_INPUT
            and watermark_details is None
            ) or (
                input_type == InputType.VIDEO_INPUT
                and video_details is None):
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.OBJECT_DETAILS_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

    def generate_input_path(self,
                            s3_input_key,
                            request_id,
                            input_number):
        """set self.input_path"""

        input_filename = s3_input_key.rpartition('/')[-1]

        # destination path of input on local machine
        input_path = os.path.join(
            self.get_inputs_root_directory(request_id),
            str(input_number),
            input_filename)

        return input_path

    def download_video(self,
                       input_path,
                       object_details,
                       s3_input_key,
                       s3_input_bucket,
                       input_number,
                       request_id
                       ) -> bool:
        """download video to local input path

        1. get video size
        2. initial callback of downloader
        3. download video from s3 cloud

        returns False when the input video is 404 or 403
        """

        # Size of the body in bytes.
        object_size = S3Service.get_object_size(object_details)
        if object_size is None:
            self.save_input_status(
                self.input_status.INPUT_FAILED,
                input_number,
                request_id)
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.OBJECT_DETAILS_IS_INVALID,
                request_kwargs=self.request.kwargs)

        if object_size == 0:
            # this condition has been checked before, at checks tasks
            self.save_input_status(
                self.input_status.INPUT_FAILED,
                input_number,
                request_id)
            self.save_job_stop_reason(
                self.stop_reason.INPUT_VIDEO_SIZE_CAN_NOT_BE_ZERO,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.INPUT_SIZE_CAN_NOT_BE_ZERO,
                request_kwargs=self.request.kwargs)

        # Initial callback
        download_callback = S3DownloadCallback(
            object_size,
            task=self,
            task_id=self.request.id.__str__(),
            input_number=input_number,
            request_id=request_id
        ).progress

        # Download the input video to the local input_path
        result = self.s3_service.download(
            s3_input_key,
            destination_path=input_path,
            bucket_name=s3_input_bucket,
            callback=download_callback
        )

        # check result same as destination_path
        # the _exception_handler of S3Service returns None
        # when it's 404 or 403
        return result == input_path
