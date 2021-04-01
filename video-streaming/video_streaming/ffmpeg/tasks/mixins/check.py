from celery import states
from video_streaming.core.constants import CacheKeysTemplates
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask


class BaseCheckMixin(object):
    cache: BaseStreamingTask.cache
    primary_status: BaseStreamingTask.primary_status

    incr: BaseStreamingTask.incr
    save_primary_status: BaseStreamingTask.save_primary_status

    def save_failed(self, request_id):
        """
        please rewrite this method to add stop_reason
        """
        self.save_primary_status(
            self.primary_status.FAILED,
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

    def incr_passed_checks(self, request_id):
        job_details: dict = self.cache.get(
            CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id))
        if job_details:
            passed_checks = self.incr("PASSED_CHECKS", request_id)
            if passed_checks == job_details['total_checks']:
                self.save_primary_status(
                    self.primary_status.CHECKS_FINISHED,
                    request_id
                )
