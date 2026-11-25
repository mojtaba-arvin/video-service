"""Durable node-claim, dependency, cancellation and idempotency tests."""

import os
import unittest
from contextlib import contextmanager
from dataclasses import replace
from unittest import mock
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import create_engine, event

from video_streaming.workflows.execution_plan import PLAN_OPERATIONS
from video_streaming.workflows.domain import AttemptLeaseLost, InvalidStateTransition
from video_streaming.workflows.executor import WorkflowNodeExecutor
from video_streaming.workflows.errors import PermanentOperationError
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
        database_url = os.environ.get("WORKFLOW_DATABASE_URL")
        if database_url:
            self.engine = create_engine(
                database_url, pool_pre_ping=True, future=True
            )
        else:
            self.engine = create_engine(
                "sqlite:///:memory:",
                connect_args={"check_same_thread": False},
                future=True,
            )

            @event.listens_for(self.engine, "connect")
            def enable_foreign_keys(connection, _record):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

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

    def test_legacy_tasks_cannot_claim_durable_owned_processing_or_publication(self):
        with self.assertRaises(InvalidStateTransition):
            self.repository.claim_attempt(
                self.job_id, "check", lease_owner="legacy-celery-worker"
            )
        with self.assertRaises(InvalidStateTransition):
            self.repository.claim_publication_attempt(
                self.job_id, "check", lease_owner="legacy-celery-worker"
            )
        result = self.executor(
            lambda _node, _context: {"validation_report": dict(VALID)}
        ).execute(self.job_id, "check")
        self.assertEqual(result["status"], "completed")

    def test_node_claim_enforces_dependencies_and_completes_workflow(self):
        executor = self.executor(lambda _node, _context: {
            "validation_report": dict(VALID),
            "metrics": {"cpu_seconds": 0.01},
        })
        self.assertEqual(
            self.repository.get_ready_node_ids(self.job_id), ["check"]
        )
        blocked = executor.execute(self.job_id, "analyze")
        self.assertEqual(blocked["status"], "dependencies_pending")

        first = executor.execute(self.job_id, "check")
        self.assertEqual(first["status"], "completed")
        self.assertEqual(
            self.repository.get_ready_node_ids(self.job_id), ["analyze"]
        )
        duplicate = executor.execute(self.job_id, "check")
        self.assertEqual(duplicate["status"], "already_completed")

        second = executor.execute(self.job_id, "analyze")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(self.repository.get_ready_node_ids(self.job_id), [])
        snapshot = self.repository.get_job_snapshot(self.job_id)
        self.assertEqual(snapshot["status"], "FINISHED")
        self.assertTrue(all(stage["status"] == "FINISHED" for stage in snapshot["stages"]))

    def test_handler_artifacts_are_persisted_and_passed_to_dependent_node(self):
        observed = {}
        checksum = "a" * 64

        def handler(current_node, context):
            if current_node["node_id"] == "check":
                return {
                    "validation_report": dict(VALID),
                    "artifacts": [{
                        "logical_name": "source-video",
                        "artifact_type": "media_file",
                        "uri": (
                            "s3://media-output/.attempts/"
                            + context.attempt_id + "/source.mp4"
                        ),
                        "media_type": "video/mp4",
                        "size_bytes": 1234,
                        "checksum_sha256": checksum,
                    }],
                }
            observed.update(context.input_artifacts)
            return {"validation_report": dict(VALID)}

        executor = self.executor(handler)
        self.assertEqual(executor.execute(self.job_id, "check")["status"], "completed")
        self.assertEqual(executor.execute(self.job_id, "analyze")["status"], "completed")
        self.assertIn("check", observed)
        self.assertEqual(observed["check"][0]["logical_name"], "source-video")
        self.assertEqual(observed["check"][0]["checksum_sha256"], checksum)
        self.assertTrue(observed["check"][0]["uri"].startswith("s3://"))

    def test_node_cannot_publish_local_paths_as_artifacts(self):
        def handler(_node, _context):
            return {
                "validation_report": dict(VALID),
                "artifacts": [{
                    "logical_name": "local-video",
                    "artifact_type": "media_file",
                    "uri": "/tmp/worker-a/video.mp4",
                    "media_type": "video/mp4",
                    "size_bytes": 10,
                    "checksum_sha256": "b" * 64,
                }],
            }
        result = self.executor(handler).execute(self.job_id, "check")
        self.assertEqual(result["status"], "retryable_failure")
        stage = self.repository.get_stage_snapshot(self.job_id, "check")
        self.assertEqual(stage["status"], "RETRY_WAIT")
        self.assertEqual(stage["artifacts"], [])

    def test_ready_node_scheduler_recovers_only_expired_processing_lease(self):
        _attempt_id, _number, _token, expires_at = (
            self.repository.claim_node_attempt(
                self.job_id, "check", lease_owner="worker-crashed",
                lease_ttl_seconds=30,
            )
        )
        self.assertEqual(self.repository.get_ready_node_ids(self.job_id), [])
        recovered = self.repository.get_ready_node_ids(
            self.job_id, now=expires_at + timedelta(seconds=1)
        )
        self.assertEqual(recovered, ["check"])

    def _make_retry_due(self, job_id):
        from video_streaming.workflows.repository import (
            AttemptRecord, StageRecord, WorkflowRecord, utcnow,
        )

        with self.repository._session.begin() as session:
            attempt = (
                session.query(AttemptRecord)
                .join(StageRecord, StageRecord.id == AttemptRecord.stage_id)
                .join(WorkflowRecord, WorkflowRecord.id == StageRecord.workflow_id)
                .filter(WorkflowRecord.job_id == job_id)
                .order_by(AttemptRecord.attempt_number.desc())
                .first()
            )
            self.assertIsNotNone(attempt)
            attempt.finished_at = utcnow() - timedelta(seconds=10)

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
        self._make_retry_due(self.job_id)
        second = executor.execute(self.job_id, "check")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(set(attempts)), 1)
        self.assertEqual(len(logical_effects), 1)

    def test_operation_retry_contract_and_typed_permanent_failure_are_enforced(self):
        job_id = str(uuid4())
        plan = {
            "schema_version": 1,
            "workflow_name": "video.vod",
            "workflow_version": "1",
            "job_id": job_id,
            "nodes": [node("funnel", "video.inputs.funnel")],
        }
        self.repository.create_job(
            job_id=job_id,
            idempotency_key="never-retry-" + job_id,
            request_hash="e" * 64,
            execution_plan=plan,
            execution_backend="durable_vod_v1",
        )
        never_retry = self.executor(
            lambda _node, _context: (_ for _ in ()).throw(
                RuntimeError("deterministic funnel validation failure")
            )
        ).execute(job_id, "funnel")
        self.assertEqual(never_retry["status"], "failed")

        permanent = self.executor(
            lambda _node, _context: (_ for _ in ()).throw(
                PermanentOperationError("invalid media input")
            )
        ).execute(self.job_id, "check")
        self.assertEqual(permanent["status"], "failed")

    def test_worker_records_contract_version_mismatch_before_handler(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        calls = []
        from video_streaming.workflows import executor as executor_module

        current = executor_module.OPERATION_CONTRACTS["video.input.check"]
        persisted_stage = self.repository.get_stage_snapshot(self.job_id, "check")
        self.assertEqual(persisted_stage["operation_contract_version"], current.version)
        with patch.dict(executor_module.OPERATION_CONTRACTS, {
            "video.input.check": SimpleNamespace(
                version=current.version + 1,
                retry_class=current.retry_class,
            )
        }):
            result = self.executor(
                lambda *_args: calls.append("called")
            ).execute(self.job_id, "check")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "OperationContractVersionMismatch")
        self.assertEqual(calls, [])
        stage = self.repository.get_stage_snapshot(self.job_id, "check")
        self.assertEqual(stage["status"], "FAILED")
        self.assertEqual(stage["attempts"][-1]["status"], "FAILED")

    def test_terminal_job_failure_does_not_schedule_another_retry(self):
        def handler(_node, _context):
            with self.repository._session.begin() as session:
                from video_streaming.workflows.repository import JobRecord
                job = session.get(JobRecord, self.job_id)
                job.state = "FAILED"
            raise RuntimeError("worker failed after sibling job failure")

        result = self.executor(handler).execute(self.job_id, "check")
        self.assertEqual(result["status"], "terminal")
        self.assertEqual(result["job_status"], "FAILED")
        stage = self.repository.get_stage_snapshot(self.job_id, "check")
        self.assertEqual(stage["status"], "CANCELLED")

    def test_retryable_failure_returns_bounded_exponential_delay(self):
        executor = self.executor(
            lambda _node, _context: (_ for _ in ()).throw(RuntimeError("retry"))
        )
        first = executor.execute(self.job_id, "check")
        self.assertEqual(first["status"], "retryable_failure")
        self.assertEqual(first["retry_after_seconds"], 1.0)
        self._make_retry_due(self.job_id)
        second = executor.execute(self.job_id, "check")
        self.assertEqual(second["status"], "retryable_failure")
        self.assertEqual(second["retry_after_seconds"], 2.0)

    def test_fixed_retry_policy_does_not_exponentially_grow(self):
        plan = self.repository.get_execution_plan(self.job_id)["plan"]
        plan["nodes"][0]["retry_policy"].update({
            "backoff": "fixed", "initial_delay_seconds": 7, "max_delay_seconds": 10,
        })
        job_id = str(uuid4())
        plan["job_id"] = job_id
        self.repository.create_job(
            job_id=job_id, idempotency_key="fixed-retry-" + job_id,
            request_hash="d" * 64, execution_plan=plan,
            execution_backend="durable_vod_v1",
        )
        executor = self.executor(
            lambda _node, _context: (_ for _ in ()).throw(RuntimeError("retry"))
        )
        result = executor.execute(job_id, "check")
        self.assertEqual(result["retry_after_seconds"], 7.0)

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

    def test_handler_exception_after_cancellation_is_reported_as_cancelled(self):
        def handler(_node, context):
            context.cancel(reason="operator requested cancellation")
            raise RuntimeError("handler observed cancellation")
        result = self.executor(handler).execute(self.job_id, "check")
        self.assertEqual(result["status"], "cancelled")
        snapshot = self.repository.get_job_snapshot(self.job_id)
        self.assertEqual(snapshot["status"], "CANCELLED")
        self.assertEqual(snapshot["stages"][0]["status"], "CANCELLED")

    def test_cancellation_by_sibling_node_fences_live_worker_completion(self):
        job_id = str(uuid4())
        plan = {
            "schema_version": 1, "workflow_name": "video.vod",
            "workflow_version": "1", "job_id": job_id,
            "nodes": [
                node("active", "video.input.check"),
                node("canceller", "video.output.bucket.check"),
            ],
        }
        self.repository.create_job(
            job_id=job_id, idempotency_key="sibling-cancel-" + job_id,
            request_hash="c" * 64, execution_plan=plan,
            execution_backend="durable_vod_v1",
        )
        other_id, _, other_token, _ = self.repository.claim_node_attempt(
            job_id, "canceller", lease_owner="worker-2", lease_ttl_seconds=30,
        )
        def handler(_node, context):
            self.repository.cancel_attempt(
                job_id, "canceller", other_id,
                reason="sibling requested cancellation", lease_token=other_token,
            )
            self.assertTrue(context.is_cancelled())
            self.assertTrue(context.is_terminal())
            return {"validation_report": dict(VALID)}
        result = self.executor(handler).execute(job_id, "active")
        self.assertEqual(result["status"], "cancelled")
        snapshot = self.repository.get_job_snapshot(job_id)
        self.assertEqual(snapshot["status"], "CANCELLED")
        stages = {stage["key"]: stage for stage in snapshot["stages"]}
        self.assertEqual(stages["active"]["status"], "CANCELLED")
        self.assertEqual(stages["active"]["attempts"][-1]["status"], "CANCELLED")
        self.assertEqual(stages["canceller"]["status"], "CANCELLED")
        self.assertFalse(stages["active"]["artifacts"])

    def test_terminal_sibling_failure_stops_active_node_without_overwriting_failure(self):
        job_id = str(uuid4())
        plan = {
            "schema_version": 1, "workflow_name": "video.vod",
            "workflow_version": "1", "job_id": job_id,
            "nodes": [
                node("active", "video.input.check"),
                node("failing", "video.output.bucket.check"),
            ],
        }
        self.repository.create_job(
            job_id=job_id, idempotency_key="sibling-failure-" + job_id,
            request_hash="f" * 64, execution_plan=plan,
            execution_backend="durable_vod_v1",
        )
        failed_id, _, failed_token, _ = self.repository.claim_node_attempt(
            job_id, "failing", lease_owner="worker-failing", lease_ttl_seconds=30,
        )

        cancellation_checks = []

        def handler(_node, context):
            self.assertTrue(self.repository.fail_attempt(
                job_id, "failing", failed_id,
                retryable=False,
                error_code="SIBLING_FAILED",
                error_message="terminal sibling failure",
                lease_token=failed_token,
            ))
            self.assertTrue(context.is_terminal())
            cancellation_checks.append(context.is_cancelled())
            context.cancel(reason="sibling failed the workflow")
            raise AttemptLeaseLost("workflow is terminal")

        result = self.executor(handler).execute(job_id, "active")
        self.assertEqual(cancellation_checks, [False])
        self.assertEqual(result["status"], "terminal")
        self.assertEqual(result["job_status"], "FAILED")
        snapshot = self.repository.get_job_snapshot(job_id)
        self.assertEqual(snapshot["status"], "FAILED")
        self.assertEqual(snapshot["workflow"]["status"], "FAILED")
        stages = {stage["key"]: stage for stage in snapshot["stages"]}
        self.assertEqual(stages["active"]["status"], "CANCELLED")
        self.assertEqual(stages["failing"]["status"], "FAILED")

    def test_media_attempt_uses_cgroup_and_persists_kernel_usage_metrics(self):
        from video_streaming.workflows import executor as executor_module

        observed = {"active": False}

        class FakeCgroup:
            def usage(self):
                observed["usage_inside_scope"] = observed["active"]
                return {
                    "cpu": {"usage_usec": 2500000},
                    "memory_peak_bytes": 4096,
                }

        @contextmanager
        def fake_scope(attempt_id):
            observed["scope_attempt_id"] = attempt_id
            observed["active"] = True
            try:
                yield FakeCgroup()
            finally:
                observed["active"] = False

        def handler(_node, _context):
            observed["handler_inside_scope"] = observed["active"]
            return {
                "validation_report": dict(VALID),
                "metrics": {"cpu_seconds": 1.0, "peak_memory_bytes": 1024},
            }

        contracts = dict(executor_module.OPERATION_CONTRACTS)
        contracts["video.input.check"] = replace(
            contracts["video.input.check"],
            cancellation="process_group_termination",
        )
        with mock.patch.object(
                executor_module, "OPERATION_CONTRACTS", contracts):
            with mock.patch.object(
                    executor_module, "attempt_cgroup_scope", fake_scope):
                result = self.executor(handler).execute(self.job_id, "check")

        self.assertEqual(result["status"], "completed")
        self.assertTrue(observed["handler_inside_scope"])
        self.assertTrue(observed["usage_inside_scope"])
        self.assertFalse(observed["active"])
        attempt = self.repository.get_job_snapshot(self.job_id)[
            "stages"
        ][0]["attempts"][-1]
        self.assertEqual(observed["scope_attempt_id"], attempt["id"])
        self.assertEqual(attempt["cpu_seconds"], 2.5)
        self.assertEqual(attempt["peak_memory_bytes"], 4096)

    def test_cgroup_counter_read_failure_does_not_change_handler_outcome(self):
        from video_streaming.workflows import executor as executor_module

        class BrokenCgroup:
            def usage(self):
                raise OSError("synthetic counter read failure")

        @contextmanager
        def fake_scope(_attempt_id):
            yield BrokenCgroup()

        contracts = dict(executor_module.OPERATION_CONTRACTS)
        contracts["video.input.check"] = replace(
            contracts["video.input.check"],
            cancellation="process_group_termination",
        )
        with mock.patch.object(
                executor_module, "OPERATION_CONTRACTS", contracts):
            with mock.patch.object(
                    executor_module, "attempt_cgroup_scope", fake_scope):
                result = self.executor(
                    lambda _node, _context: (_ for _ in ()).throw(
                        PermanentOperationError("deterministic media rejection")
                    )
                ).execute(self.job_id, "check")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "PermanentOperationError")
        attempt = self.repository.get_job_snapshot(self.job_id)[
            "stages"
        ][0]["attempts"][-1]
        self.assertEqual(attempt["error_code"], "PermanentOperationError")
        self.assertEqual(
            attempt["metrics"]["cgroup_metrics_status"], "unavailable"
        )
        self.assertEqual(
            attempt["metrics"]["cgroup_metrics_error_code"], "OSError"
        )

    def test_media_attempt_persists_cgroup_usage_when_handler_fails(self):
        from video_streaming.workflows import executor as executor_module

        class FakeCgroup:
            def usage(self):
                return {
                    "cpu": {"usage_usec": 750000},
                    "memory_peak_bytes": 8192,
                }

        @contextmanager
        def fake_scope(_attempt_id):
            yield FakeCgroup()

        contracts = dict(executor_module.OPERATION_CONTRACTS)
        contracts["video.input.check"] = replace(
            contracts["video.input.check"],
            cancellation="process_group_termination",
        )
        with mock.patch.object(
                executor_module, "OPERATION_CONTRACTS", contracts):
            with mock.patch.object(
                    executor_module, "attempt_cgroup_scope", fake_scope):
                result = self.executor(
                    lambda _node, _context: (_ for _ in ()).throw(
                        RuntimeError("synthetic media failure")
                    )
                ).execute(self.job_id, "check")

        self.assertEqual(result["status"], "retryable_failure")
        attempt = self.repository.get_job_snapshot(self.job_id)[
            "stages"
        ][0]["attempts"][-1]
        self.assertEqual(attempt["cpu_seconds"], 0.75)
        self.assertEqual(attempt["peak_memory_bytes"], 8192)

    def test_handler_registry_must_cover_every_plan_operation(self):
        with self.assertRaisesRegex(ValueError, "exactly cover"):
            WorkflowNodeExecutor(
                self.repository,
                {"video.input.check": lambda *_: {"validation_report": VALID}},
                worker_id="worker-1",
            )


if __name__ == "__main__":
    unittest.main()
