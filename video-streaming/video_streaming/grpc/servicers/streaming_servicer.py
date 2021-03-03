from video_streaming.grpc.protos import streaming_pb2, streaming_pb2_grpc


class Streaming(streaming_pb2_grpc.StreamingServicer):

    def video_processor(self, request, context):
        response = streaming_pb2.JobResponse()

        # s3_input = request.s3_input
        # outputs = request.outputs
        # reference_id = request.reference_id
        # webhook_url = request.webhook_url

        return response

    def _add_to_server(self, server):
        streaming_pb2_grpc.add_StreamingServicer_to_server(
            self.__class__(),
            server)

