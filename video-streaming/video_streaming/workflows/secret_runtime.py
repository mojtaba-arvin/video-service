"""Fail-closed KMS and IAM integration for workflow callback secrets.

Call resolve_job_webhook_secret only inside a trusted worker process. The
returned URL exists only in that process's memory and must never be written
to Redis, task arguments, logs, or durable result metadata.
"""
from functools import lru_cache

from video_streaming import settings
from video_streaming.security.secret_store import resolve_secret
from video_streaming.workflows.worker_identity import authenticate_workflow_worker


def _aws_clients():
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("boto3 is required for workflow secret resolution") from exc
    region = settings.WORKFLOW_SECRET_KMS_REGION or None
    return boto3.client("sts", region_name=region), boto3.client(
        "kms", region_name=region
    )


@lru_cache(maxsize=1)
def get_workflow_secret_key_provider():
    key_id = settings.WORKFLOW_SECRET_KMS_KEY_ID
    if not key_id:
        raise RuntimeError("WORKFLOW_SECRET_KMS_KEY_ID is required")
    _, kms = _aws_clients()
    from video_streaming.security.secret_store import AwsKMSKeyProvider
    return AwsKMSKeyProvider(kms, key_id)


def get_verified_workflow_worker_identity():
    prefix = settings.WORKFLOW_SECRET_WORKER_ROLE_ARN_PREFIX
    if not prefix:
        raise RuntimeError("WORKFLOW_SECRET_WORKER_ROLE_ARN_PREFIX is required")
    sts, _ = _aws_clients()
    return authenticate_workflow_worker(sts, prefix)


def _resolve_job_webhook_secret(job_id, secret_id, purpose, repository=None):
    """Resolve one Job-scoped webhook secret after live IAM identity verification."""
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("job_id is required")
    if repository is None:
        from video_streaming.workflows.service import get_workflow_repository
        repository = get_workflow_repository()
    worker_identity = get_verified_workflow_worker_identity()
    envelope = repository.get_job_secret_envelope_for_worker(
        job_id,
        worker_identity,
        purpose,
        secret_id,
    )
    return resolve_secret(
        envelope,
        envelope.context,
        get_workflow_secret_key_provider(),
    )

    
def resolve_job_webhook_secret(job_id, secret_id, repository=None):
    """Resolve one Job-scoped callback URL after live IAM identity verification."""
    return _resolve_job_webhook_secret(
        job_id, secret_id, "webhook.callback", repository=repository
    )


def resolve_job_webhook_signing_secret(job_id, secret_id, repository=None):
    """Resolve one Job-scoped signing key after live IAM identity verification."""
    return _resolve_job_webhook_secret(
        job_id, secret_id, "webhook.signing", repository=repository
    )
