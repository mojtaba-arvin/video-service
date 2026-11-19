"""Unit tests for the OIDC/JWKS verifier without external identity services."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from video_streaming.security.oidc import (
    AuthenticationError,
    OIDCAuthenticator,
)


class OIDCAuthenticatorTests(unittest.TestCase):
    def make_authenticator(self, **overrides):
        values = {
            "issuer": "https://issuer.example.test/",
            "audience": "video-api",
            "jwks_uri": "https://issuer.example.test/.well-known/jwks.json",
            "algorithms": ("RS256",),
        }
        values.update(overrides)
        return OIDCAuthenticator(**values)

    @patch("video_streaming.security.oidc.jwt.PyJWKClient")
    @patch("video_streaming.security.oidc.jwt.decode")
    def test_verifies_with_configured_jwks_and_builds_principal(
            self, decode, jwks_client):
        key = Mock()
        jwks_client.return_value.get_signing_key_from_jwt.return_value = key
        expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
        decode.return_value = {
            "iss": "https://issuer.example.test/",
            "aud": "video-api",
            "sub": "subject-1",
            "tenant_id": "tenant-1",
            "exp": expiry.timestamp(),
            "scope": "video.jobs:create video.jobs:read",
            "roles": ["operator"],
        }

        authenticator = self.make_authenticator()
        principal = authenticator.authenticate("signed.jwt.value")

        jwks_client.assert_called_once_with(
            "https://issuer.example.test/.well-known/jwks.json",
            cache_jwk_set=True,
            lifespan=300,
            timeout=5,
        )
        jwks_client.return_value.get_signing_key_from_jwt.assert_called_once_with(
            "signed.jwt.value"
        )
        self.assertEqual(principal.issuer, "https://issuer.example.test/")
        self.assertEqual(principal.subject, "subject-1")
        self.assertEqual(principal.tenant_id, "tenant-1")
        self.assertEqual(principal.scopes, frozenset([
            "video.jobs:create", "video.jobs:read"
        ]))
        self.assertEqual(principal.roles, frozenset(["operator"]))
        self.assertEqual(decode.call_args.kwargs["algorithms"], ["RS256"])
        self.assertEqual(decode.call_args.kwargs["audience"], "video-api")
        self.assertEqual(
            decode.call_args.kwargs["issuer"], "https://issuer.example.test/"
        )

    def test_rejects_non_https_trust_urls(self):
        with self.assertRaises(ValueError):
            self.make_authenticator(issuer="http://issuer.example.test/")
        with self.assertRaises(ValueError):
            self.make_authenticator(
                jwks_uri="http://issuer.example.test/keys"
            )

    def test_rejects_symmetric_or_unsigned_algorithms(self):
        for algorithm in ("none", "HS256"):
            with self.subTest(algorithm=algorithm):
                with self.assertRaises(ValueError):
                    self.make_authenticator(algorithms=(algorithm,))

    @patch("video_streaming.security.oidc.jwt.PyJWKClient")
    @patch("video_streaming.security.oidc.jwt.decode")
    def test_missing_tenant_claim_fails_closed(self, decode, jwks_client):
        jwks_client.return_value.get_signing_key_from_jwt.return_value.key = object()
        decode.return_value = {
            "iss": "https://issuer.example.test/",
            "aud": "video-api",
            "sub": "subject-1",
            "exp": (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp(),
        }
        with self.assertRaises(AuthenticationError):
            self.make_authenticator().authenticate("signed.jwt.value")

    @patch("video_streaming.security.oidc.jwt.PyJWKClient")
    def test_jwks_failure_fails_closed(self, jwks_client):
        jwks_client.return_value.get_signing_key_from_jwt.side_effect = RuntimeError(
            "network failure"
        )
        with self.assertRaises(AuthenticationError):
            self.make_authenticator().authenticate("signed.jwt.value")


if __name__ == "__main__":
    unittest.main()
