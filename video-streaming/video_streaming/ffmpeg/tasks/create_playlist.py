from abc import ABC
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.ffmpeg.utils import FfmpegCallback
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from .base import BaseStreamingTask
from .mixins import CreatePlaylistMixin, BaseOutputMixin


class CreatePlaylistTask(
        ChainCallbackMixin,
        CreatePlaylistMixin,
        BaseOutputMixin,
        BaseStreamingTask,
        ABC
        ):
    pass


@celery_app.task(name="create_playlist",
                 base=CreatePlaylistTask,
                 **TASK_DECORATOR_KWARGS)
def create_playlist(self,
                    *args,
                    input_path: str = None,
                    output_path: str = None,
                    s3_output_key: str = None,
                    fragmented: bool = None,
                    encode_format: str = None,
                    video_codec: str = None,
                    audio_codec: str = None,
                    quality_names: list[str] = None,
                    custom_qualities: list[dict] = None,
                    async_run: bool = None,
                    request_id: str = None,
                    output_number: int = None,
                    is_hls: bool = None,
                    delete_inputs: bool = None) -> dict:
    """create an playlist ( HLS or DASH )

       required parameters:
         - input_path
         - output_path or s3_output_key
         - encode_format
    """

    self._initial_params()

    # save primary status using request_id
    self.save_primary_status(self.primary_status.OUTPUTS_PROGRESSING)

    # save output status using output_number and request_id
    self.save_output_status(self.output_status.PREPARATION_PROCESSING)

    playlist = self.initial_protocol()

    # ensure set self.directory and self.output_path
    self.ensure_set_output_location()

    try:
        # self.output_path includes the file name
        playlist.output(
            self.output_path,
            monitor=FfmpegCallback(
                task=self,
                task_id=self.request.id.__str__()
            ).progress,
            ffmpeg_bin=settings.FFMPEG_BIN_PATH,
            async_run=self.async_run)
    except Exception as e:
        # TODO handle possible Runtime Errors
        raise e

    self.save_output_status(self.output_status.PROCESSING_FINISHED)

    # It will be used to safely delete local inputs
    # after all outputs have been processed
    self.incr_processed_outputs()

    return dict(directory=self.directory)
