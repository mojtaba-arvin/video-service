"""Tests for the immutable authenticated-request principal contract."""

import unittest
from datetime import datetime, timedelta, timezone

from video_streaming.security import RequestPrincipal


class RequestPrincipalTests(unittest.TestCase):

    def make_principal(self, **overrides):
        values = {
            "issuer": "https://identity.example.test/",
            "subject": "user-123",
            "tenant_id": "tenant-456",
            "scopes": ["video.jobs:read", "video.jobs:create"],
            "roles": ["operator"],
            "authentication_method": "bearer",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        }
        values.update(overrides)
        return RequestPrincipal(**values)

    def test_normalizes_claims_to_immutable_sets(self):
        principal = self.make_principal(
            scopes=[" video.jobs:read ", "video.jobs:read"],
            roles=["operator"],
        )
        self.assertEqual(principal.scopes, frozenset(["video.jobs:read"]))
        self.assertEqual(principal.roles, frozenset(["operator"]))
        with self.assertRaises(AttributeError):
            principal.scopes.add("video.jobs:admin")

    def test_rejects_missing_identity_fields(self):
        for field in ("issuer", "subject", "tenant_id", "authentication_method"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.make_principal(**{field: " "})

    def test_rejects_scalar_scope_string(self):
        with self.assertRaises(TypeError):
            self.make_principal(scopes="video.jobs:read")

    def test_rejects_naive_expiry(self):
        with self.assertRaises(ValueError):
            self.make_principal(expires_at=datetime(2030, 1, 1))

    def test_expiry_and_scope_helpers(self):
        now = datetime.now(timezone.utc)
        principal = self.make_principal(
            expires_at=now + timedelta(seconds=1),
            scopes=["video.jobs:read"],
        )
        self.assertTrue(principal.has_scope("video.jobs:read"))
        self.assertFalse(principal.has_scope("video.jobs:cancel"))
        self.assertFalse(principal.is_expired(now))
        self.assertTrue(principal.is_expired(now + timedelta(seconds=1)))

    def test_rejects_naive_comparison_clock(self):
        principal = self.make_principal()
        with self.assertRaises(ValueError):
            principal.is_expired(datetime(2030, 1, 1))


if __name__ == "__main__":
    unittest.main()
