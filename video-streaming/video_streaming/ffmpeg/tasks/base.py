import json
import os
import shutil
from abc import ABC
from celery import states
from celery.utils.log import get_task_logger
from video_streaming import settings
from video_streaming.cache import RedisCache
from video_streaming.core.constants.status import FailedReason
from video_streaming.core.tasks import BaseTask
from video_streaming.core.constants.cache_keys import CacheKeysTemplates
from video_streaming.core.services import S3Service
from video_streaming.core.constants import ErrorMessages, \
    PrimaryStatus, InputStatus, OutputStatus
from video_streaming.ffmpeg.constants import VideoEncodingFormats
celery_logger = get_task_logger(__name__)


class BaseStreamingTask(BaseTask, ABC):
    """Base class for video streaming tasks
    default values of tasks parameters are set in this class as attr
    """

    s3_service = S3Service()  # create s3 client
    error_messages = ErrorMessages
    logger = celery_logger
    cache = RedisCache()
    primary_status = PrimaryStatus
    input_status = InputStatus
    output_status = OutputStatus
    failed_reason = FailedReason

    TMP_DOWNLOADED_DIR: str = settings.TMP_DOWNLOADED_DIR or ""
    INPUTS_DIRECTORY_PREFIX: str = "inputs_"
    TMP_TRANSCODED_DIR: str = settings.TMP_TRANSCODED_DIR or ""
    OUTPUTS_DIRECTORY_PREFIX: str = "outputs_"

    # a unique id as gRPC request id,
    # several tasks can be points to one request_id.
    # e.g. "3b06519e-1h5c-475b-p473-1c8ao63bbe58"
    request_id: str = None

    # The S3 key of video for ffmpeg input.
    # e.g. "/foo/bar/example.mp4"
    s3_input_key: str = None

    # The S3 key to save m3u8.
    # e.g. "/foo/bar/example.m3u8"
    s3_output_key: str = None

    s3_input_bucket: str = settings.S3_DEFAULT_INPUT_BUCKET_NAME
    s3_output_bucket: str = settings.S3_DEFAULT_OUTPUT_BUCKET_NAME

    # Create the output bucket If not exist
    s3_create_bucket: bool = True

    # Check if s3_output_key is already exist, ignore the task
    s3_dont_replace: bool = True

    # Set hls segment type to fmp4 if True
    fragmented: bool = False

    # The encode format of HLS,
    # e.g "h264", "hevc" or "vp9"
    encode_format: str = VideoEncodingFormats.H264

    # The video codec format,
    # e.g "libx264", "libx265" or "libvpx-vp9"
    video_codec: str = None

    # The audio codec format, e.g "aac"
    audio_codec: str = None

    # List of quality names to generate. e.g. ["360p","480p","720p"]
    #  or [Resolutions.R_360P, Resolutions.R_480P, Resolutions.R_720P]
    quality_names: list[str] = None

    # e.g. [dict(size=[256, 144], bitrate=[97280, 65536])]
    custom_qualities: list[dict] = None

    webhook_url: str = None

    # The response of head object of input video from S3
    object_details: dict = None

    # The local input path
    input_path: str = None

    # The local output path
    output_path: str = None

    # The local output directory
    directory: str = None

    # default of async_run is False to don't call async method
    # inside the task, it can raise RuntimeError: asyncio.run()
    # cannot be called from a running event loop
    async_run: bool = False

    # output_number is using in redis key, to save progress of every output
    # also it's using to create different path for outputs
    output_number: int = None

    # input_number is using in redis key, to save progress of every input
    # also it's using to create different path for inputs
    input_number: int = None

    # HLS or MPEG-Dash
    is_hls: bool = True

    # delete local inputs after all outputs have been processed
    delete_inputs: bool = True

    # delete local outputs after all outputs have been uploaded
    delete_outputs: bool = True

    # attrs that can not be empty or whitespace string
    _NO_SPACE_STRINGS = [
        'request_id',
        's3_input_key',
        's3_output_key',
        's3_input_bucket',
        's3_output_bucket',
        'encode_format',
        'video_codec',
        'audio_codec',
        'webhook_url',
        'input_path',
        'output_path',
        'directory'
    ]

    def _initial_params(self, callback: callable = None):
        self.logger.info(
            'Executing task id {0.id},'
            ' args: {0.args!r} kwargs: {0.kwargs!r}'.
            format(self.request)
        )
        for name, value in self.request.kwargs.items():

            # mapping attr value as default value when it's not exist
            # in kwargs
            value = self.request.kwargs.get(
                name, getattr(self, name))

            # mapping attr value as default value when string
            # parameters is "" instead of None
            if name in self._NO_SPACE_STRINGS:

                # ensure value is not a whitespace string, and fill it
                # by attr value as default
                if isinstance(value, str) and value.isspace():
                    value = getattr(self, name)

            setattr(self, name, value)

        if not callable(callback):
            callback = self.check_to_force_stop

        callback()

    def save_primary_status(self, status_name):
        """
            1. save as celery task status
            2. add to celery logger
            3. save primary status on cache when request_id and
            JOB_DETAILS has been set and current status is not 'FAILED'
        """

        # save as celery task status
        # self.update_state(
        #     task_id=self.request.id,
        #     state=status_name)

        # add to celery logger
        log_message = f"primary status: {status_name}"
        if self.request_id:
            log_message += f" ,request id: {self.request_id}"
        self.logger.info(log_message)

        # save primary status on cache when request_id
        # and JOB_DETAILS has been set
        if self.request_id is None:
            # request_id has been not set
            return

        if not self.cache.get(CacheKeysTemplates.JOB_DETAILS.format(
                request_id=self.request_id)):
            # JOB_DETAILS has been not set
            return None

        # to prevent set any status after it was set to 'FAILED' or 'REVOKED'
        if self.can_set_status():
            self.cache.set(
                CacheKeysTemplates.PRIMARY_STATUS.format(
                    request_id=self.request_id),
                status_name
            )

    def can_set_status(self) -> None or bool:
        """to check current primary status of job is
         in 'FAILED' and 'REVOKED'
        """
        if self.request_id is None:
            return None
        current_status = self.cache.get(
            CacheKeysTemplates.PRIMARY_STATUS.format(
                request_id=self.request_id), decode=False)
        return current_status not in [
            self.primary_status.FAILED,
            self.primary_status.REVOKED
        ]

    def get_job_details_by_request_id(self) -> None or dict:
        if self.request_id is None:
            return None
        job_details = self.cache.get(
            CacheKeysTemplates.JOB_DETAILS.format(
                request_id=self.request_id))
        if job_details:
            return job_details

    def incr(self, key_template: str) -> None or int:
        if self.request_id is None:
            return None
        key = getattr(CacheKeysTemplates, key_template).format(
            request_id=self.request_id)
        self.cache.incr(key)
        return self.cache.get(key)

    def inputs_remover(self, directory: str = None):
        if not directory:
            directory = self.get_inputs_root_directory_by_request_id()

        # check remove directory is safe and not remove other inputs
        if directory and directory != self.INPUTS_DIRECTORY_PREFIX:
            shutil.rmtree(
                directory,
                ignore_errors=False,
                onerror=self.on_error_delete_inputs)

    def on_error_delete_inputs(self, func, path, exc_info: tuple):
        # TODO capture error and notify developer
        self.logger.error(f"error_delete_inputs : {path}")
        self.logger.error(exc_info)

    def outputs_remover(self, directory: str = None):
        if not directory:
            directory = self.get_outputs_root_directory_by_request_id()

        # check remove directory is safe and not remove other outputs
        if directory and directory != self.OUTPUTS_DIRECTORY_PREFIX:
            shutil.rmtree(
                directory,
                ignore_errors=False,
                onerror=self.on_error_delete_outputs)

    def on_error_delete_outputs(self, func, path, exc_info: tuple):
        # TODO capture error and notify developer
        self.logger.error(f"error_delete_outputs : {path}")
        self.logger.error(exc_info)

    def save_input_downloading_progress(self, total, current):
        if self.request_id is not None and \
                self.input_number is not None:
            # save progress of input downloading
            # by input_number and request_id
            self.cache.set(
                CacheKeysTemplates.INPUT_DOWNLOADING_PROGRESS.format(
                    request_id=self.request_id,
                    input_number=self.input_number
                ),
                json.dumps(dict(
                    total=total,
                    current=current
                )))

    def save_output_progress(self, total, current):
        if self.request_id is not None and \
                self.output_number is not None:
            # save progress of processing or uploading
            # by output_number and request_id
            self.cache.set(
                CacheKeysTemplates.OUTPUT_PROGRESS.format(
                    request_id=self.request_id,
                    output_number=self.output_number
                ),
                json.dumps(dict(
                    total=total,
                    current=current
                )))

    def get_inputs_root_directory_by_request_id(self) -> None or str:
        if self.request_id is None:
            return None
        return os.path.join(
            self.TMP_DOWNLOADED_DIR,
            self.INPUTS_DIRECTORY_PREFIX + str(self.request_id))

    def get_outputs_root_directory_by_request_id(self) -> None or str:
        if self.request_id is None:
            return None
        return os.path.join(
            self.TMP_TRANSCODED_DIR,
            self.OUTPUTS_DIRECTORY_PREFIX + str(self.request_id))

    def check_to_force_stop(
            self, delete_inputs=True, delete_outputs=True):
        if self.is_forced_to_stop():
            if delete_inputs:
                self.inputs_remover()
            if delete_outputs:
                self.outputs_remover()
            self.save_primary_status(self.primary_status.REVOKED)
            self.raise_revoked()

    def is_forced_to_stop(self) -> None or bool:
        if self.request_id is None:
            return None
        force_stop = self.cache.get(
            CacheKeysTemplates.FORCE_STOP_REQUEST.format(
                request_id=self.request_id))
        return force_stop

    def raise_revoked(self):
        raise self.raise_ignore(
            message=self.error_messages.TASK_WAS_FORCIBLY_STOPPED,
            state=states.REVOKED
        )

    def save_job_failed_reason(self, reason):
        if not reason:
            return

        # add to celery logger
        self.logger.info(f"failed reason: {reason}")

        # save primary status on cache when request_id
        # and JOB_DETAILS has been set
        if self.request_id is None:
            # request_id has been not set
            return

        if not self.cache.get(CacheKeysTemplates.JOB_DETAILS.format(
                request_id=self.request_id)):
            # JOB_DETAILS has been not set
            return None

        # check already has failed reason, to prevent rewrite reason
        if not self.has_already_failed_reason():
            self.cache.set(
                CacheKeysTemplates.FAILED_REASON.format(
                    request_id=self.request_id),
                reason
            )

    def has_already_failed_reason(self) -> None or bool:
        """to check already has failed reason
        """
        if self.request_id is None:
            return None
        reason = self.cache.get(
            CacheKeysTemplates.FAILED_REASON.format(
                request_id=self.request_id), decode=False)
        return reason is not None
