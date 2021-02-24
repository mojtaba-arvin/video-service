from grpc_health.v1 import health, health_pb2_grpc

from video_streaming.core.commands import GrpcServerBaseCommand
from video_streaming.grpc.servicers import Streaming


class GrpcServer(GrpcServerBaseCommand):
    """
    gRPC server command
    """

    def serve(self):
        args = self.parse_args()
        server = self.create_server()

        # add StreamingServicer to server
        Streaming().add_to_server(server)

        health_servicer = health.HealthServicer()
        health_pb2_grpc.add_HealthServicer_to_server(
            health_servicer,
            server)

        server = self.config_ssl(args, server)

        print('Starting server. Listening on port {}...'.format(args.port))
        server.start()

        self.set_server_status(server, health_servicer)

