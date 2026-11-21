"""IAM-authenticated identity for internal workflow secret resolution.

This is a workload identity, not a user principal. It must be created by
authenticate_workflow_worker(), which verifies the live AWS STS identity
against an operator-configured role ARN prefix. Never deserialize this value
from a Celery message, HTTP request, or Job metadata.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowWorkerIdentity:
    account_id: str
    arn: str
    role_arn_prefix: str

    def __post_init__(self):
        for field in ("account_id", "arn", "role_arn_prefix"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("{} is required".format(field))
        if not self.arn.startswith(self.role_arn_prefix):
            raise ValueError("worker identity is outside the allowed IAM role")


def authenticate_workflow_worker(sts_client, allowed_role_arn_prefix):
    """Verify the worker's actual AWS credentials using STS GetCallerIdentity."""
    if sts_client is None:
        raise ValueError("STS client is required")
    if (
        not isinstance(allowed_role_arn_prefix, str)
        or not allowed_role_arn_prefix.startswith("arn:")
        or not allowed_role_arn_prefix.endswith("/")
    ):
        raise ValueError("an exact assumed-role ARN prefix is required")
    try:
        identity = sts_client.get_caller_identity()
    except Exception as exc:
        raise PermissionError("workflow worker identity verification failed") from exc
    account = identity.get("Account")
    arn = identity.get("Arn")
    if not isinstance(account, str) or not account.isdigit():
        raise PermissionError("STS returned an invalid account identity")
    if not isinstance(arn, str) or not arn.startswith(allowed_role_arn_prefix):
        raise PermissionError("workflow worker role is not authorized")
    return WorkflowWorkerIdentity(
        account_id=account,
        arn=arn,
        role_arn_prefix=allowed_role_arn_prefix,
    )
