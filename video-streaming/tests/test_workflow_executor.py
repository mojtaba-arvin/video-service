"""Durable node-claim, dependency, cancellation and idempotency tests."""

import unittest
from uuid import uuid4

from sqlalchemy import create_engine

from video_streaming.workflows.execution_plan import PLAN_OPERATIONS
from video_streaming.workflows.executor import WorkflowNodeExecutor
from video_streaming.workflows.repository import WorkflowRepository


VALID = {"valid": True, "errors": []}


def node(node_id, operation, dependencies=None, stage_key=None):
    return {
        "node_id": node_id,
        "operation": operation,
        "stage_key": stage_key or node_id,
        "depends_on": list(dependencies or []),
        "parameters": {},
        "retry_policy": {
            "max_attempts": 3,
            "backoff": "exponential",
            "initial_delay_seconds": 1,
            "max_delay_seconds": 60,
        },
        "resource_profile": "control",
    }


class WorkflowNodeExecutorTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            future=True,
        )
        self.repository = WorkflowRepository(
            None, engine=self.engine, create_schema=True
        )
        self.job_id = str(uuid4())
        self.plan = {
            "schema_version": 1,
            "workflow_name": "video.vod",
            "workflow_version": "1",
            "job_id": self.job_id,
            "nodes": [
                node("check", "video.input.check"),
                node("analyze", "video.input.analyze", ["check"]),
            ],
        }
        self.repository.create_job(
            job_id=self.job_id,
            idempotency_key="executor-" + self.job_id,
            request_hash="b" * 64,
            execution_plan=self.plan,
            execution_backend="durable_vod_v1",
        )

    def tearDown(self):
        self.repository.close()

    @staticmethod
    def handlers(default_handler):
        return {operation: default_handler for operation in PLAN_OPERATIONS}

    def executor(self, handler, worker="worker-1"):
        return WorkflowNodeExecutor(
            self.repository,
            self.handlers(handler),
            worker_id=worker,
            lease_ttl_seconds=30,
        )

    def test_node_claim_enforces_dependencies_and_completes_workflow(self):
        executor = self.executor(lambda _node, _context: {
            "validation_report": dict(VALID),
            "metrics": {"cpu_seconds": 0.01},
        })
        blocked = executor.execute(self.job_id, "analyze")
        self.assertEqual(blocked["status"], "dependencies_pending")

        first = executor.execute(self.job_id, "check")
        self.assertEqual(first["status"], "completed")
        duplicate = executor.execute(self.job_id, "check")
        self.assertEqual(duplicate["status"], "already_completed")

        second = executor.execute(self.job_id, "analyze")
        self.assertEqual(second["status"], "completed")
        snapshot = self.repository.get_job_snapshot(self.job_id)
        self.assertEqual(snapshot["status"], "FINISHED")
        self.assertTrue(all(stage["status"] == "FINISHED" for stage in snapshot["stages"]))

    def test_crash_after_side_effect_reuses_stable_idempotency_key(self):
        seen_keys = set()
        logical_effects = []
        attempts = []

        def handler(_node, context):
            attempts.append(context.idempotency_key)
            if context.idempotency_key not in seen_keys:
                seen_keys.add(context.idempotency_key)
                logical_effects.append(context.idempotency_key)
                raise RuntimeError("worker crashed after external acceptance")
            return {"validation_report": dict(VALID)}

        executor = self.executor(handler)
        first = executor.execute(self.job_id, "check")
        self.assertEqual(first["status"], "retryable_failure")
        second = executor.execute(self.job_id, "check")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(set(attempts)), 1)
        self.assertEqual(len(logical_effects), 1)

    def test_duplicate_delivery_during_live_attempt_does_not_run_handler_twice(self):
        nested_results = []
        calls = []
        executor = None

        def handler(_node, _context):
            calls.append(1)
            nested_results.append(executor.execute(self.job_id, "check"))
            return {"validation_report": dict(VALID)}

        executor = self.executor(handler)
        outer = executor.execute(self.job_id, "check")
        self.assertEqual(outer["status"], "completed")
        self.assertEqual(nested_results[0]["status"], "already_running")
        self.assertEqual(len(calls), 1)

    def test_cancellation_fences_completion_after_handler_returns(self):
        def handler(_node, context):
            context.cancel(reason="operator requested cancellation")
            return {"validation_report": dict(VALID)}

        result = self.executor(handler).execute(self.job_id, "check")
        self.assertEqual(result["status"], "cancelled")
        snapshot = self.repository.get_job_snapshot(self.job_id)
        self.assertEqual(snapshot["status"], "CANCELLED")
        self.assertEqual(snapshot["stages"][0]["status"], "CANCELLED")

    def test_handler_registry_must_cover_every_plan_operation(self):
        with self.assertRaisesRegex(ValueError, "exactly cover"):
            WorkflowNodeExecutor(
                self.repository,
                {"video.input.check": lambda *_: {"validation_report": VALID}},
                worker_id="worker-1",
            )


if __name__ == "__main__":
    unittest.main()
