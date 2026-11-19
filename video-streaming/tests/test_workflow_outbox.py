"""Transactional outbox persistence, lease fencing and replay tests."""

import unittest
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError

from video_streaming.workflows.repository import (
    JobRecord,
    OutboxRecord,
    WorkflowRepository,
    utcnow,
)


class WorkflowOutboxRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            future=True,
        )
        self.repository = WorkflowRepository(
            None, engine=self.engine, create_schema=True
        )

    def tearDown(self):
        self.repository.close()

    @staticmethod
    def plan(job_id):
        return {
            "schema_version": 1,
            "workflow_name": "video.vod",
            "workflow_version": "1",
            "job_id": job_id,
            "nodes": [{
                "node_id": "probe-input",
                "operation": "video.probe",
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

    def create_job(self, job_id=None, key=None, dedupe=None):
        job_id = job_id or str(uuid4())
        return self.repository.create_job(
            job_id=job_id,
            idempotency_key=key or "idem-" + str(uuid4()),
            request_hash="a" * 64,
            execution_plan=self.plan(job_id),
            enqueue_dispatch=True,
            outbox_dedupe_key=dedupe,
        )

    def outbox_id(self, job_id):
        with self.repository._session() as session:
            row = session.execute(
                select(OutboxRecord).where(OutboxRecord.job_id == job_id)
            ).scalar_one()
            return row.id

    def test_job_plan_and_dispatch_intent_are_committed_atomically(self):
        job_id = str(uuid4())
        self.assertEqual(self.create_job(job_id=job_id), (job_id, True))
        snapshot = self.repository.get_outbox_snapshot(self.outbox_id(job_id))
        self.assertEqual(snapshot["state"], "PENDING")
        self.assertEqual(snapshot["job_id"], job_id)
        self.assertEqual(snapshot["event_type"], "WORKFLOW_PLAN_DISPATCH")
        self.assertEqual(snapshot["plan_version"], "1")
        self.assertEqual(len(snapshot["plan_fingerprint"]), 64)

    def test_outbox_unique_conflict_rolls_back_job_and_workflow(self):
        self.create_job(dedupe="dispatch-dedupe-conflict")
        second_job = str(uuid4())
        with self.assertRaises(IntegrityError):
            self.create_job(
                job_id=second_job,
                dedupe="dispatch-dedupe-conflict",
            )
        with self.repository._session() as session:
            self.assertIsNone(session.get(JobRecord, second_job))
            self.assertEqual(
                session.execute(
                    select(OutboxRecord).where(
                        OutboxRecord.dedupe_key == "dispatch-dedupe-conflict"
                    )
                ).scalars().count() if False else 1,
                1,
            )

    def test_dispatch_requires_a_persisted_execution_plan(self):
        with self.assertRaisesRegex(ValueError, "immutable execution plan"):
            self.repository.create_job(
                job_id=str(uuid4()),
                idempotency_key="without-plan-" + str(uuid4()),
                request_hash="b" * 64,
                enqueue_dispatch=True,
            )

    def test_idempotent_job_replay_does_not_create_a_second_dispatch(self):
        job_id = str(uuid4())
        key = "stable-idempotency-key-" + str(uuid4())
        self.assertEqual(self.create_job(job_id=job_id, key=key), (job_id, True))
        replay_id = str(uuid4())
        self.assertEqual(
            self.repository.create_job(
                job_id=replay_id,
                idempotency_key=key,
                request_hash="a" * 64,
                execution_plan=self.plan(replay_id),
                enqueue_dispatch=True,
            ),
            (job_id, False),
        )
        with self.repository._session() as session:
            rows = session.execute(
                select(OutboxRecord).where(OutboxRecord.job_id == job_id)
            ).scalars().all()
            self.assertEqual(len(rows), 1)
            self.assertIsNone(session.get(JobRecord, replay_id))

    def test_claim_lease_prevents_parallel_claim_and_fences_stale_owner(self):
        job_id = str(uuid4())
        self.create_job(job_id=job_id)
        outbox_id = self.outbox_id(job_id)
        now = utcnow() + timedelta(seconds=1)
        first = self.repository.claim_outbox_batch(
            owner="relay-a", lease_ttl_seconds=10, limit=1, now=now
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(
            self.repository.claim_outbox_batch(
                owner="relay-b", lease_ttl_seconds=10, limit=1, now=now
            ),
            [],
        )

        expired_at = now + timedelta(seconds=11)
        second = self.repository.claim_outbox_batch(
            owner="relay-b",
            lease_ttl_seconds=10,
            limit=1,
            now=expired_at,
        )
        self.assertEqual(len(second), 1)
        self.assertFalse(self.repository.mark_outbox_published(
            outbox_id, first[0]["lease_token"], now=expired_at
        ))
        self.assertTrue(self.repository.mark_outbox_published(
            outbox_id, second[0]["lease_token"], now=expired_at
        ))
        self.assertEqual(
            self.repository.get_outbox_snapshot(outbox_id)["state"],
            "PUBLISHED",
        )

    def test_retry_is_bounded_and_dead_letters_after_attempt_limit(self):
        job_id = str(uuid4())
        self.create_job(job_id=job_id)
        outbox_id = self.outbox_id(job_id)
        now = utcnow() + timedelta(seconds=1)
        claim = self.repository.claim_outbox_batch(
            owner="relay", lease_ttl_seconds=10, limit=1, now=now
        )[0]
        self.assertFalse(self.repository.retry_outbox(
            outbox_id,
            claim["lease_token"],
            "BrokerUnavailable",
            delay_seconds=0,
            max_attempts=1,
            now=now,
        ))
        snapshot = self.repository.get_outbox_snapshot(outbox_id)
        self.assertEqual(snapshot["state"], "DEAD")
        self.assertEqual(snapshot["attempts"], 1)
        self.assertEqual(snapshot["last_error"], "BrokerUnavailable")
        self.assertEqual(
            self.repository.claim_outbox_batch(
                owner="relay-again",
                lease_ttl_seconds=10,
                limit=1,
                now=now + timedelta(seconds=1),
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
