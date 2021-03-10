import os
import ffmpeg_streaming
from celery import Task
from ffmpeg_streaming import Representation, Size, Bitrate
from video_streaming import settings
from video_streaming.core.tasks import BaseTask
from video_streaming.ffmpeg.constants import Resolutions, \
    VideoEncodingFormats
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask
from .output import BaseOutputMixin


class CreatePlaylistMixin(BaseOutputMixin):
    directory: str
    output_path: str
    s3_output_key: str
    output_number: int
    input_path: str
    encode_format: str
    video_codec: str
    audio_codec: str
    is_hls: bool
    fragmented: bool
    quality_names: list[str]
    custom_qualities: list[dict]
    request_id: str

    failed_reason: BaseStreamingTask.failed_reason
    error_messages: BaseStreamingTask.error_messages
    get_outputs_root_directory_by_request_id: BaseStreamingTask.get_outputs_root_directory_by_request_id
    save_job_failed_reason: BaseStreamingTask.save_job_failed_reason

    raise_ignore: BaseTask.raise_ignore

    retry: Task.retry

    def initial_protocol(self):
        """build HLS or MPEG ffmpeg command
        using ffmpeg_streaming package
        """

        if self.input_path is None:
            # TODO notify developer
            self.save_output_status(self.output_status.OUTPUT_FAILED)
            self.incr_failed_outputs()
            self.save_job_failed_reason(
                self.failed_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.INPUT_PATH_IS_REQUIRED)

        # checking file is exist and not empty
        try:
            if os.stat(self.input_path).st_size == 0:
                self.save_output_status(
                    self.output_status.OUTPUT_FAILED)
                self.incr_failed_outputs()
                self.save_job_failed_reason(
                    self.failed_reason.INPUT_VIDEO_SIZE_CAN_NOT_BE_ZERO)
                raise self.raise_ignore(
                    message=self.error_messages.INPUT_SIZE_CAN_NOT_BE_ZERO)
        except FileNotFoundError:
            # TODO notify developer
            self.save_output_status(
                self.output_status.OUTPUT_FAILED)
            self.incr_failed_outputs()
            self.save_job_failed_reason(
                self.failed_reason.INTERNAL_ERROR)
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

        self.add_representations(protocol)
        return protocol

    def add_representations(self, protocol):
        """create a list of Representation instances to
        add to the protocol instance
        """

        # generate default representations
        if not (self.custom_qualities or self.quality_names):
            try:
                protocol.auto_generate_representations(
                    ffprobe_bin=settings.FFPROBE_BIN_PATH)
                return
            except RuntimeError as e:
                # TODO capture error and notify developer
                raise self.retry(exc=e)
            except Exception as e:
                # FileNotFoundError: [Errno 2] No such file or directory: 'ffprobe'
                # TODO capture error and notify developer
                raise self.retry(exc=e)

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

            # when just one of them is exist,
            # force client to fill both of them
            if not size or not bitrate:
                self.save_output_status(
                    self.output_status.OUTPUT_FAILED)
                self.incr_failed_outputs()
                self.save_job_failed_reason(
                    self.failed_reason.REPRESENTATION_NEEDS_BOTH_SIZE_AND_BITRATE)
                raise self.raise_ignore(
                    message=self.error_messages.REPRESENTATION_NEEDS_BOTH_SIZE_AND_BITRATE)

            reps.append(
                Representation(
                    Size(*size),
                    Bitrate(*bitrate))
            )

        # generate representations
        protocol.representations(*reps)

    def ensure_set_output_location(self):
        """ensure set self.directory and self.output_path

           1. check requirement : self.output_path or self.s3_output_key
           2. using self.output_path to set self.directory
           3. when self.output_path is None, using self.s3_output_key
             to set self.directory and self.output_path
        """

        # check requirement
        if self.output_path is None and self.s3_output_key is None:
            # TODO notify developer
            self.save_output_status(self.output_status.OUTPUT_FAILED)
            self.incr_failed_outputs()
            self.save_job_failed_reason(
                self.failed_reason.INTERNAL_ERROR)
            raise self.raise_ignore(
                message=self.error_messages.OUTPUT_PATH_OR_S3_OUTPUT_KEY_IS_REQUIRED)

        # using self.output_path to set self.directory
        if self.output_path:
            self.directory = self.output_path.rpartition('/')[0]
            return

        # when self.output_path is None, using self.s3_output_key
        #  to set self.directory and self.output_path

        # s3_output_key : "/foo/bar/example.mpd"
        output_filename = self.s3_output_key.rpartition('/')[-1]

        self.directory = os.path.join(
            self.get_outputs_root_directory_by_request_id(),
            str(self.output_number))

        self.output_path = os.path.join(self.directory,
                                        output_filename)
