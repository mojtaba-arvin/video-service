import json
import os
import shutil
import urllib3
import ffmpeg_streaming
from abc import ABC
from ffmpeg_streaming import Representation, Size, Bitrate, FFProbe
from celery import Task, states
from celery.exceptions import Ignore
from celery.utils.log import get_task_logger
from video_streaming import settings
from video_streaming.cache import RedisCache
from video_streaming.core.constants.cache_keys import CacheKeysTemplates
from video_streaming.core.services import S3Service
from video_streaming.core.constants import ErrorMessages, \
    PrimaryStatus, InputStatus, OutputStatus
from video_streaming.ffmpeg.utils import S3DownloadCallback, \
    S3UploadDirectoryCallback
from video_streaming.ffmpeg.constants import Resolutions, \
    VideoEncodingFormats
celery_logger = get_task_logger(__name__)

__all__ = [
    'BaseCeleryTask',
    'VideoStreamingTask',
    'DownloadInputTask',
    'CreatePlaylistTask',
    'UploadDirectoryTask',
    'CallWebhookTask',
    'AnalyzeInputTask'
]


class BaseCeleryTask(Task, ABC):

    def raise_ignore(self, message=None):
        try:
            # to trigger the task_failure signal
            raise Exception
        except Exception:
            if not self.request.called_directly:
                update_kwargs = dict(state=states.FAILURE)
                if message is not None:
                    update_kwargs['meta'] = dict(
                        exc_type='Exception',
                        exc_message=message)
                self.update_state(
                    **update_kwargs)
            raise Ignore()


class VideoStreamingTask(BaseCeleryTask, ABC):

    s3_service = S3Service()  # create s3 client
    error_messages = ErrorMessages
    logger = celery_logger
    cache = RedisCache()
    primary_status = PrimaryStatus
    input_status = InputStatus
    output_status = OutputStatus

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

    # List of quality names to genrate.
    # e.g. ["360p","480p","720p"] or [Resolutions.R_360P, Resolutions.R_480P, Resolutions.R_720P]

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

    # delete local inputs files after after all outputs have been processed
    delete_inputs: bool = True

    # delete local outputs files after after all outputs have been uploaded
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

    def _initial_params(self):
        self.logger.info(
            'Executing task id {0.id}, args: {0.args!r} kwargs: {0.kwargs!r}'.
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

    def save_primary_status(self, status_name):
        """
            1. save as celery task status
            2. add to celery logger
            3. save primary status on cache when request_id
            and JOB_DETAILS has been set
        """

        # save as celery task status
        self.update_state(
            task_id=self.request.id,
            state=status_name)

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

        self.cache.set(
            CacheKeysTemplates.PRIMARY_STATUS.format(
                request_id=self.request_id),
            status_name
        )

    def save_input_status(self, status_name):
        # add input status name as message to logger
        log_message = f"input status: {status_name}"
        if self.request_id:
            log_message += f" ,request id: {self.request_id}"
        if self.input_number:
            log_message += f" ,input number: {self.input_number}"
        self.logger.info(log_message)

        # save input status when request_id , input_number
        # and JOB_DETAILS has been set

        if self.request_id is None:
            # request_id has been not set
            return

        if self.input_number is None:
            # input_number has been not set
            return None

        if not self.cache.get(CacheKeysTemplates.JOB_DETAILS.format(
                request_id=self.request_id)):
            # JOB_DETAILS has been not set
            return None

        self.cache.set(
            CacheKeysTemplates.INPUT_STATUS.format(
                request_id=self.request_id,
                input_number=self.input_number),
            status_name
        )
        # check to delete progress data of downloading
        if status_name == self.input_status.DOWNLOADING_FINISHED:
            self.cache.delete(
                CacheKeysTemplates.INPUT_DOWNLOADING_PROGRESS.format(
                    request_id=self.request_id,
                    input_number=self.input_number
                ))

    def save_output_status(self, status_name):
        # add output status name as message to logger
        log_message = f"output status: {status_name}"
        if self.request_id:
            log_message += f" ,request id: {self.request_id}"
        if self.output_number:
            log_message += f" ,output number: {self.output_number}"
        self.logger.info(log_message)

        # save output status when request_id , input_number
        # and JOB_DETAILS has been set

        if self.request_id is None:
            # request_id has been not set
            return

        if self.output_number is None:
            # input_number has been not set
            return None

        if not self.cache.get(CacheKeysTemplates.JOB_DETAILS.format(
                request_id=self.request_id)):
            # JOB_DETAILS has been not set
            return None

        self.cache.set(
            CacheKeysTemplates.OUTPUT_STATUS.format(
                request_id=self.request_id,
                output_number=self.output_number),
            status_name
        )

        # check to delete unnecessary data
        if status_name in [
                self.output_status.PROCESSING_FINISHED,
                self.output_status.UPLOADING_FINISHED]:
            self.cache.delete(
                CacheKeysTemplates.OUTPUT_PROGRESS.format(
                    request_id=self.request_id,
                    output_number=self.output_number
                ))

    def _get_job_details_by_request_id(self) -> None or dict:
        if self.request_id is None:
            return None
        job_details = self.cache.get(
            CacheKeysTemplates.JOB_DETAILS.format(
                request_id=self.request_id))
        if job_details:
            return job_details

    def _incr(self, key_template: str) -> None or int:
        if self.request_id is None:
            return None
        key = getattr(CacheKeysTemplates, key_template).format(
            request_id=self.request_id)
        self.cache.incr(key)
        return self.cache.get(key)

    def incr_passed_checks(self):
        job_details = self._get_job_details_by_request_id()
        if job_details:
            passed_checks = self._incr("PASSED_CHECKS")
            if passed_checks == job_details['total_checks']:
                self.save_primary_status(
                    self.primary_status.CHECKS_FINISHED
                )

    def incr_ready_inputs(self):
        job_details = self._get_job_details_by_request_id()
        if job_details:
            ready_inputs = self._incr("READY_INPUTS")
            if ready_inputs == job_details['total_inputs']:
                self.save_primary_status(
                    self.primary_status.ALL_INPUTS_DOWNLOADED
                )

    def _delete_outputs(self, directory: str = None):
        if not directory:
            directory = self._get_outputs_root_directory_by_request_id()

        # check remove directory is safe and not remove other outputs
        if directory and directory != self.OUTPUTS_DIRECTORY_PREFIX:
            shutil.rmtree(
                directory,
                ignore_errors=False,
                onerror=self._on_error_delete_outputs)

    def _on_error_delete_outputs(self, func, path, exc_info: tuple):
        # TODO capture error and notify developer
        self.logger.error(f"error_delete_outputs : {path}")

    def incr_ready_outputs(self):
        job_details = self._get_job_details_by_request_id()
        if job_details:
            ready_outputs = self._incr("READY_OUTPUTS")
            if ready_outputs == job_details['total_outputs']:
                self.save_primary_status(
                    self.primary_status.ALL_OUTPUTS_ARE_READY
                )
                if self.delete_outputs:
                    self._delete_outputs()

    def _delete_inputs(self, directory: str = None):
        if not directory:
            directory = self._get_inputs_root_directory_by_request_id()

        # check remove directory is safe and not remove other inputs
        if directory and directory != self.INPUTS_DIRECTORY_PREFIX:
            shutil.rmtree(
                directory,
                ignore_errors=False,
                onerror=self._on_error_delete_inputs)

    def _on_error_delete_inputs(self, func, path, exc_info: tuple):
        # TODO capture error and notify developer
        self.logger.error(f"error_delete_inputs : {path}")

    def incr_processed_outputs(self):
        job_details = self._get_job_details_by_request_id()
        if job_details:
            processed_outputs = self._incr("PROCESSED_OUTPUTS")
            if self.delete_inputs and processed_outputs == job_details['total_outputs']:
                self._delete_inputs()

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

    def check_input_video(self) -> dict:
        """check s3_input_key on s3_input_bucket

        1. using self.s3_service to send head object request to S3
            and get object details
        2. ignore the task, when object_details is None for 404 or
            403 reason

        Returns:
          object_details
        """

        if self.s3_input_key is None:
            raise self.raise_ignore(
                message=self.error_messages.S3_INPUT_KEY_IS_REQUIRED)

        if self.s3_input_bucket is None:
            raise self.raise_ignore(
                message=self.error_messages.S3_INPUT_BUCKET_IS_REQUIRED)

        # check s3_input_key on s3_input_bucket
        object_details = self.s3_service.head(
            key=self.s3_input_key,
            bucket_name=self.s3_input_bucket)
        if not object_details:
            raise self.raise_ignore(
                message=self.error_messages.INPUT_VIDEO_404_OR_403)

        return object_details

    def ensure_bucket_exist(self):
        """ensure bucket exist

        1. send head bucket request to S3 to check bucket exist or no
        2. check the task s3_create_bucket boolean param to create a
           output bucket when does not exist.
           ignore the task when s3_create_bucket is False/None and the
           output bucket does not exist.
        """

        if self.s3_output_bucket is None:
            raise self.raise_ignore(
                message=self.error_messages.S3_OUTPUT_BUCKET_IS_REQUIRED)

        # check output bucket is exist
        bucket_details = self.s3_service.head_bucket(
            bucket_name=self.s3_output_bucket)
        if not bucket_details:
            if not self.s3_create_bucket:
                raise self.raise_ignore(
                    message=self.error_messages.OUTPUT_BUCKET_404_OR_403)
            self._create_output_bucket()

    def _create_output_bucket(self):
        """create output bucket"""

        try:
            # create s3_output_bucket
            self.s3_service.create_bucket(
                bucket_name=self.s3_output_bucket)
        except self.s3_service.exceptions.BucketExist:
            pass

    def check_output_key(self):
        """check if s3_output_key is already exist

        this check is for prevent replace the output

        1. send head object request for key of output
        2. if the s3_dont_replace boolean param of the task is True,
           ignore the task.
        """

        if self.s3_output_key is None:
            raise self.raise_ignore(
                message=self.error_messages.S3_OUTPUT_KEY_IS_REQUIRED)

        if self.s3_output_bucket is None:
            raise self.raise_ignore(
                message=self.error_messages.S3_OUTPUT_BUCKET_IS_REQUIRED)

        if self.s3_service.head(
                key=self.s3_output_key,
                bucket_name=self.s3_output_bucket):
            if self.s3_dont_replace:
                raise self.raise_ignore(
                    message=self.error_messages.OUTPUT_KEY_IS_ALREADY_EXIST)

    def download_video(self):
        """download video to local input path

        1. get video size
        2. initial callback of downloader
        3. download video from s3 cloud
        """

        if self.input_path is None:
            raise self.raise_ignore(
                message=self.error_messages.INPUT_PATH_IS_REQUIRED)

        if self.object_details is None:
            raise self.raise_ignore(
                message=self.error_messages.OBJECT_DETAILS_IS_REQUIRED)

        if self.request_id is None:
            raise self.raise_ignore(
                message=self.error_messages.REQUEST_ID_IS_REQUIRED)

        if self.s3_input_key is None:
            raise self.raise_ignore(
                message=self.error_messages.S3_INPUT_KEY_IS_REQUIRED)

        if self.s3_input_bucket is None:
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
        if result != self.input_path:

            # the _exception_handler of S3Service returns None
            # when it's 404 or 403
            if result is None:
                raise self.raise_ignore(
                    message=self.error_messages.INPUT_VIDEO_404_OR_403)

            # if it's an Exception, just raise it,
            # task decorator have autoretry_for attr for some exceptions
            raise result

    def analyze_input(self):
        if self.input_path is None:
            raise self.raise_ignore(
                message=self.error_messages.INPUT_PATH_IS_REQUIRED)

        if self.request_id is None:
            raise self.raise_ignore(
                message=self.error_messages.REQUEST_ID_IS_REQUIRED)

        ffprobe = FFProbe(self.input_path)

        """
        exmaples :
            ffprobe.format()
            ffprobe.all()
            ffprobe.streams().audio().get('bit_rate', 0)
        """
        self.cache.set(
            CacheKeysTemplates.INPUT_FFPROBE_DATA.format(
                request_id=self.request_id,
                input_number=self.input_number
            ),
            ffprobe.out)
        return ffprobe

    def initial_protocol(self):
        """build HLS or MPEG ffmpeg command
        using ffmpeg_streaming package
        """

        if self.input_path is None:
            raise self.raise_ignore(
                message=self.error_messages.INPUT_PATH_IS_REQUIRED)

        # checking file is exist and not empty
        try:
            if os.stat(self.input_path).st_size == 0:
                raise self.raise_ignore(
                    message=self.error_messages.INPUT_SIZE_CAN_NOT_BE_ZERO)
        except FileNotFoundError:
            raise self.raise_ignore(
                message=self.error_messages.INPUT_FILE_IS_NOT_FOUND)

        video = ffmpeg_streaming.input(self.input_path)
        format_instance = VideoEncodingFormats().get_format_class(
            self.encode_format,
            video=self.video_codec,
            audio=self.audio_codec,
        )
        if self.is_hls:
            # HLS Protocol
            protocol = video.hls(format_instance)
            if self.fragmented:
                protocol.fragmented_mp4()
        else:
            # MPEG-Dash Protocol
            protocol = video.dash(format_instance)

        self._add_representations(protocol)
        return protocol

    def _add_representations(self, protocol):
        """create a list of Representation instances to
        add to the protocol instance
        """

        if not (self.custom_qualities or self.quality_names):
            # generate default representations
            protocol.auto_generate_representations(
                ffprobe_bin=settings.FFPROBE_BIN_PATH)
        else:
            reps = []

            # quality_names is like ["360p","480p","720p"]
            if self.quality_names:
                reps.extend(
                    Resolutions().get_reps(self.quality_names)
                )

            # custom_qualities is like :
            # [dict(size=[256, 144], bitrate=[97280, 65536])]
            for quality in self.custom_qualities:
                size = quality.get('size', None)
                bitrate = quality.get('bitrate', None)

                # when both of them has not valid value, just continue
                if not (size or bitrate):
                    continue

                # when just one of them is exist, force client to fill both of them
                if not size or not bitrate:
                    raise self.raise_ignore(
                        message=self.error_messages.REPRESENTATION_NEEDS_BOTH_SIZE_AND_BITRATE)

                reps.append(
                    Representation(
                        Size(*size),
                        Bitrate(*bitrate))
                )

            # generate representations
            protocol.representations(*reps)

    def upload_directory(self):
        """upload the directory of the output files
         to S3 object storage
         """
        if self.directory is None:
            raise self.raise_ignore(
                message=self.error_messages.DIRECTORY_IS_REQUIRED)

        if self.s3_output_key is None:
            raise self.raise_ignore(
                message=self.error_messages.S3_OUTPUT_KEY_IS_REQUIRED)

        if self.s3_output_bucket is None:
            raise self.raise_ignore(
                message=self.error_messages.S3_OUTPUT_BUCKET_IS_REQUIRED)

        self.s3_service.upload_directory(
            self.s3_output_key,
            self.directory,
            bucket_name=self.s3_output_bucket,
            directory_callback=S3UploadDirectoryCallback(
                task=self,
                task_id=self.request.id.__str__()
            ).progress
        )

    def ensure_set_output_location(self):
        """ensure set self.directory and self.output_path

           1. check requirement : self.output_path or self.s3_output_key
           2. using self.output_path to set self.directory
           3. when self.output_path is None, using self.s3_output_key
             to set self.directory and self.output_path
        """
        if self.output_path is None and self.s3_output_key is None:
            raise self.raise_ignore(
                message=self.error_messages.OUTPUT_PATH_OR_S3_OUTPUT_KEY_IS_REQUIRED)

        if self.output_path:
            self.directory = self.output_path.rpartition('/')[0]
        else:
            # s3_output_key : "/foo/bar/example.mpd"
            output_filename = self.s3_output_key.rpartition('/')[-1]

            self.directory = os.path.join(
                self._get_outputs_root_directory_by_request_id(),
                str(self.output_number))

            self.output_path = os.path.join(self.directory,
                                            output_filename)

    def _get_outputs_root_directory_by_request_id(self) -> None or str:
        if self.request_id is None:
            return None
        return os.path.join(
            self.TMP_TRANSCODED_DIR,
            self.OUTPUTS_DIRECTORY_PREFIX + str(self.request_id))

    def _get_inputs_root_directory_by_request_id(self) -> None or str:
        if self.request_id is None:
            return None
        return os.path.join(
            self.TMP_DOWNLOADED_DIR,
            self.INPUTS_DIRECTORY_PREFIX + str(self.request_id))

    def set_input_path(self):
        """set self.input_path"""

        if self.request_id is None:
            raise self.raise_ignore(
                message=self.error_messages.REQUEST_ID_IS_REQUIRED)

        if self.s3_input_key is None:
            raise self.raise_ignore(
                message=self.error_messages.S3_INPUT_KEY_IS_REQUIRED)

        input_filename = self.s3_input_key.rpartition('/')[-1]

        # destination path of input on local machine
        self.input_path = os.path.join(
            self._get_inputs_root_directory_by_request_id(),
            str(self.input_number),
            input_filename)

    def call_webhook(self, url: str = None) -> None or True:
        if not url or self.request_id:
            return None
        job_details = self._get_job_details_by_request_id()
        if job_details is None:
            return None

        http_method = 'POST'
        headers = {'Content-Type': 'application/json'}
        encoded_body = json.dumps({
            "request_id": self.request_id,
            "reference_id": job_details["reference_id"]
        })
        http_pool = urllib3.PoolManager()
        try:
            http_response = http_pool.request(
                http_method,
                url,
                headers=headers,
                body=encoded_body)
        except Exception as e:
            # urllib3.exceptions.NewConnectionError
            raise self.retry(exc=e)

        self.logger.info(
            f'http status: {http_response.status}, url: {url}')

        # check the HTTP status code to raise ignore/retry
        is_delivered = self._check_http_status(http_response)
        return is_delivered

    def _check_http_status(self, http_response) -> True or None:
        if 200 <= http_response.status < 300:
            # success
            return True
        if 300 <= http_response.status < 400:
            raise self.raise_ignore(
                message=self.error_messages.WEBHOOK_URL_MUST_NOT_BE_REDIRECTED
            )
        if 400 <= http_response.status < 500:
            raise self.raise_ignore(
                message=self.error_messages.WEBHOOK_HTTP_FAILED.format(
                    status=http_response.status,
                    reason=http_response.reason,
                    request_id=self.request_id
                )
            )
        if 500 <= http_response.status:
            raise self.retry(
                exc=Exception(
                    self.error_messages.WEBHOOK_HTTP_FAILED.format(
                        status=http_response.status,
                        reason=http_response.reason,
                        request_id=self.request_id
                    )))


class ChordCallbackMixin:

    def __call__(self, *args, **kwargs):
        print(self.request.args)
        """
        args=[[None, dict(key1=value1), dict(key2=value2), None,...], arg1, arg2]
        kwargs=dict(key3=value3, key4=value4)
        ->
        args=[arg1 ,arg2]
        kwargs=dict(key1=value1, key2=value2, key3=value3, key4=value4)
        """

        if self.request.args and len(self.request.args):
            for result in self.request.args.pop(0):
                if isinstance(result, dict):
                    self.request.kwargs.update(result)

        return super().__call__(*self.request.args, **self.request.kwargs)


class ChainCallbackMixin:

    def __call__(self, *args, **kwargs):
        print(self.request.args)
        """
        args=[None, dict(key1=value1, key2=value2), None, arg1, arg2]
        kwargs=dict(key3=value3, key4=value4)
        ->
        args=[arg1 ,arg2]
        kwargs=dict(key1=value1, key2=value2, key3=value3, key4=value4)
        """
        if self.request.args and len(self.request.args):
            result = self.request.args.pop(0)
            if isinstance(result, dict):
                self.request.kwargs.update(result)
        return super().__call__(*self.request.args, **self.request.kwargs)


class DownloadInputTask(
        ChordCallbackMixin,
        VideoStreamingTask,
        ABC):
    pass


class AnalyzeInputTask(
        ChainCallbackMixin,
        VideoStreamingTask,
        ABC):
    pass


class CreatePlaylistTask(
        ChainCallbackMixin,
        VideoStreamingTask,
        ABC):
    pass


class UploadDirectoryTask(
        ChainCallbackMixin,
        VideoStreamingTask,
        ABC):
    pass


class CallWebhookTask(
        VideoStreamingTask,
        ABC):
    pass
