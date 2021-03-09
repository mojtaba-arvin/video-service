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

    def incr_processed_outputs(self):
        job_details = self.get_job_details_by_request_id()
        if job_details:
            processed_outputs = self.incr("PROCESSED_OUTPUTS")
            if self.delete_inputs and processed_outputs == job_details['total_outputs']:
                self.inputs_remover()

    def incr_ready_outputs(self):
        job_details = self.get_job_details_by_request_id()
        if job_details:
            ready_outputs = self.incr("READY_OUTPUTS")
            if ready_outputs == job_details['total_outputs']:
                self.save_primary_status(
                    self.primary_status.ALL_OUTPUTS_ARE_READY
                )
                if self.delete_outputs:
                    self.outputs_remover()

