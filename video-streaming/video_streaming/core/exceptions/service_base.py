

__all__ = [
    'ServiceBaseException'
]


class ServiceBaseException(Exception):

    def __init__(self, *args):
        super().__init__(*args)
