from video_streaming.grpc.protos import streaming_pb2, streaming_pb2_grpc


class Streaming(streaming_pb2_grpc.StreamingServicer):

    def video_processor(self, request, context):
        # TODO apply_async celery task
        response = streaming_pb2.TaskResponse()
        # TODO add tracking id to response
        return response

    def _add_to_server(self, server):
        streaming_pb2_grpc.add_StreamingServicer_to_server(
            self.__class__(),
            server)
