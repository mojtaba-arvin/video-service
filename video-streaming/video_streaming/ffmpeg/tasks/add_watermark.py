import ffmpeg
from abc import ABC
from pathlib import Path
from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.core.constants import CacheKeysTemplates
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS
from .base import BaseStreamingTask
from .mixins import AddWatermarkMixin


class AddWatermarkTask(
        ChainCallbackMixin,
        AddWatermarkMixin,
        BaseStreamingTask,
        ABC
        ):

    # rewrite BaseOutputMixin.save_failed
    def save_failed(self, request_id, output_id):
        #
        super().save_failed(request_id, output_id)
        # set failed status for all watermarked outputs tasks

        job_details: dict = self.cache.get(
            CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id))
        if job_details:
            for output_id in set(job_details['watermarked_outputs_ids']):
                super().save_failed(request_id, output_id)

        # stop reason will only be set if there is no reason before.
        # set common reason for the task after many retries or etc.
        self.save_job_stop_reason(
            self.stop_reason.FAILED_ADD_WATERMARK,
            request_id
        )


@celery_app.task(name="add_watermark",
                 base=AddWatermarkTask,
                 **TASK_DECORATOR_KWARGS)
def add_watermark(
        self,
        *args,
        video_path: str = None,
        watermark_path: str = None,
        output_path: str = None,
        s3_output_key: str = None,
        request_id: str = None,
        output_id: str = None
        ) -> dict:

    # TODO add overlay params

    self.check_add_watermark_requirements(
        request_id=request_id,
        output_id=output_id,
        video_path=video_path,
        watermark_path=watermark_path,
        output_path=output_path,
        s3_output_key=s3_output_key)

    if self.is_forced_to_stop(request_id):
        raise self.raise_revoke(request_id)
    if self.is_output_forced_to_stop(request_id, output_id):
        raise self.raise_revoke_output(request_id, output_id)

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

    main = ffmpeg.input(video_path)
    watermark = ffmpeg.input(watermark_path)

    # TODO use callback
    self.save_output_status(
        self.output_status.PROCESSING,
        output_id,
        request_id)
    try:
        (ffmpeg.filter([main, watermark], 'overlay', 0, 0)
            .output(output_path)
            .run(
                cmd=settings.FFMPEG_BIN_PATH,
                capture_stdout=True,
                capture_stderr=True,
                # Overwrite output files without asking (ffmpeg -y option)
                overwrite_output=True
            )
        )
    except ffmpeg.Error as e:
        raise self.retry(exc=e)

    # process: Popen = (
    #     ffmpeg.filter([main, watermark], 'overlay', 0, 0)
    #     .output(output_path)
    #     .run_async(
    #         cmd=settings.FFMPEG_BIN_PATH,
    #         pipe_stdout=True,
    #         pipe_stderr=True,
    #         # Overwrite output files without asking (ffmpeg -y option)
    #         overwrite_output=True
    #     ))
    # callback: callable = FfmpegCallback(
    #             task=self,
    #             task_id=self.request.id.__str__(),
    #             output_id=output_id,
    #             request_id=request_id
    #         ).ffmpeg_progress
    # try:
    #     ffmpeg_process = FfmpegProcess(
    #         process=process,
    #         progress_callback=callback
    #     )
    #     print("ffmpeg_process run")
    #     ffmpeg_process.run()
    # except ffmpeg.Error as e:
    #     raise self.retry(exc=e)

    self.save_output_status(
        self.output_status.PROCESSING_FINISHED,
        output_id,
        request_id)

    return dict(
        # to use in create_playlist or generate_thumbnail task
        video_path=output_path,
        # to use in upload_file task
        file_path=output_path)
