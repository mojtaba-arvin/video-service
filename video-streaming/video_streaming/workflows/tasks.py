"""Celery consumer for transactional durable workflow dispatch.

The task boundary carries only immutable Job/plan/node identifiers. The
production operation registry is loaded from an explicit trusted Python
factory; missing or incomplete configuration fails closed. Dispatch remains
disabled by default until the real handlers and infrastructure acceptance
suite are enabled by operators.
"""
import importlib
import socket
import uuid

from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.workflows.executor import WorkflowNodeExecutor
from video_streaming.workflows.service import get_workflow_repository
from video_streaming.workflows.relay import WorkflowOutboxRelay

DISPATCH_TASK = "video_streaming.workflows.dispatch_plan"
EXECUTE_NODE_TASK = "video_streaming.workflows.execute_node"


def _load_handler_factory():
    target = settings.WORKFLOW_HANDLER_FACTORY
    if not target or ":" not in target:
        raise RuntimeError(
            "WORKFLOW_HANDLER_FACTORY must be configured as module:callable"
        )
    module_name, attribute = target.split(":", 1)
    if not module_name or not attribute:
        raise RuntimeError("WORKFLOW_HANDLER_FACTORY is malformed")
    factory = getattr(importlib.import_module(module_name), attribute, None)
    if not callable(factory):
        raise RuntimeError("WORKFLOW_HANDLER_FACTORY is not callable")
    return factory


def _build_artifact_store():
    import boto3
    bucket = settings.S3_DEFAULT_OUTPUT_BUCKET_NAME
    if not bucket:
        raise RuntimeError("S3_DEFAULT_OUTPUT_BUCKET_NAME is required")
    client = boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL or None,
        aws_access_key_id=settings.S3_ACCESS_KEY_ID or None,
        aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY or None,
        region_name=settings.S3_REGION_NAME or None,
        use_ssl=settings.S3_IS_SECURE,
    )
    from video_streaming.workflows.artifact_store import S3ArtifactStore
    return S3ArtifactStore(client, bucket)


def _build_executor():
    repository = get_workflow_repository()
    factory = _load_handler_factory()
    handlers = factory(
        repository=repository,
        artifact_store=_build_artifact_store(),
    )
    return WorkflowNodeExecutor(
        repository=repository,
        handlers=handlers,
        worker_id="{}:{}".format(socket.gethostname(), uuid.uuid4().hex),
        lease_ttl_seconds=settings.WORKFLOW_ATTEMPT_LEASE_TTL_SECONDS,
    )


def _assert_dispatch_identity(repository, job_id, workflow_id, plan_version,
                              plan_fingerprint):
    snapshot = repository.get_execution_plan(job_id)
    if snapshot is None:
        raise KeyError("durable workflow execution plan not found")
    if (
        snapshot["workflow_id"] != workflow_id
        or snapshot["workflow_version"] != plan_version
        or snapshot["fingerprint"] != plan_fingerprint
    ):
        raise ValueError("durable workflow dispatch identity mismatch")
    return snapshot


@celery_app.task(
    name=DISPATCH_TASK,
    acks_late=True,
    reject_on_worker_lost=True,
)
def dispatch_plan(job_id, workflow_id, plan_version, plan_fingerprint):
    """Fan out idempotent node references for one immutable plan."""
    if not settings.WORKFLOW_DURABLE_DISPATCH_ENABLED:
        raise RuntimeError("durable workflow dispatch is disabled")
    repository = get_workflow_repository()
    snapshot = _assert_dispatch_identity(
        repository, job_id, workflow_id, plan_version, plan_fingerprint
    )
    # Validate the complete operation registry before emitting any child task.
    _build_executor()
    ready_node_ids = repository.get_ready_node_ids(job_id)
    for node_id in ready_node_ids:
        execute_node.apply_async(
            args=(job_id, node_id, workflow_id, plan_version,
                  plan_fingerprint),
        )
    return {"status": "dispatched", "nodes": len(ready_node_ids)}


@celery_app.task(
    name=EXECUTE_NODE_TASK,
    acks_late=True,
    reject_on_worker_lost=True,
)
def execute_node(job_id, node_id, workflow_id, plan_version, plan_fingerprint):
    """Execute one node and re-drive the immutable plan after progress."""
    if not settings.WORKFLOW_DURABLE_DISPATCH_ENABLED:
        raise RuntimeError("durable workflow dispatch is disabled")
    repository = get_workflow_repository()
    snapshot = _assert_dispatch_identity(
        repository, job_id, workflow_id, plan_version, plan_fingerprint
    )
    executor = _build_executor()
    outcome = executor.execute(job_id, node_id)
    if outcome["status"] in ("completed", "already_completed"):
        dispatch_plan.apply_async(
            args=(job_id, workflow_id, plan_version, plan_fingerprint)
        )
    elif outcome["status"] == "retryable_failure":
        execute_node.apply_async(
            args=(job_id, node_id, workflow_id, plan_version, plan_fingerprint),
            countdown=outcome["retry_after_seconds"],
        )
    elif outcome["status"] in ("already_running", "lease_lost"):
        # A duplicate delivery must not acquire a second lease. Recheck after
        # the current lease window so an abandoned Attempt can be recovered.
        execute_node.apply_async(
            args=(job_id, node_id, workflow_id, plan_version, plan_fingerprint),
            countdown=settings.WORKFLOW_ATTEMPT_LEASE_TTL_SECONDS + 1,
        )
    return outcome


@celery_app.task(
    name="video_streaming.workflows.relay_outbox",
    acks_late=True,
    reject_on_worker_lost=True,
)
def relay_workflow_outbox():
    """Publish committed outbox rows only after the full handler registry loads."""
    if not settings.WORKFLOW_DURABLE_DISPATCH_ENABLED:
        return {"status": "disabled"}
    # Validate every handler and required artifact-storage configuration before
    # claiming any outbox row. A bad deployment must not mark dispatch as sent.
    _build_executor()
    repository = get_workflow_repository()
    relay = WorkflowOutboxRelay(
        repository=repository,
        publisher=celery_app,
        owner="{}:{}".format(socket.gethostname(), uuid.uuid4().hex),
        lease_ttl_seconds=30,
        batch_size=25,
    )
    return relay.run_once()

@celery_app.task(
    name="video_streaming.workflows.reconcile_expired_attempts",
    acks_late=True,
    reject_on_worker_lost=True,
)
def reconcile_expired_workflow_attempts():
    """Re-dispatch durable Jobs with expired worker leases.

    The scan is bounded and read-only. Node ownership is still decided by the
    PostgreSQL Attempt claim, so concurrent schedulers and duplicate messages
    cannot authorize duplicate side effects.
    """
    if not settings.WORKFLOW_DURABLE_DISPATCH_ENABLED:
        return {"status": "disabled", "jobs": 0, "dispatched": 0}
    repository = get_workflow_repository()
    job_ids = repository.list_jobs_with_expired_attempts(limit=100)
    dispatched = 0
    for job_id in job_ids:
        snapshot = repository.get_execution_plan(job_id)
        if snapshot is None:
            continue
        ready_node_ids = repository.get_ready_node_ids(job_id)
        if not ready_node_ids:
            continue
        dispatch_plan.apply_async(
            args=(
                job_id,
                snapshot["workflow_id"],
                snapshot["workflow_version"],
                snapshot["fingerprint"],
            ),
            expires=45,
        )
        dispatched += 1
    return {
        "status": "reconciled",
        "jobs": len(job_ids),
        "dispatched": dispatched,
    }
