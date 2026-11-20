from video_streaming import settings
from video_streaming.core.commands import GrpcServerBaseCommand
from video_streaming.grpc.authentication import OIDCBearerInterceptor
from video_streaming.security.oidc import OIDCAuthenticator
from video_streaming.grpc.servicers import Health, Streaming


class GrpcServer(GrpcServerBaseCommand):
    """
    gRPC server command
    """

    def serve(self):
        args = self.parse_args()
        if settings.GRPC_AUTH_MODE != "oidc":
            raise RuntimeError("GRPC_AUTH_MODE must be explicitly set to oidc")
        authenticator = OIDCAuthenticator(
            issuer=settings.OIDC_ISSUER,
            audience=settings.OIDC_AUDIENCE,
            jwks_uri=settings.OIDC_JWKS_URI,
            algorithms=settings.OIDC_ALLOWED_ALGORITHMS,
            tenant_claim=settings.OIDC_TENANT_CLAIM,
            subject_claim=settings.OIDC_SUBJECT_CLAIM,
            scopes_claim=settings.OIDC_SCOPES_CLAIM,
            roles_claim=settings.OIDC_ROLES_CLAIM,
            leeway_seconds=settings.OIDC_CLOCK_LEEWAY_SECONDS,
            jwks_timeout_seconds=settings.OIDC_JWKS_TIMEOUT_SECONDS,
        )
        server = self.create_server(interceptors=(
            OIDCBearerInterceptor(authenticator),
        ))

        # add StreamingServicer to the server
        Streaming()._add_to_server(server)

        # add HealthServicer to the server
        health_servicer = Health()._add_to_server(server)

        # check args to add secure port to the server
        server = self.config_ssl(args, server)

        print('Starting server. Listening on port {}...'.format(args.port))
        server.start()

        self.set_server_status(server, health_servicer)

