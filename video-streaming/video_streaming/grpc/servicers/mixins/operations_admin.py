"""Authenticated administrative and bounded operational-history RPCs."""

import grpc

from video_streaming.grpc.authentication import get_request_principal
from video_streaming.security import AuthorizationError, require_scope
from video_streaming.workflows.service import get_workflow_repository


class OperationsAdminMixin:
    """Expose narrow operator and Job-owner repository operations over gRPC."""

    @staticmethod
    def _operator_principal(context):
        principal = get_request_principal()
        try:
            require_scope(principal, "video.tenant.quota.admin")
        except AuthorizationError:
            context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Quota administration permission is required",
            )
        return principal

    @staticmethod
    def _isoformat(value):
        return value.isoformat() if value is not None else ""

    def provision_tenant_job_quota_policy(self, request, context):
        principal = self._operator_principal(context)
        repository = get_workflow_repository(required=True)
        try:
            result = repository.provision_tenant_job_quota_policy(
                tenant_id=request.tenant_id,
                max_active_jobs=request.max_active_jobs,
                operator_principal=principal,
                reason=request.reason,
            )
            updated_at = self._isoformat(result["updated_at"])
        except AuthorizationError:
            context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Quota administration permission is required",
            )
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        return self.pb2.TenantJobQuotaPolicyResponse(
            tenant_id=result["tenant_id"],
            max_active_jobs=result["max_active_jobs"],
            policy_version=result["policy_version"],
            updated_at=updated_at,
        )

    def list_tenant_job_quota_policies(self, request, context):
        principal = self._operator_principal(context)
        repository = get_workflow_repository(required=True)
        limit = request.limit or 100
        try:
            policies = repository.list_tenant_job_quota_policies(
                principal, limit=limit
            )
        except AuthorizationError:
            context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Quota administration permission is required",
            )
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        return self.pb2.ListTenantJobQuotaPoliciesResponse(
            policies=[
                self.pb2.TenantJobQuotaPolicyResponse(
                    tenant_id=item["tenant_id"],
                    max_active_jobs=item["max_active_jobs"],
                    policy_version=item["policy_version"],
                    updated_at=self._isoformat(item["updated_at"]),
                )
                for item in policies
            ]
        )

    def list_tenant_job_quota_audit_events(self, request, context):
        principal = self._operator_principal(context)
        repository = get_workflow_repository(required=True)
        limit = request.limit or 100
        try:
            events = repository.list_tenant_job_quota_audit_events(
                principal,
                tenant_id=request.tenant_id or None,
                limit=limit,
            )
        except AuthorizationError:
            context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Quota administration permission is required",
            )
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        return self.pb2.ListTenantJobQuotaAuditEventsResponse(
            events=[
                self.pb2.TenantJobQuotaAuditEvent(
                    tenant_id=item["tenant_id"],
                    previous_max_active_jobs=(
                        item["previous_max_active_jobs"] or 0
                    ),
                    previous_limit_available=(
                        item["previous_max_active_jobs"] is not None
                    ),
                    max_active_jobs=item["max_active_jobs"],
                    policy_version=item["policy_version"],
                    operator_issuer=item["operator_issuer"],
                    operator_subject=item["operator_subject"],
                    reason=item["reason"],
                    created_at=self._isoformat(item["created_at"]),
                )
                for item in events
            ]
        )

    def get_webhook_delivery_history(self, request, context):
        principal = get_request_principal()
        repository = get_workflow_repository(required=True)
        limit = request.limit or 50
        try:
            events = repository.list_webhook_delivery_history(
                request.tracking_id, principal, limit=limit
            )
        except AuthorizationError:
            context.abort(
                grpc.StatusCode.NOT_FOUND,
                "Resource not found",
            )
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        return self.pb2.WebhookDeliveryHistoryResponse(
            tracking_id=request.tracking_id,
            events=[
                self.pb2.WebhookDeliveryAttemptEvent(
                    event_type=item["event_type"],
                    occurred_at=self._isoformat(item["occurred_at"]),
                    stage_id=item["stage_id"] or "",
                    attempt_id=item["attempt_id"] or "",
                    delivery_id=item["payload"].get("delivery_id", ""),
                    http_status=item["payload"].get("http_status") or 0,
                    http_status_available=(
                        item["payload"].get("http_status") is not None
                    ),
                    failure_code=item["payload"].get("failure_code", ""),
                )
                for item in events
            ]
        )
