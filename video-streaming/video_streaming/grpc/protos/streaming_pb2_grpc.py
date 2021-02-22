# Generated by the gRPC Python protocol compiler plugin. DO NOT EDIT!
"""Client and server classes corresponding to protobuf-defined services."""
import grpc

from . import streaming_pb2 as streaming__pb2


class StreamingStub(object):
    """Missing associated documentation comment in .proto file."""

    def __init__(self, channel):
        """Constructor.

        Args:
            channel: A grpc.Channel.
        """
        self.video_processor = channel.unary_unary(
                '/Streaming/video_processor',
                request_serializer=streaming__pb2.TaskRequest.SerializeToString,
                response_deserializer=streaming__pb2.TaskResponse.FromString,
                )


class StreamingServicer(object):
    """Missing associated documentation comment in .proto file."""

    def video_processor(self, request, context):
        """Missing associated documentation comment in .proto file."""
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details('Method not implemented!')
        raise NotImplementedError('Method not implemented!')


def add_StreamingServicer_to_server(servicer, server):
    rpc_method_handlers = {
            'video_processor': grpc.unary_unary_rpc_method_handler(
                    servicer.video_processor,
                    request_deserializer=streaming__pb2.TaskRequest.FromString,
                    response_serializer=streaming__pb2.TaskResponse.SerializeToString,
            ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
            'Streaming', rpc_method_handlers)
    server.add_generic_rpc_handlers((generic_handler,))


 # This class is part of an EXPERIMENTAL API.
class Streaming(object):
    """Missing associated documentation comment in .proto file."""

    @staticmethod
    def video_processor(request,
            target,
            options=(),
            channel_credentials=None,
            call_credentials=None,
            insecure=False,
            compression=None,
            wait_for_ready=None,
            timeout=None,
            metadata=None):
        return grpc.experimental.unary_unary(request, target, '/Streaming/video_processor',
            streaming__pb2.TaskRequest.SerializeToString,
            streaming__pb2.TaskResponse.FromString,
            options, channel_credentials,
            insecure, call_credentials, compression, wait_for_ready, timeout, metadata)
