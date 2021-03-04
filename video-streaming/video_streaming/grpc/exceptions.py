from grpc import StatusCode
from video_streaming.core.constants import ErrorMessages
from video_streaming.core.exceptions import GrpcBaseException


__all__ = [
    'UnknownException'
]


class UnknownException(GrpcBaseException):
    status_code = StatusCode.UNKNOWN
    message = ErrorMessages.UNKNOWN_ERROR

