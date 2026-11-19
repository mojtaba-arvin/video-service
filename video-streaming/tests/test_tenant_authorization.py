"""Tests for shared tenant-scoped authorization primitives."""

import unittest
from datetime import datetime, timedelta, timezone

from video_streaming.security import (
    AuthorizationError,
    RequestPrincipal,
    authorize_tenant_resource,
    require_scope,
)


class TenantAuthorizationTests(unittest.TestCase):

    def setUp(self):
        self.principal = RequestPrincipal(
            issuer="https://issuer.example.test/",
            subject="subject-1",
            tenant_id="tenant-a",
            scopes=frozenset(["video.jobs:read", "video.jobs:create"]),
            roles=frozenset(),
            authentication_method="bearer",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    def test_allows_same_tenant_with_required_scope(self):
        self.assertTrue(authorize_tenant_resource(
            self.principal, "tenant-a", "video.jobs:read"
        ))

    def test_rejects_cross_tenant_without_enumerating_resource(self):
        with self.assertRaises(AuthorizationError) as context:
            authorize_tenant_resource(
                self.principal, "tenant-b", "video.jobs:read"
            )
        self.assertEqual(
            str(context.exception), "Resource not found or access denied"
        )

    def test_rejects_legacy_unowned_resource(self):
        with self.assertRaises(AuthorizationError) as context:
            authorize_tenant_resource(
                self.principal, None, "video.jobs:read"
            )
        self.assertEqual(
            str(context.exception), "Resource not found or access denied"
        )

    def test_rejects_missing_scope(self):
        with self.assertRaises(AuthorizationError):
            require_scope(self.principal, "video.jobs:cancel")

    def test_rejects_expired_principal(self):
        expired = RequestPrincipal(
            issuer=self.principal.issuer,
            subject=self.principal.subject,
            tenant_id=self.principal.tenant_id,
            scopes=self.principal.scopes,
            roles=self.principal.roles,
            authentication_method=self.principal.authentication_method,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        with self.assertRaises(AuthorizationError):
            require_scope(expired, "video.jobs:read")


if __name__ == "__main__":
    unittest.main()
