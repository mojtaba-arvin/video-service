"""Tests for gRPC bearer authentication and request-scoped identity."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import grpc

from video_streaming.grpc.authentication import (
    OIDCBearerInterceptor,
    get_request_principal,
)
from video_streaming.security import RequestPrincipal


class AbortCall(Exception):
    def __init__(self, code, details):
        self.code = code
        self.details = details


class FakeContext:
    def abort(self, code, details):
        raise AbortCall(code, details)


class FakeAuthenticator:
    def __init__(self, principal=None, error=False):
        self.principal = principal
        self.error = error

    def authenticate(self, token):
        if self.error or token != "valid-token":
            from video_streaming.security.oidc import AuthenticationError
            raise AuthenticationError("invalid")
        return self.principal


class GrpcAuthenticationInterceptorTests(unittest.TestCase):
    def setUp(self):
        self.principal = RequestPrincipal(
            issuer="https://issuer.example.test/",
            subject="subject-1",
            tenant_id="tenant-1",
            scopes=frozenset(["video.jobs:create"]),
            roles=frozenset(),
            authentication_method="bearer",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    def handler(self, callback):
        return grpc.unary_unary_rpc_method_handler(callback)

    def details(self, metadata=(), method="/video.Streaming/CreateJob"):
        return SimpleNamespace(
            method=method,
            invocation_metadata=metadata,
        )

    def test_valid_token_attaches_principal_only_during_handler(self):
        seen = []
        handler = self.handler(
            lambda request, context: seen.append(get_request_principal()) or "ok"
        )
        interceptor = OIDCBearerInterceptor(FakeAuthenticator(self.principal))
        wrapped = interceptor.intercept_service(
            lambda _: handler,
            self.details((("authorization", "Bearer valid-token"),)),
        )

        self.assertEqual(wrapped.unary_unary("request", FakeContext()), "ok")
        self.assertEqual(seen, [self.principal])
        self.assertIsNone(get_request_principal())

    def test_missing_or_invalid_bearer_is_unauthenticated(self):
        handler = self.handler(lambda request, context: "must not run")
        interceptor = OIDCBearerInterceptor(FakeAuthenticator(self.principal))
        wrapped = interceptor.intercept_service(lambda _: handler, self.details())

        with self.assertRaises(AbortCall) as caught:
            wrapped.unary_unary("request", FakeContext())
        self.assertEqual(caught.exception.code, grpc.StatusCode.UNAUTHENTICATED)

    def test_health_check_is_explicitly_public(self):
        handler = self.handler(lambda request, context: "healthy")
        interceptor = OIDCBearerInterceptor(FakeAuthenticator(error=True))
        wrapped = interceptor.intercept_service(
            lambda _: handler,
            self.details(method="/grpc.health.v1.Health/Check"),
        )
        self.assertIs(wrapped, handler)
        self.assertEqual(wrapped.unary_unary("request", FakeContext()), "healthy")


if __name__ == "__main__":
    unittest.main()
