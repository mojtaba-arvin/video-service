"""Request-scoped identity propagation for synchronous gRPC handlers.

The interceptor accepts an injected authenticator. It does not parse JWTs or
trust metadata itself; the configured OIDC verifier is separate and must be
wired by the server before protected RPCs are used.
"""
from contextvars import ContextVar

import grpc

from video_streaming.security.principal import RequestPrincipal


class AuthenticationError(Exception):
    """Raised when a request has no trusted, live principal."""


_CURRENT_PRINCIPAL = ContextVar("video_streaming_request_principal", default=None)

_HEALTH_METHODS = frozenset((
    "/grpc.health.v1.Health/Check",
    "/grpc.health.v1.Health/Watch",
))


def get_current_principal():
    """Return the principal established for the current RPC execution."""
    principal = _CURRENT_PRINCIPAL.get()
    if not isinstance(principal, RequestPrincipal):
        raise AuthenticationError("Authenticated request principal is required")
    if principal.is_expired():
        raise AuthenticationError("Authenticated request principal is required")
    return principal


class GrpcAuthenticationInterceptor(grpc.ServerInterceptor):
    """Authenticate protected RPCs and scope identity to handler execution.

    The authenticator must expose authenticate(metadata) and return a
    verified RequestPrincipal. Authentication failures are normalized so
    token, issuer, tenant and key details are not disclosed.
    """

    def __init__(self, authenticator, health_methods=None):
        if authenticator is None or not callable(
                getattr(authenticator, "authenticate", None)):
            raise ValueError("A trusted authenticator is required")
        self._authenticator = authenticator
        self._health_methods = frozenset(
            health_methods if health_methods is not None else _HEALTH_METHODS
        )

    def _authenticate(self, context):
        try:
            principal = self._authenticator.authenticate(
                context.invocation_metadata()
            )
            if not isinstance(principal, RequestPrincipal):
                raise AuthenticationError("Invalid principal")
            if principal.is_expired():
                raise AuthenticationError("Expired principal")
            return principal
        except Exception:
            context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "Authentication required",
            )

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        if (
                handler is None
                or handler_call_details.method in self._health_methods):
            return handler

        if handler.unary_unary:
            def unary_unary(request, context):
                return self._invoke(
                    handler.unary_unary, (request, context), context
                )
            return grpc.unary_unary_rpc_method_handler(
                unary_unary,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )

        if handler.unary_stream:
            def unary_stream(request, context):
                return self._invoke_stream(
                    handler.unary_stream, (request, context), context
                )
            return grpc.unary_stream_rpc_method_handler(
                unary_stream,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )

        if handler.stream_unary:
            def stream_unary(request_iterator, context):
                return self._invoke(
                    handler.stream_unary, (request_iterator, context), context
                )
            return grpc.stream_unary_rpc_method_handler(
                stream_unary,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )

        if handler.stream_stream:
            def stream_stream(request_iterator, context):
                return self._invoke_stream(
                    handler.stream_stream, (request_iterator, context), context
                )
            return grpc.stream_stream_rpc_method_handler(
                stream_stream,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )

        return handler

    def _invoke(self, handler, args, context):
        principal = self._authenticate(context)
        token = _CURRENT_PRINCIPAL.set(principal)
        try:
            return handler(*args)
        finally:
            _CURRENT_PRINCIPAL.reset(token)

    def _invoke_stream(self, handler, args, context):
        principal = self._authenticate(context)
        token = _CURRENT_PRINCIPAL.set(principal)
        try:
            yield from handler(*args)
        finally:
            _CURRENT_PRINCIPAL.reset(token)
