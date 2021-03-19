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
    # TODO
    return dict(
        # when pass to create_playlist or generate_thumbnail task
        video_path=video_path,
        # when pass to upload_file task
        file_path=video_path)

    # TODO add prefix 'watermarked_' when save on local


