"""Lazy exports for gRPC servicers.

Importing a focused transport adapter or protobuf module must not eagerly
initialize the network server and its optional grpc runtime dependency.
"""

__all__ = ["Health", "Streaming"]


def __getattr__(name):
    if name == "Health":
        from .health_servicer import Health
        globals()[name] = Health
        return Health
    if name == "Streaming":
        from .streaming_servicer import Streaming
        globals()[name] = Streaming
        return Streaming
    raise AttributeError(name)
