"""Security boundary contracts for authenticated service requests."""

from .authorization import AuthorizationError, authorize_tenant_resource, require_scope
from .principal import RequestPrincipal

__all__ = [
    "AuthorizationError",
    "RequestPrincipal",
    "authorize_tenant_resource",
    "require_scope",
]
