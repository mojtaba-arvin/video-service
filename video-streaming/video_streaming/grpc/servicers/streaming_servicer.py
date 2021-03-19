import pprint
import traceback
from celery import chain, group, chord
from video_streaming.cache import RedisCache
from video_streaming.core.constants import CacheKeysTemplates, \
    ErrorMessages
from video_streaming.ffmpeg.tasks.download_input import \
    DownloadInputTask
from video_streaming.ffmpeg import tasks
from video_streaming.grpc import exceptions
from video_streaming.grpc.protos import streaming_pb2_grpc, \
    streaming_pb2
from .mixins import CreateJobMixin, GetResultsMixin, RevokeJobsMixin, \
    RevokeOutputsMixin


class Streaming(
        RevokeOutputsMixin,
        RevokeJobsMixin,
        GetResultsMixin,
        CreateJobMixin,
        streaming_pb2_grpc.StreamingServicer):

    cache = RedisCache()
    pb2 = streaming_pb2

    def _add_to_server(self, server):
        streaming_pb2_grpc.add_StreamingServicer_to_server(
            self,
            server)

    @staticmethod
    def _exception_handler(exc: Exception):
        if isinstance(exc, exceptions.GrpcBaseException):
            raise exc
        print(traceback.format_exc())
        print(exc)
        raise exceptions.GrpcBaseException(
            ErrorMessages.INTERNAL_ERROR)

    def create_job(self, request, context):
        pprint.pprint(request)
        try:
            return self._create_job(request, context)
        except Exception as exc:
            self._exception_handler(exc)

    def get_results(self, request, context):
        """get results for a list of jobs"""
        try:
            return self._get_results(request, context)
        except Exception as exc:
            self._exception_handler(exc)

    def revoke_jobs(self, request, context):
        """force stop a list of jobs
        to kill job outputs processes and delete all local files
        """
        results: list[Streaming.pb2.RevokeDetails] = []
        for request_id in request.tracking_ids:
            result = self._revoke_job(request_id)
            if result:
                results.append(result)
        response = self.pb2.RevokeJobsResponse(results=results)
        return response

    def revoke_job_outputs(self, request, context):
        """force stop a list of outputs for one job"""

        request_id: str = request.tracking_id
        primary_status: str = self.cache.get(
            CacheKeysTemplates.PRIMARY_STATUS.format(
                request_id=request_id), decode=False)
        job_details: dict = self.cache.get(
            CacheKeysTemplates.JOB_DETAILS.format(
                request_id=request_id))

        # raise if job is already has been executed
        self._raise_if_job_already_executed(
            primary_status,
            job_details)

        playlists_to_revoke: list[Streaming.pb2.OutputsToRevoke] = \
            self._outputs_to_revoke(
            request.playlists_numbers,
            primary_status,
            request_id,
            job_details['total_outputs'],
            CacheKeysTemplates.PLAYLIST_OUTPUT_ID
        )

        thumbnails_to_revoke: list[Streaming.pb2.OutputsToRevoke] = \
            self._outputs_to_revoke(
            request.thumbnails_numbers,
            primary_status,
            request_id,
            job_details['total_outputs'],
            CacheKeysTemplates.THUMBNAIL_OUTPUT_ID
        )

        response = self.pb2.RevokeOutputsResponse(
            tracking_id=request_id,
            reference_id=job_details['reference_id'],
            playlists_to_revoke=playlists_to_revoke,
            thumbnails_to_revoke=thumbnails_to_revoke
        )
        return response
