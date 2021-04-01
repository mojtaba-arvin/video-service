import ffmpeg
from abc import ABC
from pathlib import Path
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from .base import BaseStreamingTask
from .mixins import GenerateThumbnailMixin


class GenerateThumbnailTask(
        ChainCallbackMixin,
        GenerateThumbnailMixin,
        BaseStreamingTask,
        ABC
        ):

    # rewrite BaseOutputMixin.save_failed
    def save_failed(self, request_id, output_id):
        super().save_failed(request_id, output_id)
        # stop reason will only be set if there is no reason before.
        # set common reason for the task after many retries or etc.
        self.save_job_stop_reason(
            self.stop_reason.FAILED_GENERATE_THUMBNAIL,
            request_id
        )


@celery_app.task(name="generate_thumbnail",
                 base=GenerateThumbnailTask,
                 **TASK_DECORATOR_KWARGS)
def generate_thumbnail(
        self,
        *args,
        video_path: str = None,
        output_path: str = None,
        s3_output_key: str = None,
        request_id: str = None,
        output_id: str = None,
        thumbnail_time: str = "0",
        scale_width: int = -1,
        scale_height: int = -1,
        **kwargs,
        ) -> dict:
    """generate a thumbnails from the input video
    
    Args:
        self:
        *args:
        video_path:
        output_path:
        s3_output_key:
        request_id:
        output_id:
        thumbnail_time:
            Specific time of video as string.
            Defaults to is '0'.
            Some examples: '00:00:14.435','55','0.2','200ms','200000us','12:03:45' or '23.189'
            See http://ffmpeg.org/ffmpeg-utils.html#toc-Time-duration
        scale_width:
            Defaults to -1 ,The -1 will tell ffmpeg to
            automatically choose the correct height in relation to
            the provided width to preserve the aspect ratio
        scale_height:
            Defaults to -1
        **kwargs:
            some unused parameters from previous tasks that set by __call__
    Returns:

    """

    self.check_generate_thumbnail_requirements(
        request_id=request_id,
        output_id=output_id,
        video_path=video_path,
        output_path=output_path,
        s3_output_key=s3_output_key)

    if self.is_forced_to_stop(request_id):
        raise self.raise_revoke(request_id)
    if self.is_output_forced_to_stop(request_id, output_id):
        raise self.raise_revoke_output(request_id, output_id)

    # save primary status using request_id
    self.save_primary_status(
        self.primary_status.OUTPUTS_PROGRESSING,
        request_id)

    # save output status using output_id and request_id
    self.save_output_status(
        self.output_status.PREPARATION_PROCESSING,
        output_id,
        request_id)

    # get output directory and set output_path if is None
    output_path, directory = self.ensure_set_output_location(
       request_id,
       output_id,
       output_path=output_path,
       s3_output_key=s3_output_key)

    # create directory with all parents, to prevent ffmpeg error
    Path(directory).mkdir(parents=True, exist_ok=True)

    # TODO validate thumbnail time is in video duration with
    #  ffprobe result cache, and it's positive

    # default value of int in proto is zero, map 0 to -1 as default value
    if scale_width == 0:
        scale_width = -1
    if scale_height == 0:
        scale_width = -1

    self.save_output_status(
        self.output_status.PROCESSING,
        output_id,
        request_id)

    try:
        (ffmpeg
            .input(
                video_path,
                ss=thumbnail_time)
            .filter(
                'scale',
                scale_width,
                scale_height
            )
            .output(
                output_path,
                vframes=1
            )
            .run(
                cmd=settings.FFMPEG_BIN_PATH,
                capture_stdout=True,
                capture_stderr=True,
                # Overwrite output files without asking (ffmpeg -y option)
                overwrite_output=True
            ))
    except ffmpeg.Error as e:
        raise self.retry(exc=e)

    self.save_output_status(
        self.output_status.PROCESSING_FINISHED,
        output_id,
        request_id)

    return dict(file_path=output_path)
