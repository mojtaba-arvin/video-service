"""Authenticated Streamable HTTP MCP adapter.

The adapter exposes typed operations only. Authentication is delegated to the
same configured OIDC/JWKS verifier as gRPC, and all resource authorization and
mutations remain in shared application services.
"""
import asyncio
import logging
from threading import Lock
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import List, Optional

from video_streaming import settings
from video_streaming.security import (
    AuthorizationError,
    RequestPrincipal,
)
from video_streaming.security.oidc import (
    AuthenticationError,
    OIDCAuthenticator,
)
from video_streaming.workflows.application import (
    CancelJobService,
    GetJobStatusService,
    VideoOperationService,
)
from video_streaming.workflows.domain import (
    IdempotencyConflict,
    TenantQuotaExceeded,
    TenantQuotaPolicyMissing,
)
from video_streaming.workflows.service import get_workflow_repository


logger = logging.getLogger(__name__)


def _principal_from_access_token(access_token):
    if access_token is None:
        raise AuthorizationError("Resource not found or access denied")
    claims = access_token.claims or {}
    identity = claims.get("video_service_principal")
    if not isinstance(identity, dict):
        raise AuthorizationError("Resource not found or access denied")
    try:
        expires_at = datetime.fromtimestamp(
            int(access_token.expires_at), timezone.utc
        )
        principal = RequestPrincipal(
            issuer=identity["issuer"],
            subject=identity["subject"],
            tenant_id=identity["tenant_id"],
            scopes=frozenset(access_token.scopes or ()),
            roles=frozenset(identity.get("roles") or ()),
            authentication_method="bearer",
            expires_at=expires_at,
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        raise AuthorizationError(
            "Resource not found or access denied"
        ) from None
    if principal.is_expired():
        raise AuthorizationError("Resource not found or access denied")
    return principal


def _safe_tool_call(operation, callback):
    try:
        return callback()
    except AuthorizationError:
        raise PermissionError("Resource not found or access denied") from None
    except TenantQuotaExceeded:
        raise ValueError("Tenant Job quota exceeded") from None
    except TenantQuotaPolicyMissing:
        raise ValueError("Tenant Job quota policy is not configured") from None
    except IdempotencyConflict:
        raise ValueError("Idempotency key was already used for a different request") from None
    except (ValueError, PermissionError):
        raise
    except Exception:
        logger.exception("MCP operation failed", extra={"operation": operation})
        raise RuntimeError("The operation could not be completed") from None


def build_server():
    """Build the MCP server; the optional SDK is imported only when requested."""
    from mcp.server import MCPServer
    from mcp.server.auth.provider import AccessToken, TokenVerifier
    from mcp.server.auth.middleware.auth_context import get_access_token
    from mcp.server.auth.settings import AuthSettings
    resource_url = settings.MCP_RESOURCE_URL.strip()
    audience = settings.MCP_OIDC_AUDIENCE.strip()
    if not resource_url or not audience:
        raise RuntimeError(
            "MCP_RESOURCE_URL and MCP_OIDC_AUDIENCE must be configured"
        )
    parsed_resource = urlparse(resource_url)
    if (
            parsed_resource.scheme not in ("https", "http")
            or not parsed_resource.netloc
            or parsed_resource.username
            or parsed_resource.password
            or parsed_resource.query
            or parsed_resource.fragment
            or parsed_resource.path != "/mcp"):
        raise RuntimeError("MCP_RESOURCE_URL must be an absolute HTTP(S) URL")
    if (
            parsed_resource.scheme != "https"
            and parsed_resource.hostname not in ("localhost", "127.0.0.1", "::1")):
        raise RuntimeError("MCP_RESOURCE_URL must use HTTPS outside loopback")

    authenticator = OIDCAuthenticator(
        issuer=settings.OIDC_ISSUER,
        audience=audience,
        jwks_uri=settings.OIDC_JWKS_URI,
        algorithms=settings.OIDC_ALLOWED_ALGORITHMS,
        tenant_claim=settings.OIDC_TENANT_CLAIM,
        subject_claim=settings.OIDC_SUBJECT_CLAIM,
        scopes_claim=settings.OIDC_SCOPES_CLAIM,
        roles_claim=settings.OIDC_ROLES_CLAIM,
        leeway_seconds=settings.OIDC_CLOCK_LEEWAY_SECONDS,
        jwks_timeout_seconds=settings.OIDC_JWKS_TIMEOUT_SECONDS,
    )

    class OIDCResourceTokenVerifier(TokenVerifier):
        async def verify_token(self, token: str):
            try:
                principal = await asyncio.to_thread(
                    authenticator.authenticate, token
                )
            except AuthenticationError:
                return None
            return AccessToken(
                token=token,
                client_id=principal.subject,
                scopes=sorted(principal.scopes),
                expires_at=int(principal.expires_at.timestamp()),
                resource=resource_url,
                subject=principal.subject,
                claims={
                    "video_service_principal": {
                        "issuer": principal.issuer,
                        "subject": principal.subject,
                        "tenant_id": principal.tenant_id,
                        "roles": sorted(principal.roles),
                    },
                },
            )

    mcp = MCPServer(
        "video-service",
        token_verifier=OIDCResourceTokenVerifier(),
        auth=AuthSettings.model_validate({
            "issuer_url": settings.OIDC_ISSUER,
            "resource_server_url": resource_url,
            "required_scopes": [],
            "validate_token_resource": True,
        }),
    )

    def principal():
        return _principal_from_access_token(get_access_token())

    repository_instance = []
    repository_lock = Lock()

    def repository():
        if not repository_instance:
            with repository_lock:
                if not repository_instance:
                    repository_instance.append(
                        get_workflow_repository(required=True)
                    )
        return repository_instance[0]

    def operation_service():
        return VideoOperationService(
            repository(),
            durable_dispatch_enabled=(
                settings.WORKFLOW_DURABLE_DISPATCH_ENABLED
            ),
            quota_enforcement_enabled=(
                settings.TENANT_JOB_QUOTA_ENFORCEMENT_ENABLED
            ),
        )

    @mcp.tool()
    def video_probe(bucket: str, key: str, idempotency_key: str) -> dict:
        """Probe a video and return its asynchronous durable Job ID."""
        return _safe_tool_call(
            "video_probe",
            lambda: operation_service().probe_video(
                bucket, key, idempotency_key, principal()
            ),
        )

    @mcp.tool()
    def video_thumbnail(
            bucket: str,
            key: str,
            output_bucket: str,
            output_key: str,
            thumbnail_time: str,
            idempotency_key: str,
            scale_width: int = 0,
            scale_height: int = 0) -> dict:
        """Generate one thumbnail using typed media settings; returns a Job ID."""
        return _safe_tool_call(
            "video_thumbnail",
            lambda: operation_service().generate_thumbnail(
                bucket, key, output_bucket, output_key, thumbnail_time,
                idempotency_key, principal(), scale_width, scale_height,
            ),
        )

    @mcp.tool()
    def video_transcode(
            bucket: str,
            key: str,
            output_bucket: str,
            output_key: str,
            protocol: str,
            idempotency_key: str,
            quality_names: Optional[List[str]] = None,
            encode_format: str = "h264") -> dict:
        """Create an HLS or DASH transcode Job with a bounded quality profile."""
        return _safe_tool_call(
            "video_transcode",
            lambda: operation_service().transcode_video(
                bucket, key, output_bucket, output_key, protocol,
                idempotency_key, principal(), quality_names, encode_format,
            ),
        )

    @mcp.tool()
    def video_job_get(job_id: str) -> dict:
        """Read a tenant-authorized Job status without exposing artifact URLs."""
        def read_status():
            snapshot = GetJobStatusService(repository()).execute(
                [job_id], principal()
            )[job_id]
            if snapshot is None:
                raise ValueError("Job not found")
            return {
                "job_id": snapshot["id"],
                "status": snapshot["status"],
                "workflow": {
                    "name": snapshot["workflow"]["name"],
                    "status": snapshot["workflow"]["status"],
                },
                "stages": [{
                    "key": stage["key"],
                    "operation": stage["operation"],
                    "status": stage["status"],
                    "attempts": [{
                        "number": attempt["number"],
                        "status": attempt["status"],
                        "error_code": attempt["error_code"],
                    } for attempt in stage["attempts"]],
                } for stage in snapshot["stages"]],
                "events": [{
                    "type": event["type"],
                    "from_state": event["from_state"],
                    "to_state": event["to_state"],
                    "occurred_at": event["occurred_at"],
                } for event in snapshot["events"]],
            }
        return _safe_tool_call("video_job_get", read_status)

    @mcp.tool()
    def video_job_cancel(job_id: str, reason: str = "mcp_operator_request") -> dict:
        """Request durable cancellation; active workers acknowledge their leases."""
        return _safe_tool_call(
            "video_job_cancel",
            lambda: CancelJobService(repository()).execute(
                job_id, principal(), reason=reason
            ).__dict__,
        )

    return mcp


def run():
    """Run only the authenticated Streamable HTTP transport (never stdio)."""
    from mcp.server.transport_security import TransportSecuritySettings

    server = build_server()
    security = TransportSecuritySettings(
        allowed_hosts=list(settings.MCP_ALLOWED_HOSTS),
        allowed_origins=list(settings.MCP_ALLOWED_ORIGINS),
    )
    resource = urlparse(settings.MCP_RESOURCE_URL)
    server.run(
        transport="streamable-http",
        host=settings.MCP_HOST,
        port=settings.MCP_PORT,
        streamable_http_path=resource.path,
        transport_security=security,
    )
