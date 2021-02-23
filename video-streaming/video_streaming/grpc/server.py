import grpc
import time
from concurrent import futures
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from video_streaming.grpc.protos import streaming_pb2, streaming_pb2_grpc
from video_streaming.core.commands.base import BaseCommand


class Streaming(streaming_pb2_grpc.StreamingServicer):

    def video_processor(self, request, context):
        # TODO apply_async celery task
        response = streaming_pb2.TaskResponse()
        # TODO add tracking id to response
        return response


class GrpcServer(BaseCommand):
    """
    gRPC server command
    """

    help = "gRPC-based streaming server."

    _MAX_WORKERS = None
    _SLEEP = 60 * 60 * 24  # seconds
    
    def add_arguments(self, parser):
        # TODO use default values from self
        parser.add_argument(
            '--port',
            type=int,
            required=True,
            help='The server listen port'
        )
        parser.add_argument(
            '--ca_cert',
            type=str,
            required=False,
            help='CA cert or bundle.'
        )
        parser.add_argument(
            '--server_cert',
            type=str,
            required=False,
            help='Server certificate.'
        )
        parser.add_argument(
            '--server_key',
            type=str,
            required=False,
            help='Server certificate key.'
        )

    def serve(self):
        parser = self.create_parser()
        args = parser.parse_args()

        server = grpc.server(futures.ThreadPoolExecutor(
            max_workers=self._MAX_WORKERS))
        streaming_pb2_grpc.add_StreamingServicer_to_server(
            Streaming(),
            server)

        health_servicer = health.HealthServicer()
        health_pb2_grpc.add_HealthServicer_to_server(
            health_servicer,
            server)

        ca_cert = None
        client_auth = False
        if args.ca_cert:
            ca_cert = open(args.ca_cert, 'rb').read()
            client_auth = True

        if args.server_cert and args.server_key:
            private_key = open(args.server_key, 'rb').read()
            certificate_chain = open(args.server_cert, 'rb').read()

            credentials = grpc.ssl_server_credentials(
                [(private_key, certificate_chain)],
                root_certificates=ca_cert,
                require_client_auth=client_auth
            )
            server.add_secure_port('[::]:' + str(args.port), credentials)
        else:
            server.add_insecure_port('[::]:' + str(args.port))

        print('Starting server. Listening on port {}...'.format(args.port))
        server.start()

        health_servicer.set('', health_pb2.HealthCheckResponse.SERVING)
        try:
            while True:
                time.sleep(self._SLEEP)
        except KeyboardInterrupt:
            health_servicer.set('',
                                health_pb2.HealthCheckResponse.NOT_SERVING)
            time.sleep(10)
            server.stop(1)
