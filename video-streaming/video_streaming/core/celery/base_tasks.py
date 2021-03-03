import os
import ffmpeg_streaming
from abc import ABC
from ffmpeg_streaming import Representation, Size, Bitrate
from celery import Task, states
from celery.exceptions import Ignore
from celery.utils.log import get_task_logger
from video_streaming import settings
from video_streaming.core.services import S3Service
from video_streaming.core.constants import ErrorMessages
from video_streaming.ffmpeg.utils import S3DownloadCallback, \
    S3UploadDirectoryCallback
from video_streaming.ffmpeg.constants import Resolutions, \
    VideoEncodingFormats
from . import custom_states


__all__ = [
    'BaseCeleryTask',
    'VideoStreamingTask',
    'DownloadInputTask',
    'CreatePlaylistTask',
    'UploadDirectoryTask'
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
    custom_states = custom_states
    error_messages = ErrorMessages
    logger = get_task_logger(__name__)

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
    # e.g. ["360p","480p","720p"] or [Resolutions.360P, Resolutions.480P, Resolutions.720P]

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

    def _initial_params(self):
        self.logger.info(
            'Executing task id {0.id}, args: {0.args!r} kwargs: {0.kwargs!r}'.
            format(self.request)
        )

        self.s3_dont_replace = self.request.kwargs.get(
            's3_dont_replace', self.s3_dont_replace)

        # a unique id as gRPC request id,
        # several tasks can be points to one request_id.
        # e.g. "3b06519e-1h5c-475b-p473-1c8ao63bbe58"
        self.request_id = self.request.kwargs.get(
            'request_id', self.request_id)

        # The S3 key of video for ffmpeg input.
        # e.g. "/foo/bar/example.mp4"
        self.s3_input_key = self.request.kwargs.get(
            's3_input_key', self.s3_input_key)

        # The S3 key to save m3u8.
        # e.g. "/foo/bar/example.m3u8"
        self.s3_output_key = self.request.kwargs.get(
            's3_output_key', self.s3_output_key)

        self.s3_input_bucket = self.request.kwargs.get(
            's3_input_bucket', self.s3_input_bucket)

        self.s3_output_bucket = self.request.kwargs.get(
            's3_output_bucket', self.s3_output_bucket)

        # Create the output bucket If not exist
        self.s3_create_bucket = self.request.kwargs.get(
            's3_create_bucket', self.s3_create_bucket)

        # Check if s3_output_key is already exist, ignore the task
        self.s3_dont_replace = self.request.kwargs.get(
            's3_dont_replace', self.s3_dont_replace)

        # Set hls segment type to fmp4 if True
        self.fragmented = self.request.kwargs.get(
            'fragmented', self.fragmented)

        # The encode format of HLS,
        # e.g "h264", "hevc" or "vp9"
        self.encode_format = self.request.kwargs.get(
            'encode_format', self.encode_format)

        # The video codec format,
        # e.g "libx264", "libx265" or "libvpx-vp9"
        self.video_codec = self.request.kwargs.get(
            'video_codec', self.video_codec)

        # The audio codec format, e.g "aac"
        self.audio_codec = self.request.kwargs.get(
            'audio_codec', self.audio_codec)

        # List of quality names to genrate.
        # e.g. ["360p","480p","720p"] or [Resolutions.360P, Resolutions.480P, Resolutions.720P]
        self.quality_names = self.request.kwargs.get(
            'quality_names', self.quality_names)

        # e.g. [dict(size=[256, 144], bitrate=[97280, 65536])]
        self.custom_qualities = self.request.kwargs.get(
            'custom_qualities', self.custom_qualities)

        self.webhook_url = self.request.kwargs.get(
            'webhook_url', self.webhook_url)

        # The response of head object of input video from S3
        self.object_details = self.request.kwargs.get(
            'object_details', self.object_details)

        # The local input path
        self.input_path = self.request.kwargs.get(
            'input_path', self.input_path)

        # The local output path
        self.output_path = self.request.kwargs.get(
            'output_path', self.output_path)

        # The local output directory
        self.directory = self.request.kwargs.get(
            'directory', self.directory)

        # default of async_run is False to don't call async method
        # inside the task
        self.async_run = self.request.kwargs.get(
            'async_run', self.async_run)

    def check_input_video(self) -> dict:
        """check s3_input_key on s3_input_bucket

        1. update state of the task
        2. using self.s3_service to send head object request to S3 and get object details
        3. ignore the task, when object_details is None for 404 or 403 reason

        Returns:
          object_details
        """

        if self.s3_input_key is None:
            raise self.raise_ignore(
                message=ErrorMessages.S3_INPUT_KEY_IS_REQUIRED)

        if self.s3_input_bucket is None:
            raise self.raise_ignore(
                message=ErrorMessages.S3_INPUT_BUCKET_IS_REQUIRED)

        # update state
        if not self.request.called_directly:
            current_state = self.custom_states.CheckingInputVideoState().create()
            self.update_state(**current_state)
        self.logger.info(
            self.custom_states.CheckingInputVideoState().message)

        # check s3_input_key on s3_input_bucket
        object_details = self.s3_service.head(
            key=self.s3_input_key,
            bucket_name=self.s3_input_bucket)
        if not object_details:
            raise self.raise_ignore(
                message=ErrorMessages.INPUT_VIDEO_404_OR_403)

        return object_details

    def get_or_create_bucket(self) -> tuple:
        """_get_or_create_bucket

        1. send head bucket request to S3 to check bucket exist or no
        2. check the task s3_create_bucket boolean param to create a
           output bucket when does not exist.
           ignore the task when s3_create_bucket is False/None and the
           output bucket does not exist.
        """

        if self.s3_output_bucket is None:
            raise self.raise_ignore(
                message=ErrorMessages.S3_OUTPUT_BUCKET_IS_REQUIRED)

        created = False
        # check output bucket is exist
        bucket_details = self.s3_service.head_bucket(
            bucket_name=self.s3_output_bucket)
        if not bucket_details:
            if not self.s3_create_bucket:
                raise self.raise_ignore(
                    message=ErrorMessages.OUTPUT_BUCKET_404_OR_403)
            self._create_output_bucket()
            created = True

        return bucket_details, created

    def _create_output_bucket(self):
        """create output bucket

        1. update state of the task
        2. create bucket
        """

        # update state
        if not self.request.called_directly:
            current_state = self.custom_states.CreatingOutputBucketState().create()
            self.update_state(**current_state)
        self.logger.info(
            self.custom_states.CreatingOutputBucketState().message)

        # create s3_output_bucket
        self.s3_service.create_bucket(
            bucket_name=self.s3_output_bucket)
        # TODO handle possible errors on create_bucket

    def check_output_key(self):
        """check if s3_output_key is already exist

        this check is for prevent replace the output

        1. send head object request for key of output
        2. if the s3_dont_replace boolean param of the task is True,
           ignore the task.
        """

        if self.s3_output_key is None:
            raise self.raise_ignore(
                message=ErrorMessages.S3_OUTPUT_KEY_IS_REQUIRED)

        if self.s3_output_bucket is None:
            raise self.raise_ignore(
                message=ErrorMessages.S3_OUTPUT_BUCKET_IS_REQUIRED)

        if self.s3_service.head(
                key=self.s3_output_key,
                bucket_name=self.s3_output_bucket):
            if self.s3_dont_replace:
                raise self.raise_ignore(
                    message=ErrorMessages.OUTPUT_KEY_IS_ALREADY_EXIST)

    def download_video(self):
        """download video to local input path

        1. update state of the task
        2. get video size
        3. initial callback of downloader
        4. download video from s3 cloud
        """

        if self.input_path is None:
            raise self.raise_ignore(
                message=ErrorMessages.INPUT_PATH_IS_REQUIRED)

        if self.object_details is None:
            raise self.raise_ignore(
                message=ErrorMessages.OBJECT_DETAILS_IS_REQUIRED)

        if self.request_id is None:
            raise self.raise_ignore(
                message=ErrorMessages.REQUEST_ID_IS_REQUIRED)

        if self.s3_input_key is None:
            raise self.raise_ignore(
                message=ErrorMessages.S3_INPUT_KEY_IS_REQUIRED)

        if self.s3_input_bucket is None:
            raise self.raise_ignore(
                message=ErrorMessages.S3_INPUT_BUCKET_IS_REQUIRED)

        # update state
        if not self.request.called_directly:
            current_state = self.custom_states.PreparationVideoDownloadingState().create()
            self.update_state(**current_state)
        self.logger.info(
            self.custom_states.PreparationVideoDownloadingState().message)

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
                    message=ErrorMessages.INPUT_VIDEO_404_OR_403)

            # if it's an Exception, just raise it,
            # task decorator have autoretry_for attr for some exceptions
            if type(result, Exception):
                raise result

            # there is a serious problem
            raise self.raise_ignore()

    def initial_protocol(self, is_hls=True):
        """build HLS or MPEG ffmpeg command
        using ffmpeg_streaming package
        """

        if self.input_path is None:
            raise self.raise_ignore(
                message=ErrorMessages.INPUT_PATH_IS_REQUIRED)

        # checking file is exist and not empty
        try:
            if os.stat(self.input_path).st_size == 0:
                raise self.raise_ignore(
                    message=ErrorMessages.INPUT_SIZE_CAN_NOT_BE_ZERO)
        except FileNotFoundError:
            raise self.raise_ignore(
                message=ErrorMessages.INPUT_FILE_IS_NOT_FOUND)

        # update state
        if not self.request.called_directly:
            current_state = self.custom_states.PreparationVideoProcessingState().create()
            self.update_state(**current_state)
        self.logger.info(
            self.custom_states.PreparationVideoProcessingState().message)

        video = ffmpeg_streaming.input(self.input_path)
        format_instance = VideoEncodingFormats().get_format_class(
            self.encode_format,
            video=self.video_codec,
            audio=self.audio_codec,
        )
        if is_hls:
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
                if not (size or bitrate):
                    continue
                if not size or not bitrate:
                    raise self.raise_ignore(
                        message=ErrorMessages.REPRESENTATION_NEEDS_BOTH_SIZE_AND_BITRATE)
                reps.append(
                    Representation(Size(*size), Bitrate(*bitrate))
                )

            # generate representations
            protocol.representations(
                *reps,
                ffprobe_bin=settings.FFPROBE_BIN_PATH)

    def upload_directory(self):
        """upload the directory of the output files
         to S3 object storage
         """
        if self.directory is None:
            raise self.raise_ignore(
                message=ErrorMessages.DIRECTORY_IS_REQUIRED)

        if self.s3_output_key is None:
            raise self.raise_ignore(
                message=ErrorMessages.S3_OUTPUT_KEY_IS_REQUIRED)

        if self.s3_output_bucket is None:
            raise self.raise_ignore(
                message=ErrorMessages.S3_OUTPUT_BUCKET_IS_REQUIRED)

        # update state
        if not self.request.called_directly:
            current_state = self.custom_states.PreparationUploadOutputsState().create()
            self.update_state(**current_state)
        self.logger.info(
            self.custom_states.PreparationUploadOutputsState().message)

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
                message=ErrorMessages.OUTPUT_PATH_OR_S3_OUTPUT_KEY_IS_REQUIRED)

        if self.output_path:
            self.directory = self.output_path.rpartition('/')[0]
        else:
            # s3_output_key : "/foo/bar/example.mpd"
            folders, _, output_filename = self.s3_output_key.rpartition('/')
            
            self.directory = os.path.join(
                settings.TMP_TRANSCODED_DIR,
                self.request.id.__str__(),  # create_dash task id
                folders)

            self.output_path = os.path.join(self.directory,
                                            output_filename)

    def set_input_path(self):
        """set self.input_path"""

        if self.request_id is None:
            raise self.raise_ignore(
                message=ErrorMessages.REQUEST_ID_IS_REQUIRED)

        if self.s3_input_key is None:
            raise self.raise_ignore(
                message=ErrorMessages.S3_INPUT_KEY_IS_REQUIRED)

        # destination path of input on local machine
        self.input_path = os.path.join(
            settings.TMP_DOWNLOADED_DIR,
            self.request_id,
            self.s3_input_key)


class ChordCallbackMixin:

    def __call__(self, *args, **kwargs):
        """
        args=[[None, ["foo", False], dict(key1=value1, key2=value2), arg1, arg2]
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
        """
        args=[dict(key1=value1, key2=value2), arg1, arg2]
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
