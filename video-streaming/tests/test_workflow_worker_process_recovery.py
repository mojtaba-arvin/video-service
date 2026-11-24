"""OS-process crash/restart acceptance test for durable Celery execution.

The first worker is a separate OS process using the solo pool. It is killed
while a handler owns a committed Attempt. After the lease expires, a second
worker process consumes an explicit redelivery and must recover the abandoned
Attempt exactly once. The redelivery is explicit so this test does not depend
on Redis visibility-timeout timing.
"""
import multiprocessing
import os
import signal
import time
import unittest
import uuid

import redis

from video_streaming import settings
from video_streaming.celery import celery_app
from video_streaming.workflows.repository import WorkflowRepository


def _worker_process(database_url, broker_url, result_backend, queue, marker_prefix):
    """Start a real Celery worker process with an isolated test handler."""
    from unittest.mock import patch

    from video_streaming import settings as child_settings
    from video_streaming.workflows import tasks

    client = redis.Redis.from_url(broker_url)

    def handler(_node, _context):
        invocation = client.incr(marker_prefix + ":invocations")
        if invocation == 1:
            client.set(marker_prefix + ":entered", "1", ex=60)
            while not client.exists(marker_prefix + ":release"):
                time.sleep(0.05)
        return {"validation_report": {"valid": True, "errors": []}}

    handlers = {
        operation: handler
        for operation in __import__(
            "video_streaming.workflows.execution_plan",
            fromlist=["PLAN_OPERATIONS"],
        ).PLAN_OPERATIONS
    }

    repository = WorkflowRepository(
        database_url, create_schema=True, pool_size=2, max_overflow=2
    )
    celery_app.conf.update(
        broker_url=broker_url,
        result_backend=result_backend,
        task_default_queue=queue,
        task_always_eager=False,
        task_ignore_result=False,
        worker_prefetch_multiplier=1,
        task_acks_late=True,
        broker_transport_options={"visibility_timeout": 30},
    )
    try:
        with patch.object(child_settings, "WORKFLOW_DURABLE_DISPATCH_ENABLED", True), \
             patch.object(child_settings, "WORKFLOW_ATTEMPT_LEASE_TTL_SECONDS", 2), \
             patch.object(tasks, "get_workflow_repository", return_value=repository), \
             patch.object(tasks, "_build_artifact_store", return_value=object()), \
             patch.object(tasks, "_load_handler_factory", return_value=lambda **_kw: handlers):
            client.set(marker_prefix + ":ready", "1", ex=60)
            celery_app.worker_main([
                "worker", "--pool=solo", "--concurrency=1",
                "--loglevel=WARNING", "--without-gossip", "--without-mingle",
                "--without-heartbeat", "--queues=" + queue,
            ])
    finally:
        repository.close()
        client.close()


@unittest.skipUnless(
    os.environ.get("WORKFLOW_DATABASE_URL")
    and os.environ.get("WORKFLOW_TEST_BROKER_URL")
    and os.environ.get("WORKFLOW_TEST_RESULT_BACKEND"),
    "PostgreSQL and Redis integration URLs are required",
)
class DurableWorkerProcessRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.database_url = os.environ["WORKFLOW_DATABASE_URL"]
        self.broker_url = os.environ["WORKFLOW_TEST_BROKER_URL"]
        self.result_backend = os.environ["WORKFLOW_TEST_RESULT_BACKEND"]
        self.queue = "durable-process-" + uuid.uuid4().hex
        self.marker = "durable-process-test:" + uuid.uuid4().hex
        self.redis = redis.Redis.from_url(self.broker_url)
        self.repository = WorkflowRepository(
            self.database_url, create_schema=True, pool_size=2, max_overflow=2
        )
        celery_app.conf.update(
            broker_url=self.broker_url,
            result_backend=self.result_backend,
            task_default_queue=self.queue,
            task_always_eager=False,
            task_ignore_result=False,
            worker_prefetch_multiplier=1,
            task_acks_late=True,
        )
        self.workers = []

    def tearDown(self):
        for process in self.workers:
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
        self.repository.close()
        self.redis.delete(
            self.marker + ":ready", self.marker + ":entered",
            self.marker + ":release", self.marker + ":invocations",
        )
        self.redis.close()

    def _start_worker(self, suffix):
        process = multiprocessing.get_context("spawn").Process(
            target=_worker_process,
            args=(
                self.database_url, self.broker_url, self.result_backend,
                self.queue, self.marker,
            ),
            name="durable-celery-" + suffix,
        )
        process.start()
        self.workers.append(process)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.redis.exists(self.marker + ":ready"):
                return process
            if not process.is_alive():
                self.fail("Celery worker process exited during startup")
            time.sleep(0.05)
        self.fail("Celery worker process did not become ready")

    def test_killed_worker_process_is_recovered_by_restarted_process(self):
        from video_streaming.workflows.execution_plan import PLAN_OPERATIONS
        from video_streaming.workflows import tasks

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
                    "initial_delay_seconds": 0,
                    "max_delay_seconds": 1,
                },
                "resource_profile": "control",
            }],
        }
        snapshot = self.repository.create_job(
            job_id=job_id,
            idempotency_key="process-recovery-" + job_id,
            request_hash="d" * 64,
            execution_plan=plan,
            execution_backend="durable_vod_v1",
        )
        # create_job returns the accepted Job metadata; fetch immutable plan
        plan_snapshot = self.repository.get_execution_plan(job_id)
        args = (
            job_id, "root", plan_snapshot["workflow_id"],
            plan_snapshot["workflow_version"], plan_snapshot["fingerprint"],
        )

        first_worker = self._start_worker("first")
        first_delivery = tasks.execute_node.apply_async(args=args, queue=self.queue)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not self.redis.exists(self.marker + ":entered"):
            if not first_worker.is_alive():
                self.fail("first worker exited before entering the handler")
            time.sleep(0.05)
        self.assertTrue(self.redis.exists(self.marker + ":entered"))

        # This is an actual OS process kill while the DB Attempt is RUNNING.
        os.kill(first_worker.pid, signal.SIGKILL)
        first_worker.join(timeout=10)
        self.assertFalse(first_worker.is_alive())
        self.assertEqual(first_worker.exitcode, -signal.SIGKILL)

        # The replacement delivery is intentionally sent after the 2s lease.
        time.sleep(2.5)
        self.redis.delete(self.marker + ":ready")
        second_worker = self._start_worker("replacement")
        second_delivery = tasks.execute_node.apply_async(args=args, queue=self.queue)
        outcome = second_delivery.get(timeout=25)
        self.assertEqual(outcome["status"], "completed")

        deadline = time.monotonic() + 15
        finished = None
        while time.monotonic() < deadline:
            finished = self.repository.get_job_snapshot(job_id)
            if finished and finished["status"] == "FINISHED":
                break
            time.sleep(0.05)
        self.assertIsNotNone(finished)
        self.assertEqual(finished["status"], "FINISHED")
        attempts = finished["stages"][0]["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["status"], "RETRYABLE")
        self.assertEqual(attempts[1]["status"], "SUCCEEDED")
        self.assertEqual(int(self.redis.get(self.marker + ":invocations")), 2)
        self.assertTrue(second_worker.is_alive())
        self.redis.set(self.marker + ":release", "1", ex=5)
        second_worker.terminate()
        second_worker.join(timeout=10)
