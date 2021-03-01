# from video_streaming.ffmpeg.tasks import create_hls
from video_streaming.grpc.protos import streaming_pb2, streaming_pb2_grpc


class Streaming(streaming_pb2_grpc.StreamingServicer):

    def video_processor(self, request, context):
        response = streaming_pb2.TaskResponse()
        # try:
        #     task = create_hls.apply_async(
        #         args=[],
        #         kwargs={}
        #     )
        # except TypeError:
        #     # TODO Argument checking exception
        #     pass
        return response

    def _add_to_server(self, server):
        streaming_pb2_grpc.add_StreamingServicer_to_server(
            self.__class__(),
            server)
