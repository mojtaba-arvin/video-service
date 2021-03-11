from video_streaming.cache import RedisCache
from video_streaming.core.constants import CacheKeysTemplates, \
    PrimaryStatus
from video_streaming.grpc.protos import streaming_pb2


class GetResultsMixin(object):

    cache: RedisCache
    pb2: streaming_pb2

    def _outputs(self, request_id, total_outputs):
        outputs: list[streaming_pb2.OutputProgress] = []
        for output_number in range(total_outputs):
            output_status = self.cache.get(
                CacheKeysTemplates.OUTPUT_STATUS.format(
                    request_id=request_id,
                    output_number=output_number),
                decode=False)
            if output_status:
                progress: dict = self.cache.get(
                    CacheKeysTemplates.OUTPUT_PROGRESS.format(
                        request_id=request_id,
                        output_number=output_number)) or {}
                outputs.append(
                    self.pb2.OutputProgress(
                        id=output_number,
                        status=self.pb2.OutputStatus.Value(
                            output_status),
                        **progress))
        return outputs

    def _inputs(self, request_id, total_inputs):
        inputs: list[streaming_pb2.InputProgress] = []
        for input_number in range(total_inputs):
            input_status: str = self.cache.get(
                CacheKeysTemplates.INPUT_STATUS.format(
                    request_id=request_id,
                    input_number=input_number),
                decode=False)
            if input_status:
                progress: dict = self.cache.get(
                    CacheKeysTemplates.INPUT_DOWNLOADING_PROGRESS.format(
                        request_id=request_id,
                        input_number=input_number)) or {}
                inputs.append(
                    self.pb2.InputProgress(
                        id=input_number,
                        status=self.pb2.InputStatus.Value(
                            input_status),
                        **progress))
        return inputs

    def _get_result(self,
                    request_id: str
                    ) -> None or streaming_pb2.ResultDetails:
        primary_status: str = self.cache.get(
            CacheKeysTemplates.PRIMARY_STATUS.format(
                request_id=request_id), decode=False)
        job_details: dict = self.cache.get(
            CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id))
        if primary_status and job_details:
            status = self.pb2.PrimaryStatus.Value(
                primary_status)
            reference_id: str = job_details['reference_id']
            total_checks: int = job_details['total_checks']
            total_inputs: int = job_details['total_inputs']
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
            checks = self.pb2.Checks(
                total=total_checks,
                passed=self.cache.get(
                    CacheKeysTemplates.PASSED_CHECKS.format(
                        request_id=request_id)) or 0)
            result_details = dict(
                request_id=request_id,
                reference_id=reference_id,
                status=status,
                total_outputs=total_outputs,
                revoked_outputs=revoked_outputs,
                ready_outputs=ready_outputs,
                failed_outputs=failed_outputs,
                checks=checks,
                inputs=self._inputs(request_id, total_inputs),
                outputs=self._outputs(request_id, total_outputs)
            )

            # get stop reason if primary status is FAILED or REVOKED
            if status in [
                    PrimaryStatus.FAILED,
                    PrimaryStatus.REVOKED]:
                stop_reason: str = self.cache.get(
                    CacheKeysTemplates.STOP_REASON.format(
                        request_id=request_id), decode=False)
                if stop_reason:
                    result_details['reason'] = self.pb2.StopReason.Value(
                        stop_reason)

            return self.pb2.ResultDetails(**result_details)
