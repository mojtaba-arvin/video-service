from abc import ABC
from celery import states
from video_streaming.celery import celery_app
from video_streaming.core.constants import CacheKeysTemplates, \
    PrimaryStatus
from video_streaming.core.tasks import ChainCallbackMixin
from video_streaming.ffmpeg.constants import TASK_DECORATOR_KWARGS, \
    InputType
from .base import BaseStreamingTask


class InputsFunnelTask(
        ChainCallbackMixin,
        BaseStreamingTask,
        ABC
        ):

    def save_failed(self, request_id):
        self.save_primary_status(
            self.primary_status.FAILED,
            request_id
        )
        # stop reason will only be set if there is no reason before.
        self.save_job_stop_reason(
            self.stop_reason.AGGREGATE_INPUTS_FAILED,
            request_id
        )

    def on_failure(self, *request_args, **request_kwargs):
        request_id = request_kwargs.get('request_id', None)
        if request_id is not None:
            self.save_failed(request_id)
        return super().on_failure(*request_args, **request_kwargs)

    def raise_ignore(self,
                     message=None,
                     state=states.FAILURE,
                     request_kwargs: dict = None):
        if request_kwargs:
            request_id = request_kwargs.get('request_id', None)
            if request_id is not None and state == states.FAILURE:
                self.save_failed(request_id)

        super().raise_ignore(
            message=message,
            state=state,
            request_kwargs=request_kwargs)


@celery_app.task(name="inputs_funnel",
                 base=InputsFunnelTask,
                 **TASK_DECORATOR_KWARGS)
def inputs_funnel(self,
                  *args,
                  request_id: str = None,
                  watermark_path: str = None,
                  video_path: str = None,
                  **kwargs
                  ) -> dict:
    """

    Args:
        self ():
        *args ():
        request_id ():
        watermark_path ():
        video_path ():
        **kwargs ():

    Returns:

    """

    if request_id is None:
        self.save_job_stop_reason(
            self.stop_reason.INTERNAL_ERROR,
            request_id)
        raise self.raise_ignore(
            message=self.error_messages.REQUEST_ID_IS_REQUIRED,
            request_kwargs=self.request.kwargs)

    if self.is_forced_to_stop(request_id):
        raise self.raise_revoke(request_id)

    job_details: dict = self.cache.get(
        CacheKeysTemplates.JOB_DETAILS.format(
            request_id=request_id))
    if not job_details:
        self.save_job_stop_reason(
            self.stop_reason.JOB_TIMEOUT,
            request_id)
        raise self.raise_ignore(
            message=self.error_messages.JOB_DETAILS_NOT_FOUND,
            request_kwargs=self.request.kwargs)
    total_inputs: int = job_details['total_inputs']

    if video_path:
        self.cache.set(
            CacheKeysTemplates.INPUT_VIDEO_PATH.format(
                request_id=request_id),
            video_path
        )
    if watermark_path:
        self.cache.set(
            CacheKeysTemplates.INPUT_WATERMARK_PATH.format(
                request_id=request_id),
            watermark_path
        )

    aggregated_inputs: int = self.incr("AGGREGATED_INPUTS", request_id)
    if aggregated_inputs != total_inputs:
        # do not stop the job, just ignore this task
        raise self.raise_ignore(
            message=self.error_messages.WAITING_FOR_AGGREGATE_INPUTS,
            state=states.REVOKED,
            request_kwargs=self.request.kwargs)

    return dict(
        video_path=self.cache.get(
            CacheKeysTemplates.INPUT_VIDEO_PATH.format(
                request_id=request_id),
            decode=False
        ),
        watermark_path=self.cache.get(
            CacheKeysTemplates.INPUT_WATERMARK_PATH.format(
                request_id=request_id),
            decode=False
        )
    )



