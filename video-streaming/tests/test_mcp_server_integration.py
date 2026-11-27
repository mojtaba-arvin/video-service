"""In-memory MCP protocol tests; the HTTP deployment remains bearer-only."""

import asyncio
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from video_streaming import settings
from video_streaming.security import RequestPrincipal
from video_streaming.mcp import server as mcp_server


MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None


class MCPFakeRepository:
    def __init__(self):
        self.commands = []
        self.cancellations = []

    def find_idempotent_job(self, key, request_hash, owner_principal=None):
        return None

    def create_job(self, **kwargs):
        self.commands.append(kwargs)
        return kwargs["job_id"], True

    def request_job_cancellation(self, job_id, principal, reason):
        self.cancellations.append((job_id, principal.subject, reason))
        return {
            "job_id": job_id,
            "status": "CANCEL_REQUESTED",
            "requested": True,
        }


@unittest.skipUnless(MCP_AVAILABLE, "optional MCP SDK is installed in Python 3.12 CI")
class MCPServerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.repository = MCPFakeRepository()
        self.owner = RequestPrincipal(
            issuer="https://issuer.example.test/",
            subject="mcp-integration-user",
            tenant_id="mcp-integration-tenant",
            scopes=frozenset([
                "video.jobs:create", "video.jobs:read", "video.jobs:cancel",
            ]),
            roles=frozenset(),
            authentication_method="bearer",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        self.token = SimpleNamespace(
            token="opaque-test-token",
            client_id=self.owner.subject,
            scopes=sorted(self.owner.scopes),
            expires_at=int(self.owner.expires_at.timestamp()),
            resource="http://127.0.0.1:3000/mcp",
            subject=self.owner.subject,
            claims={
                "video_service_principal": {
                    "issuer": self.owner.issuer,
                    "subject": self.owner.subject,
                    "tenant_id": self.owner.tenant_id,
                    "roles": [],
                },
            },
        )

    def configure(self):
        settings.MCP_RESOURCE_URL = "http://127.0.0.1:3000/mcp"
        settings.MCP_OIDC_AUDIENCE = settings.MCP_RESOURCE_URL
        settings.OIDC_ISSUER = "https://issuer.example.test/"
        settings.OIDC_JWKS_URI = "https://issuer.example.test/jwks"
        settings.OIDC_ALLOWED_ALGORITHMS = ("RS256",)
        settings.OIDC_TENANT_CLAIM = "tenant_id"
        settings.OIDC_SUBJECT_CLAIM = "sub"
        settings.OIDC_SCOPES_CLAIM = "scope"
        settings.OIDC_ROLES_CLAIM = "roles"
        settings.OIDC_CLOCK_LEEWAY_SECONDS = 0
        settings.OIDC_JWKS_TIMEOUT_SECONDS = 5
        settings.WORKFLOW_DURABLE_DISPATCH_ENABLED = True
        settings.TENANT_JOB_QUOTA_ENFORCEMENT_ENABLED = True

    def test_tools_are_typed_and_delegate_to_shared_application_services(self):
        from mcp import Client
        from mcp.server.auth.middleware import auth_context

        self.configure()

        class FakeAuthenticator:
            def __init__(self, **_kwargs):
                pass

            def authenticate(self, _token):
                return self.owner

        # The in-memory SDK client intentionally bypasses HTTP auth middleware.
        # Inject the verified AccessToken at the same context boundary used by
        # the HTTP resource-server middleware.
        with patch.object(mcp_server, "OIDCAuthenticator", FakeAuthenticator), \
                patch.object(
                    mcp_server, "get_workflow_repository",
                    return_value=self.repository,
                ), patch.object(
                    auth_context, "get_access_token",
                    return_value=self.token,
                ):
            server = mcp_server.build_server()

            async def exercise():
                async with Client(server) as client:
                    tools = await client.list_tools()
                    names = {tool.name for tool in tools.tools}
                    self.assertEqual(names, {
                        "video_probe",
                        "video_thumbnail",
                        "video_transcode",
                        "video_job_get",
                        "video_job_cancel",
                    })
                    for tool in tools.tools:
                        schema = tool.input_schema
                        properties = schema.get("properties", {})
                        self.assertNotIn("command", properties)
                        self.assertNotIn("ffmpeg_args", properties)
                        self.assertNotIn("provider_credentials", properties)

                    submitted = await client.call_tool(
                        "video_probe",
                        {
                            "bucket": "input-bucket",
                            "key": "media/source.mp4",
                            "idempotency_key": "mcp-integration-" + str(uuid4()),
                        },
                    )
                    self.assertFalse(submitted.is_error)
                    self.assertEqual(
                        submitted.structured_content["submission_status"],
                        "accepted",
                    )
                    job_id = submitted.structured_content["job_id"]
                    self.assertEqual(
                        self.repository.commands[-1]["execution_backend"],
                        "durable_vod_v1",
                    )

                    cancelled = await client.call_tool(
                        "video_job_cancel",
                        {"job_id": job_id, "reason": "integration cancellation"},
                    )
                    self.assertFalse(cancelled.is_error)
                    self.assertEqual(
                        cancelled.structured_content["status"],
                        "CANCEL_REQUESTED",
                    )
                    self.assertEqual(
                        self.repository.cancellations[-1][0], job_id
                    )

            asyncio.run(exercise())

    def test_tool_scope_is_enforced_by_shared_service(self):
        from mcp import Client
        from mcp.server.auth.middleware import auth_context

        self.configure()
        token_without_create = SimpleNamespace(
            claims=self.token.claims,
            scopes=["video.jobs:read"],
            expires_at=self.token.expires_at,
        )

        class FakeAuthenticator:
            def __init__(self, **_kwargs):
                pass

            def authenticate(self, _token):
                return self.owner

        with patch.object(mcp_server, "OIDCAuthenticator", FakeAuthenticator), \
                patch.object(
                    mcp_server, "get_workflow_repository",
                    return_value=self.repository,
                ), patch.object(
                    auth_context, "get_access_token",
                    return_value=token_without_create,
                ):
            server = mcp_server.build_server()

            async def exercise():
                async with Client(server) as client:
                    result = await client.call_tool(
                        "video_probe",
                        {
                            "bucket": "input-bucket",
                            "key": "media/source.mp4",
                            "idempotency_key": "scope-denied-" + str(uuid4()),
                        },
                    )
                    self.assertTrue(result.is_error)
                    self.assertFalse(self.repository.commands)

            asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
