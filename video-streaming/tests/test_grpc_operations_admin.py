"""Tests for operator quota administration and owner-scoped history RPCs."""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import grpc

from video_streaming.grpc.protos import streaming_pb2
from video_streaming.grpc.servicers.mixins.operations_admin import (
    OperationsAdminMixin,
)
from video_streaming.security import AuthorizationError, RequestPrincipal


class AbortCall(Exception):
    def __init__(self, code, details):
        self.code = code
        self.details = details


class FakeContext:
    def abort(self, code, details):
        raise AbortCall(code, details)


class FakeRepository:
    def __init__(self):
        self.provision_args = None
        self.policy_limit = None
        self.audit_args = None
        self.webhook_args = None

    def provision_tenant_job_quota_policy(self, **kwargs):
        self.provision_args = kwargs
        return {
            "tenant_id": kwargs["tenant_id"],
            "max_active_jobs": kwargs["max_active_jobs"],
            "policy_version": 3,
            "updated_at": datetime(2026, 11, 25, tzinfo=timezone.utc),
        }

    def list_tenant_job_quota_policies(self, principal, limit):
        self.policy_limit = limit
        return [{
            "tenant_id": "tenant-a",
            "max_active_jobs": 12,
            "policy_version": 3,
            "updated_at": datetime(2026, 11, 25, tzinfo=timezone.utc),
        }]

    def list_tenant_job_quota_audit_events(
            self, principal, tenant_id, limit):
        self.audit_args = (tenant_id, limit)
        return [{
            "tenant_id": "tenant-a",
            "previous_max_active_jobs": None,
            "max_active_jobs": 12,
            "policy_version": 1,
            "operator_issuer": principal.issuer,
            "operator_subject": principal.subject,
            "reason": "Initial allocation",
            "created_at": datetime(2026, 11, 25, tzinfo=timezone.utc),
        }]

    def list_webhook_delivery_history(self, tracking_id, principal, limit):
        self.webhook_args = (tracking_id, principal, limit)
        return [{
            "event_type": "WEBHOOK_DELIVERY_ATTEMPT_FAILED",
            "occurred_at": datetime(2026, 11, 25, tzinfo=timezone.utc),
            "stage_id": "stage-1",
            "attempt_id": "attempt-1",
            "payload": {
                "delivery_id": "d" * 64,
                "http_status": 503,
                "failure_code": "HTTP_5XX",
            },
        }]


class OperationsAdminService(OperationsAdminMixin):
    pb2 = streaming_pb2


class OperationsAdminRpcTests(unittest.TestCase):
    def setUp(self):
        self.principal = RequestPrincipal(
            issuer="https://issuer.example.test/",
            subject="operator-1",
            tenant_id="operator-tenant",
            scopes=frozenset(["video.tenant.quota.admin", "video.jobs:read"]),
            roles=frozenset(),
            authentication_method="bearer",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        self.repository = FakeRepository()
        self.service = OperationsAdminService()
        self.context = FakeContext()

    @patch(
        "video_streaming.grpc.servicers.mixins.operations_admin.get_workflow_repository"
    )
    @patch(
        "video_streaming.grpc.servicers.mixins.operations_admin.get_request_principal"
    )
    def test_provision_policy_uses_authenticated_operator_and_reason(
            self, get_principal, get_repository):
        get_principal.return_value = self.principal
        get_repository.return_value = self.repository
        request = streaming_pb2.ProvisionTenantJobQuotaPolicyRequest(
            tenant_id="tenant-a",
            max_active_jobs=12,
            reason="Approved capacity increase",
        )

        response = self.service.provision_tenant_job_quota_policy(
            request, self.context
        )

        self.assertEqual(response.tenant_id, "tenant-a")
        self.assertEqual(response.max_active_jobs, 12)
        self.assertEqual(response.policy_version, 3)
        self.assertEqual(
            self.repository.provision_args["operator_principal"],
            self.principal,
        )
        self.assertEqual(
            self.repository.provision_args["reason"],
            "Approved capacity increase",
        )

    @patch(
        "video_streaming.grpc.servicers.mixins.operations_admin.get_workflow_repository"
    )
    @patch(
        "video_streaming.grpc.servicers.mixins.operations_admin.get_request_principal"
    )
    def test_quota_admin_rpc_fails_closed_without_admin_scope(
            self, get_principal, get_repository):
        unprivileged = RequestPrincipal(
            issuer=self.principal.issuer,
            subject="ordinary-user",
            tenant_id="tenant-a",
            scopes=frozenset(["video.jobs:read"]),
            roles=frozenset(),
            authentication_method="bearer",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        get_principal.return_value = unprivileged
        get_repository.return_value = self.repository
        with self.assertRaises(AbortCall) as caught:
            self.service.list_tenant_job_quota_policies(
                streaming_pb2.ListTenantJobQuotaPoliciesRequest(),
                self.context,
            )
        self.assertEqual(caught.exception.code, grpc.StatusCode.PERMISSION_DENIED)
        self.assertIsNone(self.repository.policy_limit)

    @patch(
        "video_streaming.grpc.servicers.mixins.operations_admin.get_workflow_repository"
    )
    @patch(
        "video_streaming.grpc.servicers.mixins.operations_admin.get_request_principal"
    )
    def test_policy_inventory_and_audit_history_are_bounded(
            self, get_principal, get_repository):
        get_principal.return_value = self.principal
        get_repository.return_value = self.repository

        policies = self.service.list_tenant_job_quota_policies(
            streaming_pb2.ListTenantJobQuotaPoliciesRequest(limit=25),
            self.context,
        )
        self.assertEqual(self.repository.policy_limit, 25)
        self.assertEqual(policies.policies[0].max_active_jobs, 12)

        audit = self.service.list_tenant_job_quota_audit_events(
            streaming_pb2.ListTenantJobQuotaAuditEventsRequest(
                tenant_id="tenant-a", limit=10
            ),
            self.context,
        )
        self.assertEqual(self.repository.audit_args, ("tenant-a", 10))
        self.assertEqual(len(audit.events), 1)
        self.assertFalse(audit.events[0].previous_limit_available)
        self.assertEqual(audit.events[0].reason, "Initial allocation")

    @patch(
        "video_streaming.grpc.servicers.mixins.operations_admin.get_workflow_repository"
    )
    @patch(
        "video_streaming.grpc.servicers.mixins.operations_admin.get_request_principal"
    )
    def test_webhook_history_maps_only_allowlisted_delivery_fields(
            self, get_principal, get_repository):
        get_principal.return_value = self.principal
        get_repository.return_value = self.repository

        response = self.service.get_webhook_delivery_history(
            streaming_pb2.GetWebhookDeliveryHistoryRequest(
                tracking_id="job-1", limit=20
            ),
            self.context,
        )

        self.assertEqual(self.repository.webhook_args[0], "job-1")
        self.assertEqual(self.repository.webhook_args[2], 20)
        event = response.events[0]
        self.assertEqual(event.event_type, "WEBHOOK_DELIVERY_ATTEMPT_FAILED")
        self.assertEqual(event.http_status, 503)
        self.assertTrue(event.http_status_available)
        self.assertEqual(event.failure_code, "HTTP_5XX")
        self.assertEqual(event.delivery_id, "d" * 64)

    @patch(
        "video_streaming.grpc.servicers.mixins.operations_admin.get_workflow_repository"
    )
    @patch(
        "video_streaming.grpc.servicers.mixins.operations_admin.get_request_principal"
    )
    def test_webhook_history_hides_missing_and_foreign_jobs(
            self, get_principal, get_repository):
        get_principal.return_value = self.principal
        repository = self.repository

        def denied(*_args, **_kwargs):
            raise AuthorizationError("Resource not found or access denied")

        repository.list_webhook_delivery_history = denied
        get_repository.return_value = repository
        with self.assertRaises(AbortCall) as caught:
            self.service.get_webhook_delivery_history(
                streaming_pb2.GetWebhookDeliveryHistoryRequest(
                    tracking_id="foreign-job"
                ),
                self.context,
            )
        self.assertEqual(caught.exception.code, grpc.StatusCode.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
