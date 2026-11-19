"""gRPC bearer authentication interceptor and request principal scope."""
from contextvars import ContextVar
import grpc

from video_streaming.security import RequestPrincipal
from video_streaming.security.oidc import AuthenticationError


_request_principal = ContextVar("video_request_principal", default=None)


def get_request_principal():
    """Return the principal for the currently executing RPC, if authenticated."""
    return _request_principal.get()


class OIDCBearerInterceptor(grpc.ServerInterceptor):
    """Authenticate every RPC except the explicitly public health service."""

    _PUBLIC_METHODS = frozenset((
        "/grpc.health.v1.Health/Check",
        "/grpc.health.v1.Health/Watch",
    ))

    def __init__(self, authenticator):
        if authenticator is None or not callable(
                getattr(authenticator, "authenticate", None)):
            raise ValueError("A configured authenticator is required")
        self._authenticator = authenticator

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        if handler is None:
            return None
        if handler_call_details.method in self._PUBLIC_METHODS:
            return handler

        try:
            principal = self._authenticator.authenticate(
                self._authorization_from_details(handler_call_details)
            )
            if not isinstance(principal, RequestPrincipal) or principal.is_expired():
                raise AuthenticationError("Invalid access token")
        except Exception:
            # Any verifier or metadata failure must fail closed.
            return self._unauthenticated_handler(handler)

        def invoke(callback, request_or_iterator, context):
            token = _request_principal.set(principal)
            try:
                return callback(request_or_iterator, context)
            finally:
                _request_principal.reset(token)

        if handler.unary_unary:
            callback = handler.unary_unary
            return grpc.unary_unary_rpc_method_handler(
                lambda request, context: invoke(callback, request, context),
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
        if handler.unary_stream:
            callback = handler.unary_stream

            def stream(request, context):
                token = _request_principal.set(principal)
                try:
                    for item in callback(request, context):
                        yield item
                finally:
                    _request_principal.reset(token)

            return grpc.unary_stream_rpc_method_handler(
                stream,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
        if handler.stream_unary:
            callback = handler.stream_unary
            return grpc.stream_unary_rpc_method_handler(
                lambda requests, context: invoke(callback, requests, context),
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
        if handler.stream_stream:
            callback = handler.stream_stream

            def stream(requests, context):
                token = _request_principal.set(principal)
                try:
                    for item in callback(requests, context):
                        yield item
                finally:
                    _request_principal.reset(token)

            return grpc.stream_stream_rpc_method_handler(
                stream,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )
        return handler

    @staticmethod
    def _authorization_from_details(details):
        # gRPC metadata entries expose key/value attributes. Accept tuple pairs
        # as well to keep the parsing contract straightforward in unit tests.
        values = details.invocation_metadata or ()
        matches = []
        for item in values:
            if hasattr(item, "key") and hasattr(item, "value"):
                key, value = item.key, item.value
            else:
                try:
                    key, value = item
                except (TypeError, ValueError):
                    raise AuthenticationError("Invalid access token")
            if isinstance(key, str) and key.lower() == "authorization":
                matches.append(value)
        if len(matches) != 1:
            raise AuthenticationError("Invalid access token")
        value = matches[0]
        if not isinstance(value, str):
            raise AuthenticationError("Invalid access token")
        scheme, separator, token = value.partition(" ")
        if not separator or scheme.lower() != "bearer":
            raise AuthenticationError("Invalid access token")
        token = token.strip()
        if not token or any(character.isspace() for character in token):
            raise AuthenticationError("Invalid access token")
        return token

    @staticmethod
    def _unauthenticated_handler(handler):
        def abort(request_or_iterator, context):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Unauthenticated")

        if handler.unary_unary:
            return grpc.unary_unary_rpc_method_handler(
                abort, request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer)
        if handler.unary_stream:
            return grpc.unary_stream_rpc_method_handler(
                abort, request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer)
        if handler.stream_unary:
            return grpc.stream_unary_rpc_method_handler(
                abort, request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer)
        if handler.stream_stream:
            return grpc.stream_stream_rpc_method_handler(
                abort, request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer)
        return handler
