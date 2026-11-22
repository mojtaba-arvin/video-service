"""Real Redis-broker/Celery-worker acceptance tests for durable node dispatch.

These tests run only in the integration CI job, which provisions Redis and
PostgreSQL. They exercise duplicate delivery and retry after an external
side-effect acceptance using the real broker/worker path.
"""
import importlib.util
from concurrent.futures import ThreadPoolExecutor
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
        OutboxRecord,
        WorkflowRepository,
        utcnow,
    )
    from video_streaming.workflows.relay import WorkflowOutboxRelay
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

    def test_outbox_republishes_when_broker_accepts_but_db_ack_fails(self):
        """A real Redis publish followed by a lost DB ack must replay safely."""
        # The integration suite reuses one ephemeral PostgreSQL database.
        # The relay claims all due rows, not only rows for this test Job, so
        # remove prior test deliveries to make the injected first-ack failure
        # apply to the one row whose state this test asserts.
        with self.repository._session.begin() as session:
            session.query(OutboxRecord).delete(synchronize_session=False)

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
                    "initial_delay_seconds": 1,
                    "max_delay_seconds": 10,
                },
                "resource_profile": "control",
            }],
        }
        self.repository.create_job(
            job_id=job_id,
            idempotency_key="outbox-redis-" + job_id,
            request_hash="d" * 64,
            execution_plan=plan,
            enqueue_dispatch=True,
        )
        with self.repository._session() as session:
            outbox_id = session.query(OutboxRecord.id).filter_by(
                job_id=job_id
            ).scalar()

        relay_queue = "outbox-ack-loss-" + uuid.uuid4().hex
        accepted = []
        send_task = celery_app.send_task

        def publish(name, **kwargs):
            # This invokes the configured Celery producer and publishes to
            # the real Redis broker. No worker consumes this isolated queue.
            accepted.append((name, dict(kwargs)))
            return send_task(name, **kwargs)

        mark_published = self.repository.mark_outbox_published
        ack_attempts = []

        def fail_first_database_ack(row_id, lease_token, **kwargs):
            ack_attempts.append(row_id)
            if len(ack_attempts) == 1:
                raise ConnectionError("simulated PostgreSQL ack interruption")
            return mark_published(row_id, lease_token, **kwargs)

        relay = WorkflowOutboxRelay(
            repository=self.repository,
            publisher=type(
                "RedisPublisher", (), {"send_task": staticmethod(publish)}
            )(),
            owner="redis-outbox-test",
            queue=relay_queue,
            retry_base_seconds=1,
        )
        with patch.object(
                self.repository,
                "mark_outbox_published",
                side_effect=fail_first_database_ack):
            first = relay.run_once()
            self.assertEqual(first["claimed"], 1)
            self.assertEqual(first["retried"], 1)
            self.assertEqual(
                self.repository.get_outbox_snapshot(outbox_id)["state"],
                "PENDING",
            )

            with self.repository._session.begin() as session:
                row = session.get(OutboxRecord, outbox_id)
                row.available_at = utcnow() - timedelta(seconds=1)

            second = relay.run_once()

        self.assertEqual(second["published"], 1)
        self.assertEqual(len(accepted), 2)
        self.assertEqual(accepted[0][0], accepted[1][0])
        self.assertEqual(
            accepted[0][1]["task_id"],
            accepted[1][1]["task_id"],
        )
        self.assertEqual(
            accepted[0][1]["args"],
            accepted[1][1]["args"],
        )
        self.assertEqual(accepted[0][1]["queue"], relay_queue)
        self.assertEqual(
            self.repository.get_outbox_snapshot(outbox_id)["state"],
            "PUBLISHED",
        )

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


    def test_concurrent_redeliveries_recover_expired_attempt_once(self):
        """Concurrent deliveries must serialize recovery and create one retry."""
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
        job_id, snapshot = self._create_job(retry_delay=0)
        attempt_id, _number, _token, _expiry = (
            self.repository.claim_node_attempt(
                job_id,
                "root",
                lease_owner="worker-that-was-lost",
                lease_ttl_seconds=30,
            )
        )
        with self.repository._session.begin() as session:
            attempt = session.get(AttemptRecord, attempt_id)
            attempt.lease_expires_at = utcnow() - timedelta(seconds=1)

        args = (
            job_id,
            "root",
            snapshot["workflow_id"],
            snapshot["workflow_version"],
            snapshot["fingerprint"],
        )
        deliveries = [
            tasks.execute_node.apply_async(args=args, queue=self.queue)
            for _ in range(2)
        ]

        try:
            self.assertTrue(
                entered.wait(10),
                "no redelivered worker entered the recovered Attempt",
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not any(
                    delivery.ready() for delivery in deliveries):
                time.sleep(0.02)
            ready = [delivery for delivery in deliveries if delivery.ready()]
            self.assertEqual(
                len(ready), 1,
                "the competing delivery should finish while the sole handler is blocked",
            )
            self.assertEqual(ready[0].get(timeout=1)["status"], "already_running")
        finally:
            release.set()

        outcomes = [delivery.get(timeout=15)["status"] for delivery in deliveries]
        self.assertIn("completed", outcomes)
        self.assertIn("already_running", outcomes)
        finished = self._wait_for_finished(job_id)
        attempts = finished["stages"][0]["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["status"], "RETRYABLE")
        self.assertEqual(attempts[1]["status"], "SUCCEEDED")
        self.assertEqual(len(calls), 1)


    def test_concurrent_claims_recover_expired_attempt_once(self):
        """Concurrent claimers must not create two successors to one lease."""
        job_id, _snapshot = self._create_job(retry_delay=0)
        attempt_id, _number, _token, _expiry = (
            self.repository.claim_node_attempt(
                job_id,
                "root",
                lease_owner="worker-that-was-lost",
                lease_ttl_seconds=30,
            )
        )
        with self.repository._session.begin() as session:
            attempt = session.get(AttemptRecord, attempt_id)
            attempt.lease_expires_at = utcnow() - timedelta(seconds=1)

        start = threading.Barrier(2)

        def claim(worker_name):
            start.wait(timeout=5)
            try:
                claimed_id, number, _token, _expiry = (
                    self.repository.claim_node_attempt(
                        job_id,
                        "root",
                        lease_owner=worker_name,
                        lease_ttl_seconds=30,
                    )
                )
                return ("claimed", claimed_id, number)
            except Exception as exc:
                return ("rejected", type(exc).__name__)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ("recovery-worker-a", "recovery-worker-b")))

        winners = [item for item in results if item[0] == "claimed"]
        losers = [item for item in results if item[0] == "rejected"]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(losers), 1, results)
        self.assertEqual(losers[0][1], "StageAlreadyRunning")

        snapshot = self.repository.get_stage_snapshot(job_id, "root")
        self.assertEqual(len(snapshot["attempts"]), 2)
        self.assertEqual(snapshot["attempts"][0]["status"], "RETRYABLE")
        self.assertEqual(snapshot["attempts"][1]["status"], "RUNNING")


if __name__ == "__main__":
    unittest.main()
