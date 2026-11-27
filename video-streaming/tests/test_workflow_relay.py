"""Broker-boundary tests for at-least-once outbox relay behavior."""

import unittest
from uuid import uuid4

from sqlalchemy import create_engine, select

from video_streaming.workflows.execution_plan import execution_plan_sha256
from video_streaming.workflows.relay import (
    DISPATCH_TASK_NAME,
    WorkflowOutboxRelay,
)
from video_streaming.workflows.repository import (
    JobRecord,
    OutboxRecord,
    WorkflowEventRecord,
    WorkflowRepository,
)


class FakePublisher:
    def __init__(self, fail_after_accept=False):
        self.messages = []
        self.fail_after_accept = fail_after_accept

    def send_task(self, name, **kwargs):
        self.messages.append((name, kwargs))
        if self.fail_after_accept:
            self.fail_after_accept = False
            raise ConnectionError("broker acknowledgement was lost")
        return object()


class WorkflowOutboxRelayTests(unittest.TestCase):
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
            "nodes": [{
                "node_id": "probe-input",
                "operation": "video.input.analyze",
                "stage_key": "probe-input",
                "depends_on": [],
                "parameters": {},
                "retry_policy": {
                    "max_attempts": 3,
                    "backoff": "exponential",
                    "initial_delay_seconds": 1,
                    "max_delay_seconds": 60,
                },
                "resource_profile": "probe",
            }],
        }
        self.repository.create_job(
            job_id=self.job_id,
            idempotency_key="relay-" + self.job_id,
            request_hash="a" * 64,
            execution_plan=self.plan,
            enqueue_dispatch=True,
        )
        with self.repository._session() as session:
            self.outbox_id = session.execute(
                select(OutboxRecord.id).where(
                    OutboxRecord.job_id == self.job_id
                )
            ).scalar_one()

    def tearDown(self):
        self.repository.close()

    def relay(self, publisher, **kwargs):
        return WorkflowOutboxRelay(
            self.repository, publisher, owner="relay-test", **kwargs
        )

    def test_publishes_only_plan_reference_and_acknowledges_outbox(self):
        publisher = FakePublisher()
        result = self.relay(publisher, queue="workflow-control").run_once()
        self.assertEqual(result["published"], 1)
        name, message = publisher.messages[0]
        self.assertEqual(name, DISPATCH_TASK_NAME)
        self.assertEqual(message["args"], [
            self.job_id,
            self.repository.get_execution_plan(self.job_id)["workflow_id"],
            "1",
            execution_plan_sha256(self.plan),
        ])
        self.assertEqual(message["queue"], "workflow-control")
        self.assertEqual(message["task_id"], "workflow-plan-{}".format(self.outbox_id))
        self.assertEqual(set(message), {"args", "task_id", "queue"})
        self.assertNotIn("plan", message)
        self.assertEqual(
            self.repository.get_outbox_snapshot(self.outbox_id)["state"],
            "PUBLISHED",
        )

    def test_crash_after_broker_acceptance_republishes_same_idempotency_reference(self):
        publisher = FakePublisher(fail_after_accept=True)
        relay = self.relay(publisher, retry_base_seconds=1)
        first = relay.run_once()
        self.assertEqual(first["retried"], 1)
        self.assertEqual(
            self.repository.get_outbox_snapshot(self.outbox_id)["state"],
            "PENDING",
        )
        # Move availability forward without sleeping; this simulates retry time.
        from datetime import timedelta
        from video_streaming.workflows.repository import utcnow
        with self.repository._session.begin() as session:
            row = session.get(OutboxRecord, self.outbox_id)
            row.available_at = utcnow() - timedelta(seconds=1)
        second = relay.run_once()
        self.assertEqual(second["published"], 1)
        self.assertEqual(len(publisher.messages), 2)
        self.assertEqual(
            publisher.messages[0][1]["task_id"],
            publisher.messages[1][1]["task_id"],
        )

    def test_duplicate_relay_run_does_not_republish_acknowledged_row(self):
        publisher = FakePublisher()
        relay = self.relay(publisher)
        self.assertEqual(relay.run_once()["published"], 1)
        self.assertEqual(relay.run_once()["claimed"], 0)
        self.assertEqual(len(publisher.messages), 1)

    def test_dead_letter_is_visible_and_operator_replay_is_audited(self):
        publisher = FakePublisher(fail_after_accept=False)
        publisher.send_task = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("broker unavailable")
        )
        relay = self.relay(publisher, max_attempts=1)
        result = relay.run_once()
        self.assertEqual(result["dead"], 1)
        metrics = self.repository.get_outbox_metrics()
        self.assertEqual(metrics["dead"], 1)
        dead_rows = self.repository.list_outbox("DEAD", limit=10)
        self.assertEqual(dead_rows[0]["id"], self.outbox_id)
        self.assertIsNone(metrics["oldest_pending_age_seconds"])

        self.assertTrue(self.repository.replay_dead_outbox(
            self.outbox_id,
            operator_id="operator@example.test",
            reason="Broker recovered; replay approved",
        ))
        snapshot = self.repository.get_outbox_snapshot(self.outbox_id)
        self.assertEqual(snapshot["state"], "PENDING")
        self.assertEqual(snapshot["attempts"], 0)
        with self.repository._session() as session:
            from video_streaming.workflows.repository import WorkflowEventRecord
            event = session.query(WorkflowEventRecord).filter_by(
                event_type="OUTBOX_MANUAL_REPLAY_REQUESTED"
            ).one()
            self.assertEqual(event.payload_json["operator_id"], "operator@example.test")
            self.assertEqual(
                event.payload_json["reason"],
                "Broker recovered; replay approved",
            )

    def test_replay_rejects_non_dead_rows_and_missing_operator_context(self):
        self.assertFalse(self.repository.replay_dead_outbox(
            self.outbox_id, "operator", "not dead yet"
        ))
        with self.assertRaises(ValueError):
            self.repository.replay_dead_outbox(self.outbox_id, "", "reason")

    def test_cancelled_job_is_suppressed_before_broker_publish(self):
        with self.repository._session.begin() as session:
            job = session.get(JobRecord, self.job_id)
            job.state = "CANCELLED"

        publisher = FakePublisher()
        result = self.relay(publisher).run_once()

        self.assertEqual(result["claimed"], 1)
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(result["published"], 0)
        self.assertEqual(publisher.messages, [])
        snapshot = self.repository.get_outbox_snapshot(self.outbox_id)
        self.assertEqual(snapshot["state"], "CANCELLED")
        self.assertEqual(snapshot["last_error"], "job_cancelled_before_dispatch")
        metrics = self.repository.get_outbox_metrics()
        self.assertEqual(metrics["cancelled"], 1)
        self.assertEqual(
            self.repository.list_outbox("CANCELLED", limit=10)[0]["id"],
            self.outbox_id,
        )
        with self.repository._session() as session:
            event = session.query(WorkflowEventRecord).filter_by(
                event_type="OUTBOX_CANCELLED_BEFORE_DISPATCH"
            ).one()
            self.assertEqual(event.payload_json["outbox_id"], self.outbox_id)

    def test_plan_fingerprint_mismatch_is_retried_without_broker_publish(self):
        publisher = FakePublisher()
        with self.repository._session.begin() as session:
            from video_streaming.workflows.repository import WorkflowRecord
            workflow = session.query(WorkflowRecord).filter_by(
                job_id=self.job_id
            ).one()
            plan = dict(workflow.execution_plan_json)
            plan["workflow_version"] = "tampered"
            workflow.execution_plan_json = plan
        result = self.relay(publisher).run_once()
        self.assertEqual(result["retried"], 1)
        self.assertEqual(publisher.messages, [])
        snapshot = self.repository.get_outbox_snapshot(self.outbox_id)
        self.assertEqual(snapshot["state"], "PENDING")
        self.assertEqual(snapshot["last_error"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
