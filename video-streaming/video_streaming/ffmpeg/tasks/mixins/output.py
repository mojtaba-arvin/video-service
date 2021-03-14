from celery import states, Task
from video_streaming.core.constants.cache_keys import CacheKeysTemplates
from video_streaming.ffmpeg.tasks.base import BaseStreamingTask


class BaseOutputMixin(object):
    delete_inputs: bool
    delete_outputs: bool

    error_messages: BaseStreamingTask.error_messages
    primary_status: BaseStreamingTask.primary_status
    output_status: BaseStreamingTask.output_status
    cache: BaseStreamingTask.cache
    logger: BaseStreamingTask.logger

    incr: BaseStreamingTask.incr
    save_primary_status: BaseStreamingTask.save_primary_status
    inputs_remover: BaseStreamingTask.inputs_remover
    outputs_remover: BaseStreamingTask.outputs_remover

    request: Task.request

    def save_failed(self, request_id, output_number):
        """
        please rewrite this method to add stop_reason
        """
        self.save_output_status(
            self.output_status.OUTPUT_FAILED,
            output_number,
            request_id
        )

    def on_failure(self, *request_args, **request_kwargs):
        request_id = request_kwargs.get('request_id', None)
        output_number = request_kwargs.get('output_number', None)
        if request_id is not None and output_number is not None:
            self.save_failed(
                request_id,
                output_number
            )
        return super().on_failure(*request_args, **request_kwargs)

    def raise_ignore(self,
                     message=None,
                     state=states.FAILURE,
                     request_kwargs: dict = None):
        if request_kwargs:
            request_id = request_kwargs.get('request_id', None)
            output_number = request_kwargs.get('output_number', None)
            if request_id is not None and output_number is not None:
                if state == states.FAILURE:
                    self.save_failed(
                        request_id,
                        output_number
                    )
                elif state == states.REVOKED:
                    self.save_output_status(
                        self.output_status.OUTPUT_REVOKED,
                        output_number,
                        request_id
                    )
        super().raise_ignore(
            message=message,
            state=state,
            request_kwargs=request_kwargs)

    def save_output_status(self, status_name, output_number, request_id):

        # check request_id, input_number and JOB_DETAILS has been set

        if request_id is None:
            # request_id has been not set
            return

        if output_number is None:
            # input_number has been not set
            return None

        if not self.cache.get(CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id)):
            # JOB_DETAILS has been not set
            return None

        # to prevent set output status after it was set to
        # in 'OUTPUT_FAILED' and 'OUTPUT_REVOKED'
        if self.can_set_output_status(output_number, request_id):

            # add output status name as message to logger
            log_message = f"output status: {status_name}"
            if request_id:
                log_message += f" ,request id: {request_id}"
            if output_number:
                log_message += f" ,output number: {output_number}"
            self.logger.info(log_message)

            self.cache.set(
                CacheKeysTemplates.OUTPUT_STATUS.format(
                    request_id=request_id,
                    output_number=output_number),
                status_name
            )

            if status_name == self.output_status.OUTPUT_REVOKED:
                self.incr("REVOKED_OUTPUTS", request_id)
                self.check_all_outputs_are_finished(request_id)
            elif status_name == self.output_status.PROCESSING_FINISHED:
                self.incr("PROCESSED_OUTPUTS", request_id)
                self.check_to_delete_inputs(request_id)
                self.check_all_outputs_are_finished(request_id)
            elif status_name == self.output_status.UPLOADING_FINISHED:
                self.incr("READY_OUTPUTS", request_id)
                self.check_all_outputs_are_finished(request_id)
            elif status_name == self.output_status.OUTPUT_FAILED:
                self.incr("FAILED_OUTPUTS", request_id)
                self.check_all_outputs_are_finished(request_id)

            # check to delete unnecessary data
            if status_name in [
                    self.output_status.OUTPUT_REVOKED,
                    self.output_status.PROCESSING_FINISHED,
                    self.output_status.UPLOADING_FINISHED,
                    self.output_status.OUTPUT_FAILED]:
                self.cache.delete(
                    CacheKeysTemplates.OUTPUT_PROGRESS.format(
                        request_id=request_id,
                        output_number=output_number
                    ))

    def can_set_output_status(self,
                              output_number,
                              request_id) -> None or bool:
        """to check output current status of job is not in
         'OUTPUT_FAILED', 'OUTPUT_REVOKED'
        """
        if request_id is None:
            return None
        if output_number is None:
            return None

        output_current_status = self.cache.get(
            CacheKeysTemplates.OUTPUT_STATUS.format(
                request_id=request_id,
                output_number=output_number), decode=False)
        return output_current_status not in [
            self.output_status.OUTPUT_REVOKED,
            self.output_status.OUTPUT_FAILED]

    def check_to_delete_inputs(self, request_id):
        if self.delete_inputs:
            job_details: dict = self.cache.get(
                CacheKeysTemplates.JOB_DETAILS.format(
                    request_id=request_id))
            if job_details:
                total_outputs: int = job_details['total_outputs']
                processed_outputs: int = self.cache.get(
                    CacheKeysTemplates.PROCESSED_OUTPUTS.format(
                        request_id=request_id)) or 0
                revoked_outputs: int = self.cache.get(
                    CacheKeysTemplates.REVOKED_OUTPUTS.format(
                        request_id=request_id)) or 0
                failed_outputs: int = self.cache.get(
                    CacheKeysTemplates.FAILED_OUTPUTS.format(
                        request_id=request_id)) or 0

                if total_outputs == (
                        processed_outputs +
                        revoked_outputs +
                        failed_outputs):
                    # delete all local inputs
                    self.inputs_remover(request_id=request_id)

    def check_all_outputs_are_finished(self, request_id):
        """
            1. check all outputs are finished
            2. set primary status to 'FINISHED' or 'REVOKED' or 'FAILED'
            3. remove local outputs files if delete_outputs flag is True
        """

        job_details: dict = self.cache.get(
            CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id))
        if job_details:
            # calculate that all outputs are finished

            total_outputs: int = job_details['total_outputs']
            ready_outputs: int = self.cache.get(
                CacheKeysTemplates.READY_OUTPUTS.format(
                    request_id=request_id)) or 0
            revoked_outputs: int = self.cache.get(
                CacheKeysTemplates.REVOKED_OUTPUTS.format(
                    request_id=request_id)) or 0
            failed_outputs: int = self.cache.get(
                CacheKeysTemplates.FAILED_OUTPUTS.format(
                    request_id=request_id)) or 0

            is_all_outputs_failed = total_outputs == failed_outputs
            if is_all_outputs_failed:
                self.save_primary_status(
                    status_name=self.primary_status.FAILED,
                    request_id=request_id
                )

            is_all_outputs_revoked = total_outputs == revoked_outputs
            if is_all_outputs_revoked:
                self.save_primary_status(
                    self.primary_status.REVOKED,
                    request_id
                )

            is_finished = total_outputs == (
                        ready_outputs + revoked_outputs + failed_outputs)
            if is_finished:
                self.save_primary_status(
                    self.primary_status.FINISHED,
                    request_id
                )

            if self.delete_outputs:
                if is_finished or is_all_outputs_failed or \
                        is_all_outputs_revoked:
                    self.outputs_remover(request_id=request_id)

    def is_output_forced_to_stop(
            self,
            request_id,
            output_number) -> None or bool:
        force_stop = self.cache.get(
            CacheKeysTemplates.FORCE_STOP_OUTPUT_REQUEST.format(
                request_id=request_id,
                output_number=output_number))
        return force_stop

    def raise_revoke_output(
            self,
            request_id,
            output_number):
        raise self.raise_ignore(
            message=self.error_messages.TASK_WAS_FORCIBLY_STOPPED,
            state=states.REVOKED,
            request_kwargs=self.request.kwargs)
