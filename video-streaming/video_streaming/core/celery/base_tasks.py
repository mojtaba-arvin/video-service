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
    'VideoStreamingTask'
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

    # TODO when task executed :
    # 1. decrease used count to remove input file when reach 0
    # 2. decrease tasks counts to call webhook when reach 0
    # 3. remove outputs file

    def retry(self, *args, **kwargs):
        # TODO get the task last state by
        #  self.AsyncResult(self.request.id).state to manage retry
        super().retry(*args, **kwargs)

    def check_input_video(self) -> dict:
        """check s3_input_key on s3_input_bucket

        1. update state of the task
        2. using self.s3_service to send head object request to S3 and get object details
        3. ignore the task, when object_details is None for 404 or 403 reason

        Returns:
          object_details
        """

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

    def check_output_bucket(self):
        """check output bucket

        1. send head bucket request to S3 to check bucket exist or no
        2. check the task s3_create_bucket boolean param to create a
           output bucket when does not exist.
           ignore the task when s3_create_bucket is False/None and the
           output bucket does not exist.
        """

        # check output bucket is exist
        if not self.s3_service.head_bucket(
                bucket_name=self.s3_output_bucket):
            if not self.s3_create_bucket:
                raise self.raise_ignore(
                    message=ErrorMessages.OUTPUT_BUCKET_404_OR_403)

            self._create_output_bucket()

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
        if self.s3_service.head(
                key=self.s3_output_key,
                bucket_name=self.s3_output_bucket):
            if self.s3_dont_replace:
                raise self.raise_ignore(
                    message=ErrorMessages.OUTPUT_KEY_IS_ALREADY_EXIST)

    def download_video(self, s3_input_key, object_details, local_input_path):
        """download video to local input path

        1. update state of the task
        2. get video size
        3. initial callback of downloader
        4. download video from s3 cloud
        """

        # update state
        if not self.request.called_directly:
            current_state = self.custom_states.PreparationVideoDownloadingState().create()
            self.update_state(**current_state)
        self.logger.info(
            self.custom_states.PreparationVideoDownloadingState().message)

        # Size of the body in bytes.
        object_size = S3Service.get_object_size(object_details)

        # Initial callback
        download_callback = S3DownloadCallback(
            object_size,
            task=self,
            task_id=self.request.id.__str__()
        ).progress

        # Download the input video to the local_input_path
        self.s3_service.download(
            s3_input_key,
            destination_path=local_input_path,
            bucket_name=self.s3_input_bucket,
            callback=download_callback
        )

    def initial_hls(self, local_input_path):
        """build ffmpeg command using ffmpeg_streaming package"""

        # update state
        if not self.request.called_directly:
            current_state = self.custom_states.PreparationVideoProcessingState().create()
            self.update_state(**current_state)
        self.logger.info(
            self.custom_states.PreparationVideoProcessingState().message)

        video = ffmpeg_streaming.input(local_input_path)
        format_instance = VideoEncodingFormats().get_format_class(
            self.encode_format,
            video=self.video_codec,
            audio=self.audio_codec,
        )
        hls = video.hls(format_instance)
        self._add_representations(hls)
        if self.fragmented:
            hls.fragmented_mp4()
        return hls

    def _add_representations(self, protocol):
        """create a list of Representation instances to add to the protocol instance"""

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

    def upload_directory(self, output_directory):
        """upload the directory of the output files to S3 object storage"""

        # update state
        if not self.request.called_directly:
            current_state = self.custom_states.PreparationUploadOutputsState().create()
            self.update_state(**current_state)
        self.logger.info(
            self.custom_states.PreparationUploadOutputsState().message)

        self.s3_service.upload_directory(
            self.s3_output_key,
            output_directory,
            bucket_name=self.s3_output_bucket,
            directory_callback=S3UploadDirectoryCallback(
                task=self,
                task_id=self.request.id.__str__()
            ).progress
        )
