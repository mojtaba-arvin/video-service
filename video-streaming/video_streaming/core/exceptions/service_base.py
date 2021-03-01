

__all__ = [
    'ServiceBaseException'
]


class ServiceBaseException(Exception):

    def __init__(self, exception: Exception = None):
        super().__init__()
