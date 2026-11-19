"""Lazy exports for the optional gRPC server runtime."""

__all__ = ["GrpcServer"]


def __getattr__(name):
    if name != "GrpcServer":
        raise AttributeError(name)
    from .server import GrpcServer
    globals()[name] = GrpcServer
    return GrpcServer
