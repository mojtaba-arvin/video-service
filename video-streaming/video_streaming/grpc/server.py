from video_streaming.core.commands import GrpcServerBaseCommand
from video_streaming.grpc.servicers import Health, Streaming


class GrpcServer(GrpcServerBaseCommand):
    """
    gRPC server command
    """

    def serve(self):
        args = self.parse_args()
        server = self.create_server()

        # add StreamingServicer to the server
        Streaming()._add_to_server(server)

        # add HealthServicer to the server
        health_servicer = Health()._add_to_server(server)

        # check args to add secure port to the server
        server = self.config_ssl(args, server)

        print('Starting server. Listening on port {}...'.format(args.port))
        server.start()

        self.set_server_status(server, health_servicer)

