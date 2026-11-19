"""Immutable identity established by a trusted request authenticator."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import FrozenSet, Iterable


@dataclass(frozen=True)
class RequestPrincipal:
    """Verified caller identity passed from transport to application services.

    Values must be derived by a trusted authenticator (for example, a
    configured OIDC/JWKS verifier), never copied from arbitrary request
    metadata. This value object does not authenticate tokens by itself.
    """

    issuer: str
    subject: str
    tenant_id: str
    scopes: FrozenSet[str]
    roles: FrozenSet[str]
    authentication_method: str
    expires_at: datetime

    def __post_init__(self):
        for name in ("issuer", "subject", "tenant_id", "authentication_method"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("{} must be a non-empty string".format(name))

        if not isinstance(self.expires_at, datetime):
            raise TypeError("expires_at must be a datetime")
        if (
                self.expires_at.tzinfo is None
                or self.expires_at.utcoffset() is None):
            raise ValueError("expires_at must be timezone-aware")

        object.__setattr__(
            self, "scopes", self._normalize_claim_values(self.scopes, "scopes")
        )
        object.__setattr__(
            self, "roles", self._normalize_claim_values(self.roles, "roles")
        )

    @staticmethod
    def _normalize_claim_values(values: Iterable[str], field_name: str):
        if values is None:
            return frozenset()
        if isinstance(values, str):
            raise TypeError("{} must be a collection of strings".format(field_name))
        normalized = set()
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    "{} must contain only non-empty strings".format(field_name)
                )
            normalized.add(value.strip())
        return frozenset(normalized)

    def is_expired(self, now=None):
        """Return whether the verified credential has expired."""
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return current >= self.expires_at

    def has_scope(self, scope):
        """Check one normalized permission scope."""
        return scope in self.scopes
