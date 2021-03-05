from grpc import StatusCode, ServicerContext


__all__ = [
    'GrpcBaseException',
]


class GrpcBaseException(Exception):
    status_code: str = StatusCode.UNKNOWN
    message: str = None

    def __init__(self,
                 context: ServicerContext = None,
                 status_code: tuple = None,
                 message: str = None):

        self.status_code = self.status_code if status_code is None\
            else status_code
        self.message = self.message if message is None\
            else message

        if context:
            context.set_code(self.status_code)
            context.set_details(self.message)
        super().__init__(message)
