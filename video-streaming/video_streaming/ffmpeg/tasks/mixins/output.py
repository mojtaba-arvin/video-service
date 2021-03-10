from video_streaming.core.constants.cache_keys import CacheKeysTemplates
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask


class BaseOutputMixin(object):
    request_id: str
    output_number: int
    delete_inputs: bool
    delete_outputs: bool

    primary_status: BaseStreamingTask.primary_status
    output_status: BaseStreamingTask.output_status
    cache: BaseStreamingTask.cache
    logger: BaseStreamingTask.logger

    incr: BaseStreamingTask.incr
    get_job_details_by_request_id: BaseStreamingTask.get_job_details_by_request_id
    save_primary_status: BaseStreamingTask.save_primary_status
    inputs_remover: BaseStreamingTask.inputs_remover
    outputs_remover: BaseStreamingTask.outputs_remover

    def save_output_status(self, status_name):
        # add output status name as message to logger
        log_message = f"output status: {status_name}"
        if self.request_id:
            log_message += f" ,request id: {self.request_id}"
        if self.output_number:
            log_message += f" ,output number: {self.output_number}"
        self.logger.info(log_message)

        # save output status when request_id , input_number
        # and JOB_DETAILS has been set

        if self.request_id is None:
            # request_id has been not set
            return

        if self.output_number is None:
            # input_number has been not set
            return None

        if not self.cache.get(CacheKeysTemplates.JOB_DETAILS.format(
                request_id=self.request_id)):
            # JOB_DETAILS has been not set
            return None

        # to prevent set output status after it was set to
        # in 'OUTPUT_FAILED' and 'OUTPUT_REVOKED'
        if self.can_set_output_status():
            self.cache.set(
                CacheKeysTemplates.OUTPUT_STATUS.format(
                    request_id=self.request_id,
                    output_number=self.output_number),
                status_name
            )

        # check to delete unnecessary data
        if status_name in [
                self.output_status.PROCESSING_FINISHED,
                self.output_status.UPLOADING_FINISHED]:
            self.cache.delete(
                CacheKeysTemplates.OUTPUT_PROGRESS.format(
                    request_id=self.request_id,
                    output_number=self.output_number
                ))

    def can_set_output_status(self) -> None or bool:
        """to check output current status of job is not in
         'OUTPUT_FAILED', 'OUTPUT_REVOKED'
        """
        if self.request_id is None:
            return None
        if self.output_number is None:
            return None

        output_current_status = self.cache.get(
            CacheKeysTemplates.OUTPUT_STATUS.format(
                request_id=self.request_id,
                output_number=self.output_number), decode=False)
        return output_current_status not in [
            self.output_status.OUTPUT_REVOKED,
            self.output_status.OUTPUT_FAILED]

    def incr_processed_outputs(self, incr_callback: callable = None):
        job_details = self.get_job_details_by_request_id()
        if job_details:
            processed_outputs = self.incr("PROCESSED_OUTPUTS")

            # do after incr processed_outputs
            if not callable(incr_callback):
                incr_callback = self.after_incr_processed_outputs
            incr_callback(processed_outputs, job_details['total_outputs'])

    def incr_failed_outputs(self):
        job_details = self.get_job_details_by_request_id()
        if job_details:
            self.incr("FAILED_OUTPUTS")
            self.check_all_outputs_are_finished()

    def incr_ready_outputs(self):
        job_details = self.get_job_details_by_request_id()
        if job_details:
            self.incr("READY_OUTPUTS")
            self.check_all_outputs_are_finished()

    def outputs_finished(self):
        """determine all outputs are finished"""
        job_details: dict = self.get_job_details_by_request_id()
        if job_details:
            total_outputs: int = job_details['total_outputs']
            ready_outputs: int = self.cache.get(
                CacheKeysTemplates.READY_OUTPUTS.format(
                    request_id=self.request_id)) or 0
            revoked_outputs: int = self.cache.get(
                CacheKeysTemplates.REVOKED_OUTPUTS.format(
                    request_id=self.request_id)) or 0
            failed_outputs: int = self.cache.get(
                CacheKeysTemplates.FAILED_OUTPUTS.format(
                    request_id=self.request_id)) or 0

            return total_outputs == (ready_outputs + revoked_outputs + failed_outputs)

    def after_incr_processed_outputs(
            self,
            processed_outputs: int,
            total_outputs: int):
        """after incr processed outputs, check to safe delete inputs"""

        revoked_outputs: int = self.cache.get(
            CacheKeysTemplates.REVOKED_OUTPUTS.format(
                request_id=self.request_id)) or 0
        failed_outputs: int = self.cache.get(
            CacheKeysTemplates.FAILED_OUTPUTS.format(
                request_id=self.request_id)) or 0

        # when delete_inputs flag is True,
        # check all videos processed to no need for inputs anymore
        if self.delete_inputs and \
            total_outputs == (
                processed_outputs +
                revoked_outputs +
                failed_outputs):
            # delete all local inputs
            self.inputs_remover()

    def check_all_outputs_are_finished(self):
        """
            1. check all outputs are finished
            2. set primary status to 'FINISHED'
            3. remove local outputs files if delete_outputs flag is True
        """
        # calculate that all outputs are finished
        if self.outputs_finished():
            self.save_primary_status(self.primary_status.FINISHED)
            if self.delete_outputs:
                self.outputs_remover()
