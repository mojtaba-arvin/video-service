from video_streaming.core.constants.cache_keys import CacheKeysTemplates
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask


class BaseInputMixin(object):
    request_id: str
    input_number: int

    primary_status: BaseStreamingTask.primary_status
    input_status: BaseStreamingTask.input_status
    cache: BaseStreamingTask.cache
    logger: BaseStreamingTask.logger

    incr: BaseStreamingTask.incr
    get_job_details_by_request_id: BaseStreamingTask.get_job_details_by_request_id
    save_primary_status: BaseStreamingTask.save_primary_status

    def save_input_status(self, status_name):
        # add input status name as message to logger
        log_message = f"input status: {status_name}"
        if self.request_id:
            log_message += f" ,request id: {self.request_id}"
        if self.input_number:
            log_message += f" ,input number: {self.input_number}"
        self.logger.info(log_message)

        # save input status when request_id , input_number
        # and JOB_DETAILS has been set

        if self.request_id is None:
            # request_id has been not set
            return

        if self.input_number is None:
            # input_number has been not set
            return None

        if not self.cache.get(CacheKeysTemplates.JOB_DETAILS.format(
                request_id=self.request_id)):
            # JOB_DETAILS has been not set
            return None

        # to prevent set input status after it was set to 'INPUT_FAILED'
        if self.can_set_input_status():
            self.cache.set(
                CacheKeysTemplates.INPUT_STATUS.format(
                    request_id=self.request_id,
                    input_number=self.input_number),
                status_name
            )
        
        # check to delete progress data of downloading
        if status_name == self.input_status.DOWNLOADING_FINISHED:
            self.cache.delete(
                CacheKeysTemplates.INPUT_DOWNLOADING_PROGRESS.format(
                    request_id=self.request_id,
                    input_number=self.input_number
                ))

    def can_set_input_status(self) -> None or bool:
        """to check input current status of job
            is not in 'INPUT_FAILED', 'INPUT_REVOKED'
        """
        if self.request_id is None:
            return None
        if self.input_number is None:
            return None

        input_current_status = self.cache.get(
            CacheKeysTemplates.INPUT_STATUS.format(
                request_id=self.request_id,
                input_number=self.input_number), decode=False)
        return input_current_status not in [
            self.input_status.INPUT_REVOKED,
            self.input_status.INPUT_FAILED
        ]

    def incr_ready_inputs(self):
        job_details = self.get_job_details_by_request_id()
        if job_details:
            ready_inputs = self.incr("READY_INPUTS")
            if ready_inputs == job_details['total_inputs']:
                self.save_primary_status(
                    self.primary_status.ALL_INPUTS_DOWNLOADED
                )
