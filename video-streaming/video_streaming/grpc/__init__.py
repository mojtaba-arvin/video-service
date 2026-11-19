"""gRPC transport package.

Keep the package import lightweight: importing a transport submodule must not
eagerly construct the server or import every service implementation. The
composition root can still import GrpcServer from this package; it is loaded
lazily for backwards compatibility.
"""

__all__ = ["GrpcServer"]


def __getattr__(name):
    if name == "GrpcServer":
        from .server import GrpcServer
        return GrpcServer
    raise AttributeError("module {!r} has no attribute {!r}".format(__name__, name))
