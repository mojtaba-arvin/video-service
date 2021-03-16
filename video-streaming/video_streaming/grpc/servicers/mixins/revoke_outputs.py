import json
from video_streaming.cache import RedisCache
from video_streaming.grpc import exceptions
from video_streaming.core.constants import CacheKeysTemplates, \
    PrimaryStatus, OutputStatus
from video_streaming.grpc.protos import streaming_pb2


class RevokeOutputsMixin(object):

    cache: RedisCache
    pb2: streaming_pb2

    @staticmethod
    def _raise_if_job_already_executed(
            primary_status: str,
            job_details: dict
            ):
        if not primary_status or not job_details:
            raise exceptions.JobNotFoundException
        if primary_status == PrimaryStatus.FAILED:
            raise exceptions.JobIsFailedException
        if primary_status == PrimaryStatus.REVOKED:
            raise exceptions.JobIsRevokedException
        if primary_status == PrimaryStatus.FINISHED:
            raise exceptions.JobIsFinishedException

    def _send_revoke_output_signal(self, request_id, output_id):
        signal_sent: bool = self.cache.get(
            CacheKeysTemplates.FORCE_STOP_OUTPUT_REQUEST.format(
                request_id=request_id,
                output_id=output_id))
        if not signal_sent:
            self.cache.set(
                CacheKeysTemplates.FORCE_STOP_OUTPUT_REQUEST.format(
                    request_id=request_id,
                    output_id=output_id),
                json.dumps(True))

    def _outputs_to_revoke(
            self,
            playlists_numbers: list[int],
            primary_status: str,
            request_id: str,
            total_outputs: int,
            id_prefix: str
            ) -> list[streaming_pb2.OutputsToRevoke]:
        outputs_to_revoke: list[streaming_pb2.OutputsToRevoke] = []
        for number in playlists_numbers:
            output_id = id_prefix + str(number)
            # is number valid?
            if number not in range(total_outputs):
                outputs_to_revoke.append(self.pb2.OutputsToRevoke(
                        output_number=number,
                        signal_failed_reason=self.pb2.
                        signalFailedReason.OUTPUT_NUMBER_IS_INVALID))
                continue

            # check job has any output status to know if output is
            # already executed and can not revoke it
            if primary_status == PrimaryStatus.OUTPUTS_PROGRESSING:

                output_status = self.cache.get(
                    CacheKeysTemplates.OUTPUT_STATUS.format(
                        request_id=request_id,
                        output_id=output_id),
                    decode=False)
                if output_status == OutputStatus.OUTPUT_FAILED:
                    outputs_to_revoke.append(self.pb2.OutputsToRevoke(
                        output_number=number,
                        signal_failed_reason=self.pb2.
                        signalFailedReason.OUTPUT_HAS_BEEN_FAILED
                    ))
                    continue
                if output_status == OutputStatus.UPLOADING_FINISHED:
                    outputs_to_revoke.append(self.pb2.OutputsToRevoke(
                        output_number=number,
                        signal_failed_reason=self.pb2.
                        signalFailedReason.OUTPUT_HAS_BEEN_UPLOADED
                    ))
                    continue
                if output_status == OutputStatus.UPLOADING:
                    # when output is uploading, it's not safe to stop
                    outputs_to_revoke.append(self.pb2.OutputsToRevoke(
                        output_number=number,
                        signal_failed_reason=self.
                        pb2.signalFailedReason.
                        OUTPUT_UPLOADING_COULD_NOT_BE_STOPPED
                    ))
                    continue

            # ok, output can revoke
            self._send_revoke_output_signal(request_id, output_id)
            outputs_to_revoke.append(self.pb2.OutputsToRevoke(
                    output_number=number,
                    signal_has_been_sent=True))
        return outputs_to_revoke
