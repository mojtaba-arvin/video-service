import json
from video_streaming.cache import RedisCache
from video_streaming.grpc import exceptions
from video_streaming.core.constants import CacheKeysTemplates, \
    PrimaryStatus
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
