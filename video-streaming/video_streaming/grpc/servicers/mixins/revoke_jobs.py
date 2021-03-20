import json
from celery.result import AsyncResult
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

        job_details: dict = self.cache.get(
            CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id))
        if not job_details:
            revoke_details = dict(
                tracking_id=request_id,
                has_been_sent=self.pb2.RevokeSignalStatus.REQUEST_NOT_FOUND
            )
            return self.pb2.RevokeDetails(**revoke_details)

        reference_id: str = job_details['reference_id']
        signal_sent: bool = self.cache.get(
            CacheKeysTemplates.FORCE_STOP_REQUEST.format(
                request_id=request_id))
        if not signal_sent:
            self.cache.set(
                CacheKeysTemplates.FORCE_STOP_REQUEST.format(
                    request_id=request_id),
                json.dumps(True))

            # celery result id
            result_id: str = self.cache.get(
                CacheKeysTemplates.REQUEST_RESULT_ID.format(
                    request_id=request_id), decode=False)
            if result_id:
                async_result = AsyncResult(result_id, app=celery_app)
                if async_result and not async_result.ready():
                    async_result.revoke()

        revoke_details = dict(
            tracking_id=request_id,
            reference_id=reference_id,
            has_been_sent=self.pb2.RevokeSignalStatus.REVOKE_SIGNAL_SENT
        )
        return self.pb2.RevokeDetails(**revoke_details)

        # # check is GroupResult
        # group_result = celery_app.GroupResult.restore(
        #     result_id)
        # if not group_result:
        #     async_result = AsyncResult(result_id, app=celery_app)
        #     if async_result:
        #         # when state is not in SUCCESS, FAILURE, REVOKED
        #         if not async_result.ready():
        #             # state is in PENDING, RECEIVED, STARTED, REJECTED, RETRY
        #             async_result.revoke()

    def _revoke_jobs(self, request, context):
        results: list[streaming_pb2.RevokeDetails] = []
        for request_id in request.tracking_ids:
            result = self._revoke_job(request_id)
            if result:
                results.append(result)
        response = self.pb2.RevokeJobsResponse(results=results)
        return response

