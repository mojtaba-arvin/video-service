import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import grpc

from video_streaming.security.grpc_authentication import (
    AuthenticationError,
    GrpcAuthenticationInterceptor,
    get_current_principal,
)
from video_streaming.security.principal import RequestPrincipal


class _Abort(Exception):
    pass


class _Context:
    def invocation_metadata(self):
        return (("authorization", "Bearer opaque"),)

    def abort(self, code, details):
        raise _Abort((code, details))


class _Authenticator:
    def __init__(self, principal=None, error=None):
        self.principal = principal
        self.error = error
        self.calls = 0

    def authenticate(self, metadata):
        self.calls += 1
        if self.error:
            raise self.error
        return self.principal


def _principal():
    return RequestPrincipal(
        issuer="https://issuer.example",
        subject="subject-1",
        tenant_id="tenant-1",
        scopes=frozenset(("jobs:read",)),
        roles=frozenset(),
        authentication_method="bearer",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


class GrpcAuthenticationInterceptorTests(unittest.TestCase):
    def setUp(self):
        self.principal = _principal()
        self.context = _Context()

    @staticmethod
    def _intercept(interceptor, handler, method="/video.Streaming/Call"):
        details = SimpleNamespace(method=method)
        return interceptor.intercept_service(lambda unused: handler, details)

    def test_unary_unary_handler_receives_request_principal(self):
        auth = _Authenticator(self.principal)
        interceptor = GrpcAuthenticationInterceptor(auth)

        def service(request, context):
            return get_current_principal().tenant_id

        original = grpc.unary_unary_rpc_method_handler(service)
        wrapped = self._intercept(interceptor, original)
        self.assertEqual(wrapped.unary_unary("request", self.context), "tenant-1")
        self.assertEqual(auth.calls, 1)
        with self.assertRaises(AuthenticationError):
            get_current_principal()

    def test_unary_stream_handler_keeps_principal_during_iteration(self):
        auth = _Authenticator(self.principal)
        interceptor = GrpcAuthenticationInterceptor(auth)

        def service(request, context):
            yield get_current_principal().subject
            yield get_current_principal().tenant_id

        original = grpc.unary_stream_rpc_method_handler(service)
        wrapped = self._intercept(interceptor, original)
        self.assertEqual(
            list(wrapped.unary_stream("request", self.context)),
            ["subject-1", "tenant-1"],
        )

    def test_stream_unary_handler_receives_principal(self):
        interceptor = GrpcAuthenticationInterceptor(
            _Authenticator(self.principal)
        )
        original = grpc.stream_unary_rpc_method_handler(
            lambda requests, context: (
                get_current_principal().tenant_id, list(requests)
            )
        )
        wrapped = self._intercept(interceptor, original)
        self.assertEqual(
            wrapped.stream_unary(iter((1, 2)), self.context),
            ("tenant-1", [1, 2]),
        )

    def test_stream_stream_handler_keeps_principal_during_iteration(self):
        interceptor = GrpcAuthenticationInterceptor(
            _Authenticator(self.principal)
        )

        def service(requests, context):
            for request in requests:
                yield (get_current_principal().tenant_id, request)

        original = grpc.stream_stream_rpc_method_handler(service)
        wrapped = self._intercept(interceptor, original)
        self.assertEqual(
            list(wrapped.stream_stream(iter(("a", "b")), self.context)),
            [("tenant-1", "a"), ("tenant-1", "b")],
        )

    def test_missing_or_invalid_principal_fails_closed(self):
        for auth in (_Authenticator(), _Authenticator(error=ValueError())):
            wrapped = self._intercept(
                GrpcAuthenticationInterceptor(auth),
                grpc.unary_unary_rpc_method_handler(lambda req, ctx: req),
            )
            with self.assertRaises(_Abort) as raised:
                wrapped.unary_unary("request", self.context)
            self.assertEqual(raised.exception.args[0][0], grpc.StatusCode.UNAUTHENTICATED)
            self.assertEqual(
                raised.exception.args[0][1], "Authentication required"
            )

    def test_health_check_is_exempt_and_does_not_authenticate(self):
        auth = _Authenticator()
        interceptor = GrpcAuthenticationInterceptor(auth)
        original = grpc.unary_unary_rpc_method_handler(
            lambda request, context: "SERVING"
        )
        wrapped = self._intercept(
            interceptor, original, "/grpc.health.v1.Health/Check"
        )
        self.assertIs(wrapped, original)
        self.assertEqual(auth.calls, 0)

    def test_authenticator_is_mandatory(self):
        with self.assertRaises(ValueError):
            GrpcAuthenticationInterceptor(None)


if __name__ == "__main__":
    unittest.main()
