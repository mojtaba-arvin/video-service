"""Shared tenant and scope authorization primitives."""

from video_streaming.security.principal import RequestPrincipal


class AuthorizationError(PermissionError):
    """Raised when a principal is not allowed to perform an operation."""


_DENIED_MESSAGE = "Resource not found or access denied"


def require_scope(principal, required_scope, now=None):
    """Require a live authenticated principal with one explicit scope."""
    if not isinstance(principal, RequestPrincipal):
        raise AuthorizationError(_DENIED_MESSAGE)
    if principal.is_expired(now=now):
        raise AuthorizationError(_DENIED_MESSAGE)
    if not required_scope or not principal.has_scope(required_scope):
        raise AuthorizationError(_DENIED_MESSAGE)
    return principal


def authorize_tenant_resource(
        principal,
        owner_tenant_id,
        required_scope,
        now=None):
    """Authorize a resource without revealing cross-tenant or legacy existence.

    Resources without an owner are deliberately inaccessible through this
    tenant-scoped path. Administrative legacy access must be a separate,
    explicit and auditable policy rather than an implicit fallback.
    """
    require_scope(principal, required_scope, now=now)
    if (
            not owner_tenant_id
            or owner_tenant_id != principal.tenant_id):
        raise AuthorizationError(_DENIED_MESSAGE)
    return True
