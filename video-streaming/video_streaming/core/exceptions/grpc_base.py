from grpc import ServicerContext
from video_streaming.core.constants import ErrorCodes

__all__ = [
    'GrpcBaseException',
]


class GrpcBaseException(Exception):
    status_code: str = ErrorCodes.INTERNAL_ERROR
    message: None

    def __init__(self,
                 message: str = None,
                 context: ServicerContext = None,
                 status_code: tuple = None):

        self.status_code = self.status_code if status_code is None\
            else status_code
        self.message = self.message if message is None\
            else message

        if context:
            context.set_code(self.status_code)
            context.set_details(self.message)
        super().__init__(message)
