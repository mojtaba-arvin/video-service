from video_streaming.cache import RedisCache
from video_streaming.core.constants import CacheKeysTemplates
from video_streaming.grpc.protos import streaming_pb2


class RevokeJobsMixin(object):

    cache: RedisCache
    pb2: streaming_pb2

    def _revoke_job(self,
                    request_id: str
                    ) -> None or streaming_pb2.ResultDetails:
        primary_status: str = self.cache.get(
            CacheKeysTemplates.PRIMARY_STATUS.format(
                request_id=request_id), decode=False)
        job_details: dict = self.cache.get(
            CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id))
        if primary_status and job_details:

            # TODO revoke the job

            status = self.pb2.PrimaryStatus.Value(
                primary_status)
            reference_id: str = job_details['reference_id']
            revoke_details = dict(
                request_id=request_id,
                reference_id=reference_id,
                status=status
            )
            return self.pb2.RevokeDetails(**revoke_details)
