from celery import states
from video_streaming.core.constants.cache_keys import CacheKeysTemplates
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask


class BaseInputMixin(object):

    primary_status: BaseStreamingTask.primary_status
    input_status: BaseStreamingTask.input_status
    cache: BaseStreamingTask.cache
    logger: BaseStreamingTask.logger

    incr: BaseStreamingTask.incr
    save_primary_status: BaseStreamingTask.save_primary_status

    def save_failed(self, request_id, input_number):
        """
        please rewrite this method to add stop_reason
        """
        self.save_primary_status(
            self.primary_status.FAILED,
            request_id
        )
        self.save_input_status(
            self.input_status.INPUT_FAILED,
            input_number,
            request_id
        )

    def on_failure(self, *request_args, **request_kwargs):
        request_id = self.request.kwargs('request_id', None)
        input_number = self.request.kwargs('input_number', None)
        if request_id is not None and input_number is not None:
            self.save_failed(
                request_id,
                input_number
            )
        return super().on_failure(*request_args, **request_kwargs)

    def raise_ignore(self,
                     message=None,
                     state=states.FAILURE,
                     request_kwargs: dict = None):
        if request_kwargs:
            request_id = request_kwargs.get('request_id', None)
            input_status = request_kwargs.get('input_status', None)
            if request_id is not None and input_status is not None:
                if state == states.FAILURE:
                    self.save_failed(
                        request_id,
                        input_status
                    )
                elif state == states.REVOKED:
                    self.save_input_status(
                        self.input_status.INPUT_REVOKED,
                        input_status,
                        request_id
                    )
        super().raise_ignore(
            message=message,
            state=state,
            request_kwargs=request_kwargs)

    def save_input_status(self, status_name, input_number, request_id):

        # check request_id, input_number and JOB_DETAILS has been set

        if request_id is None:
            # request_id has been not set
            return

        if input_number is None:
            # input_number has been not set
            return None

        if not self.cache.get(CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id)):
            # JOB_DETAILS has been not set
            return None

        # to prevent set input status after it was set to 'INPUT_FAILED'
        if self.can_set_input_status(input_number, request_id):

            # add input status name as message to logger
            log_message = f"input status: {status_name}"
            if request_id:
                log_message += f" ,request id: {request_id}"
            if input_number:
                log_message += f" ,input number: {input_number}"
            self.logger.info(log_message)

            self.cache.set(
                CacheKeysTemplates.INPUT_STATUS.format(
                    request_id=request_id,
                    input_number=input_number),
                status_name
            )
        
        # check to delete progress data of downloading
        if status_name == self.input_status.DOWNLOADING_FINISHED:
            self.cache.delete(
                CacheKeysTemplates.INPUT_DOWNLOADING_PROGRESS.format(
                    request_id=request_id,
                    input_number=input_number
                ))

    def can_set_input_status(self,
                             input_number,
                             request_id) -> None or bool:
        """to check input current status of job
            is not in 'INPUT_FAILED', 'INPUT_REVOKED'
        """
        if request_id is None:
            return None
        if input_number is None:
            return None

        input_current_status = self.cache.get(
            CacheKeysTemplates.INPUT_STATUS.format(
                request_id=request_id,
                input_number=input_number), decode=False)
        return input_current_status not in [
            self.input_status.INPUT_REVOKED,
            self.input_status.INPUT_FAILED
        ]

    def incr_ready_inputs(self, request_id):
        job_details: dict = self.cache.get(
            CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id))
        if job_details:
            ready_inputs = self.incr("READY_INPUTS", request_id)
            if ready_inputs == job_details['total_inputs']:
                self.save_primary_status(
                    self.primary_status.ALL_INPUTS_DOWNLOADED,
                    request_id
                )
