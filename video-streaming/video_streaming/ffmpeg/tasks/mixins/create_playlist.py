import os
import ffmpeg_streaming
from celery import Task
from ffmpeg_streaming import Representation, Size, Bitrate
from video_streaming import settings
from video_streaming.ffmpeg.constants import Resolutions, \
    VideoEncodingFormats
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask
from .output import BaseOutputMixin


class CreatePlaylistMixin(BaseOutputMixin):

    stop_reason: BaseStreamingTask.stop_reason
    error_messages: BaseStreamingTask.error_messages
    save_job_stop_reason: BaseStreamingTask.save_job_stop_reason

    request = Task.request
    retry: Task.retry

    def check_create_playlist_requirements(
            self,
            request_id=None,
            output_id=None,
            video_path=None,
            output_path=None,
            s3_output_key=None):

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

        if video_path is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.INPUT_PATH_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

        if output_path is None and s3_output_key is None:
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.
                OUTPUT_PATH_OR_S3_OUTPUT_KEY_IS_REQUIRED,
                request_kwargs=self.request.kwargs)

    def initial_protocol(
            self,
            input_path: str,
            output_id: str,
            request_id: str,
            encode_format: str,
            video_codec: str = None,
            audio_codec: str = None,
            is_hls: bool = None,
            fragmented: bool = None,
            quality_names: list[str] = None,
            custom_qualities: list[dict] = None):

        """build HLS or MPEG ffmpeg command
        using ffmpeg_streaming package
        """
        # checking file is exist and not empty
        try:
            if os.stat(input_path).st_size == 0:
                self.save_output_status(
                    self.output_status.OUTPUT_FAILED,
                    output_id,
                    request_id)
                self.save_job_stop_reason(
                    self.stop_reason.INPUT_VIDEO_SIZE_CAN_NOT_BE_ZERO,
                    request_id)
                raise self.raise_ignore(
                    message=self.error_messages.
                    INPUT_SIZE_CAN_NOT_BE_ZERO,
                    request_kwargs=self.request.kwargs)
        except FileNotFoundError:
            # TODO notify developer
            self.save_output_status(
                self.output_status.OUTPUT_FAILED,
                output_id,
                request_id)
            self.save_job_stop_reason(
                self.stop_reason.INTERNAL_ERROR,
                request_id)
            raise self.raise_ignore(
                message=self.error_messages.INPUT_FILE_IS_NOT_FOUND,
                request_kwargs=self.request.kwargs)

        video = ffmpeg_streaming.input(input_path)
        format_instance = VideoEncodingFormats().get_format_class(
            encode_format,
            video=video_codec,
            audio=audio_codec,
        )
        if is_hls:
            # HLS Protocol
            protocol = video.hls(format_instance)
            if fragmented:
                protocol.fragmented_mp4()
        else:
            # MPEG-Dash Protocol
            protocol = video.dash(format_instance)

        """create a list of Representation instances to
        add to the protocol instance
        """

        # generate default representations
        if not (custom_qualities or quality_names):
            try:
                protocol.auto_generate_representations(
                    ffprobe_bin=settings.FFPROBE_BIN_PATH)
                return
            except RuntimeError as e:
                # TODO capture error and notify developer
                raise self.retry(exc=e)
            except Exception as e:
                # FileNotFoundError:
                # [Errno 2] No such file or directory: 'ffprobe'
                # TODO capture error and notify developer
                raise self.retry(exc=e)

        reps = []

        # quality_names is like ["360p","480p","720p"]
        if quality_names:
            reps.extend(
                Resolutions().get_reps(quality_names)
            )

        # custom_qualities is like :
        # [dict(size=[256, 144], bitrate=[97280, 65536])]
        for quality in custom_qualities:
            size = quality.get('size', None)
            bitrate = quality.get('bitrate', None)

            # when both of them has not valid value, just continue
            if not (size or bitrate):
                continue

            # when just one of them is exist,
            # force client to fill both of them
            if not size or not bitrate:
                self.save_output_status(
                    self.output_status.OUTPUT_FAILED,
                    output_id,
                    request_id
                )
                self.save_job_stop_reason(
                    self.stop_reason.
                    REPRESENTATION_NEEDS_BOTH_SIZE_AND_BITRATE,
                    request_id
                   )
                raise self.raise_ignore(
                    message=self.error_messages.
                    REPRESENTATION_NEEDS_BOTH_SIZE_AND_BITRATE,
                    request_kwargs=self.request.kwargs)

            reps.append(
                Representation(
                    Size(*size),
                    Bitrate(*bitrate))
            )

        # generate representations
        protocol.representations(*reps)

        return protocol
