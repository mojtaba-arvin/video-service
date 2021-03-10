import json

from celery.result import AsyncResult, GroupResult
from video_streaming.celery import celery_app
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
        # celery result id
        result_id: str = self.cache.get(
            CacheKeysTemplates.REQUEST_RESULT_ID.format(
                request_id=request_id), decode=False)

        if primary_status and job_details and result_id:

            # # check is GroupResult
            # group_result = celery_app.GroupResult.restore(
            #     result_id)
            # if not group_result:

            async_result = AsyncResult(result_id, app=celery_app)
            if async_result:

                # when state is not in SUCCESS, FAILURE, REVOKED
                if not async_result.ready():
                    # state is in PENDING, RECEIVED, STARTED, REJECTED, RETRY

                    self.cache.set(
                        CacheKeysTemplates.FORCE_STOP_REQUEST.format(
                            request_id=request_id),
                        json.dumps(True))

                    # async_result.revoke()
                    #
                    # status = self.pb2.PrimaryStatus.Value(
                    #     primary_status)
                    # reference_id: str = job_details['reference_id']
                    # revoke_details = dict(
                    #     request_id=request_id,
                    #     reference_id=reference_id,
                    #     status=status
                    # )
                    # return self.pb2.RevokeDetails(**revoke_details)
