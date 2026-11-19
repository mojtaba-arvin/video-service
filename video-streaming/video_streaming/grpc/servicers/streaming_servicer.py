import logging
import traceback
from video_streaming.cache import RedisCache
from video_streaming.core.constants import ErrorMessages
from video_streaming.grpc import exceptions
from video_streaming.grpc.protos import streaming_pb2_grpc, \
    streaming_pb2
from .mixins import (
    CreateJobMixin,
    GetResultsMixin,
    PlaybackQoEMixin,
    RevokeJobsMixin,
    RevokeOutputsMixin,
)


logger = logging.getLogger(__name__)


class Streaming(
        RevokeOutputsMixin,
        RevokeJobsMixin,
        GetResultsMixin,
        CreateJobMixin,
        PlaybackQoEMixin,
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
        # Request and exception values may contain secrets or signed URLs.
        # Keep stack context without interpolating untrusted values.
        logger.error(
            "Unhandled gRPC service failure",
            extra={
                "error_type": type(exc).__name__,
                "traceback": traceback.format_tb(exc.__traceback__),
            },
        )
        raise exceptions.GrpcBaseException(
            ErrorMessages.INTERNAL_ERROR)

    def create_job(self, request, context):
        # The request can contain private storage and callback information.
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
        try:
            return self._revoke_jobs(request, context)
        except Exception as exc:
            self._exception_handler(exc)

    def revoke_job_outputs(self, request, context):
        """force stop a list of outputs for one job"""
        try:
            return self._revoke_job_outputs(request, context)
        except Exception as exc:
            self._exception_handler(exc)
