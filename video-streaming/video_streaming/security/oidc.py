"""Configured OIDC/JWKS validation for bearer access tokens.

The issuer and JWKS endpoint are deployment trust anchors. Neither is ever
read from an unverified token or caller-controlled metadata.
"""
from datetime import datetime, timezone
from urllib.parse import urlparse

import jwt

from video_streaming.security import RequestPrincipal


class AuthenticationError(Exception):
    """A credential could not be authenticated safely."""


class OIDCAuthenticator:
    def __init__(
            self, issuer, audience, jwks_uri, algorithms,
            tenant_claim="tenant_id", subject_claim="sub",
            scopes_claim="scope", roles_claim="roles", leeway_seconds=0,
            jwks_timeout_seconds=5):
        self.issuer = self._https_url(issuer, "OIDC_ISSUER")
        self.audience = self._required(audience, "OIDC_AUDIENCE")
        self.jwks_uri = self._https_url(jwks_uri, "OIDC_JWKS_URI")
        self.algorithms = tuple(
            item.strip() for item in algorithms if item and item.strip()
        )
        supported_asymmetric_algorithms = {
            "RS256", "RS384", "RS512",
            "PS256", "PS384", "PS512",
            "ES256", "ES384", "ES512",
            "EdDSA",
        }
        if (
                not self.algorithms
                or any(
                    item not in supported_asymmetric_algorithms
                    for item in self.algorithms
                )):
            raise ValueError(
                "OIDC_ALLOWED_ALGORITHMS must use supported asymmetric algorithms"
            )
        self.tenant_claim = self._required(tenant_claim, "OIDC_TENANT_CLAIM")
        self.subject_claim = self._required(subject_claim, "OIDC_SUBJECT_CLAIM")
        self.scopes_claim = scopes_claim
        self.roles_claim = roles_claim
        if leeway_seconds < 0 or jwks_timeout_seconds <= 0:
            raise ValueError("OIDC timeouts and leeway are invalid")
        self._jwks_client = jwt.PyJWKClient(
            self.jwks_uri,
            cache_jwk_set=True,
            lifespan=300,
            timeout=jwks_timeout_seconds,
        )
        self._leeway_seconds = leeway_seconds

    @staticmethod
    def _required(value, name):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("{} is required".format(name))
        return value.strip()

    @classmethod
    def _https_url(cls, value, name):
        value = cls._required(value, name)
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("{} must be an absolute HTTPS URL".format(name))
        return value

    @staticmethod
    def _claim_values(value):
        if value is None:
            return frozenset()
        if isinstance(value, str):
            return frozenset(part for part in value.split() if part)
        if isinstance(value, (list, tuple, set)):
            if any(not isinstance(part, str) or not part.strip() for part in value):
                raise AuthenticationError("Invalid access token")
            return frozenset(part.strip() for part in value)
        raise AuthenticationError("Invalid access token")

    def authenticate(self, token):
        if not isinstance(token, str) or not token or len(token) > 16384:
            raise AuthenticationError("Invalid access token")
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self.algorithms),
                audience=self.audience,
                issuer=self.issuer,
                leeway=self._leeway_seconds,
                options={"require": ["exp", "iss", "aud", self.subject_claim]},
            )
            subject = claims.get(self.subject_claim)
            tenant_id = claims.get(self.tenant_claim)
            expires_at = datetime.fromtimestamp(claims["exp"], timezone.utc)
            if not isinstance(subject, str) or not subject.strip():
                raise AuthenticationError("Invalid access token")
            if not isinstance(tenant_id, str) or not tenant_id.strip():
                raise AuthenticationError("Invalid access token")
            scopes = self._claim_values(claims.get(self.scopes_claim))
            roles = self._claim_values(claims.get(self.roles_claim))
            return RequestPrincipal(
                issuer=self.issuer,
                subject=subject,
                tenant_id=tenant_id,
                scopes=scopes,
                roles=roles,
                authentication_method="bearer",
                expires_at=expires_at,
            )
        except AuthenticationError:
            raise
        except Exception as exc:
            # Do not expose parser, key, network, or token details to callers.
            raise AuthenticationError("Invalid access token") from exc
