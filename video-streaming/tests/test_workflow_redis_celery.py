"""Real Redis-broker/Celery-worker acceptance tests for durable node dispatch.

These tests run only in the integration CI job, which provisions Redis and
PostgreSQL. They exercise duplicate delivery and retry after an external
side-effect acceptance using the real broker/worker path.
"""
import importlib.util
import os
import threading
import time
import unittest
from datetime import timedelta
import uuid
from unittest.mock import patch

if importlib.util.find_spec("celery") and importlib.util.find_spec("redis"):
    from celery.contrib.testing.worker import start_worker
    from video_streaming import settings
    from video_streaming.celery import celery_app
    from video_streaming.workflows import tasks
    from video_streaming.workflows.execution_plan import PLAN_OPERATIONS
    from video_streaming.workflows.repository import (
        AttemptRecord,
        WorkflowRepository,
        utcnow,
    )
else:
    start_worker = None
    settings = None
    celery_app = None
    tasks = None
    PLAN_OPERATIONS = None
    WorkflowRepository = None


@unittest.skipUnless(
    importlib.util.find_spec("celery") and importlib.util.find_spec("redis"),
    "Celery and Redis are installed only in the integration environment",
)
class DurableWorkflowRedisCeleryTests(unittest.TestCase):
    def setUp(self):
        database_url = os.environ.get("WORKFLOW_DATABASE_URL")
        broker_url = os.environ.get("WORKFLOW_TEST_BROKER_URL")
        result_backend = os.environ.get("WORKFLOW_TEST_RESULT_BACKEND")
        if not database_url or not broker_url or not result_backend:
            self.skipTest("PostgreSQL and Redis integration URLs are required")

        self.repository = WorkflowRepository(
            database_url, create_schema=True, pool_size=2, max_overflow=2
        )
        self.queue = "durable-vod-test-{}".format(uuid.uuid4().hex)
        celery_app.conf.update(
            broker_url=broker_url,
            result_backend=result_backend,
            task_default_queue=self.queue,
            task_always_eager=False,
            task_ignore_result=False,
            task_store_eager_result=True,
            worker_prefetch_multiplier=1,
            task_acks_late=True,
        )
        self.worker = start_worker(
            celery_app,
            pool="threads",
            concurrency=2,
            queues=[self.queue],
            perform_ping_check=False,
        )
        self.worker.__enter__()
        self._patches = [
            patch.object(settings, "WORKFLOW_DURABLE_DISPATCH_ENABLED", True),
            patch.object(tasks, "get_workflow_repository",
                         return_value=self.repository),
            patch.object(tasks, "_build_artifact_store", return_value=object()),
        ]
        for item in self._patches:
            item.start()
        self.factory_patch = patch.object(
            tasks,
            "_load_handler_factory",
            return_value=lambda **_kwargs: self.handlers,
        )
        self.factory_patch.start()

    def tearDown(self):
        if hasattr(self, "factory_patch"):
            self.factory_patch.stop()
        for item in getattr(self, "_patches", []):
            item.stop()
        if hasattr(self, "worker"):
            self.worker.__exit__(None, None, None)
        if hasattr(self, "repository"):
            self.repository.close()

    def _create_job(self, retry_delay=0.2):
        job_id = str(uuid.uuid4())
        plan = {
            "schema_version": 1,
            "workflow_name": "video.vod",
            "workflow_version": "1",
            "job_id": job_id,
            "nodes": [{
                "node_id": "root",
                "operation": "video.input.check",
                "stage_key": "root",
                "depends_on": [],
                "parameters": {},
                "retry_policy": {
                    "max_attempts": 3,
                    "backoff": "exponential",
                    "initial_delay_seconds": retry_delay,
                    "max_delay_seconds": 1,
                },
                "resource_profile": "control",
            }],
        }
        self.repository.create_job(
            job_id=job_id,
            idempotency_key="redis-celery-" + job_id,
            request_hash="c" * 64,
            execution_plan=plan,
            execution_backend="durable_vod_v1",
        )
        return job_id, self.repository.get_execution_plan(job_id)

    def _wait_for_finished(self, job_id, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.repository.get_job_snapshot(job_id)
            if snapshot and snapshot["status"] == "FINISHED":
                return snapshot
            time.sleep(0.05)
        self.fail("durable Job did not finish before timeout")

    def test_duplicate_delivery_uses_one_attempt_owner(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def handler(_node, _context):
            calls.append(1)
            entered.set()
            if not release.wait(10):
                raise TimeoutError("test handler release timed out")
            return {"validation_report": {"valid": True, "errors": []}}

        self.handlers = {
            operation: handler for operation in PLAN_OPERATIONS
        }
        job_id, snapshot = self._create_job()
        args = (
            job_id,
            snapshot["workflow_id"],
            snapshot["workflow_version"],
            snapshot["fingerprint"],
        )

        dispatch = tasks.dispatch_plan.apply_async(args=args, queue=self.queue)
        dispatch.get(timeout=10)
        self.assertTrue(entered.wait(10), "first node did not enter its handler")

        duplicate = tasks.execute_node.apply_async(
            args=(job_id, "root") + args[1:],
            queue=self.queue,
        )
        duplicate_result = duplicate.get(timeout=10)
        self.assertEqual(duplicate_result["status"], "already_running")
        self.assertEqual(len(calls), 1)

        release.set()
        snapshot = self._wait_for_finished(job_id)
        self.assertEqual(snapshot["stages"][0]["status"], "FINISHED")
        self.assertEqual(len(snapshot["stages"][0]["attempts"]), 1)
        self.assertEqual(len(calls), 1)

    def test_retry_after_external_acceptance_reuses_idempotency_key(self):
        accepted_effects = set()
        handler_keys = []

        def handler(_node, context):
            handler_keys.append(context.idempotency_key)
            if context.idempotency_key not in accepted_effects:
                accepted_effects.add(context.idempotency_key)
                # Simulate a worker-side failure after the external system
                # accepted the idempotent effect but before DB completion.
                raise RuntimeError("simulated worker interruption")
            return {"validation_report": {"valid": True, "errors": []}}

        self.handlers = {
            operation: handler for operation in PLAN_OPERATIONS
        }
        job_id, snapshot = self._create_job()
        dispatch = tasks.dispatch_plan.apply_async(
            args=(
                job_id,
                snapshot["workflow_id"],
                snapshot["workflow_version"],
                snapshot["fingerprint"],
            ),
            queue=self.queue,
        )
        dispatch.get(timeout=10)
        finished = self._wait_for_finished(job_id)
        attempts = finished["stages"][0]["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(handler_keys), 2)
        self.assertEqual(len(set(handler_keys)), 1)
        self.assertEqual(len(accepted_effects), 1)


    def test_redelivery_recovers_an_expired_attempt_after_worker_loss(self):
        """A redelivered node task must fence an abandoned lease and retry."""
        calls = []

        def handler(_node, _context):
            calls.append(1)
            return {"validation_report": {"valid": True, "errors": []}}

        self.handlers = {
            operation: handler for operation in PLAN_OPERATIONS
        }
        job_id, snapshot = self._create_job(retry_delay=0)
        # Model abrupt worker loss after the database claim committed but
        # before the handler could begin. The original broker delivery is
        # assumed lost; a later delivery must recover the expired lease.
        attempt_id, _number, _token, _expiry = (
            self.repository.claim_node_attempt(
                job_id,
                "root",
                lease_owner="worker-that-was-killed",
                lease_ttl_seconds=30,
            )
        )
        with self.repository._session.begin() as session:
            attempt = session.get(AttemptRecord, attempt_id)
            attempt.lease_expires_at = utcnow() - timedelta(seconds=1)

        result = tasks.execute_node.apply_async(
            args=(
                job_id,
                "root",
                snapshot["workflow_id"],
                snapshot["workflow_version"],
                snapshot["fingerprint"],
            ),
            queue=self.queue,
        ).get(timeout=15)

        self.assertEqual(result["status"], "completed")
        finished = self._wait_for_finished(job_id)
        attempts = finished["stages"][0]["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["status"], "RETRYABLE")
        self.assertEqual(attempts[1]["status"], "SUCCEEDED")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
