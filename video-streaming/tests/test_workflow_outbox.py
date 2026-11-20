"""Transactional outbox persistence, lease fencing and replay tests."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
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
        with self.repository._session() as session:
            row = session.get(OutboxRecord, self.outbox_id(job_id))
            self.assertEqual(
                set(row.payload_json),
                {"schema_version", "job_id", "workflow_version", "plan_fingerprint"},
            )
            self.assertNotIn("task", row.payload_json)

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
            persisted = session.execute(
                select(OutboxRecord).where(
                    OutboxRecord.dedupe_key == "dispatch-dedupe-conflict"
                )
            ).scalars().all()
            self.assertEqual(len(persisted), 1)

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
        with self.repository._session() as session:
            leased_row = session.get(OutboxRecord, outbox_id)
            self.assertEqual(len(leased_row.lock_token_hash), 64)
            self.assertNotEqual(
                leased_row.lock_token_hash, first[0]["lease_token"]
            )
        self.assertIn(
            "OUTBOX_CLAIMED",
            [event["type"] for event in self.repository.get_job_snapshot(job_id)["events"]],
        )
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

    def test_repeated_relay_crashes_dead_letter_after_attempt_limit(self):
        job_id = str(uuid4())
        self.create_job(job_id=job_id)
        outbox_id = self.outbox_id(job_id)
        first_claim_at = utcnow() + timedelta(seconds=1)
        first = self.repository.claim_outbox_batch(
            owner="relay-crash-1",
            lease_ttl_seconds=2,
            limit=1,
            now=first_claim_at,
            max_attempts=1,
        )
        self.assertEqual(len(first), 1)

        # Simulate a process crash: no retry_outbox() call is made. The next
        # relay must dead-letter the expired row rather than claim it forever.
        recovered_at = first_claim_at + timedelta(seconds=3)
        recovered = self.repository.claim_outbox_batch(
            owner="relay-crash-2",
            lease_ttl_seconds=2,
            limit=1,
            now=recovered_at,
            max_attempts=1,
        )
        self.assertEqual(recovered, [])
        snapshot = self.repository.get_outbox_snapshot(outbox_id)
        self.assertEqual(snapshot["state"], "DEAD")
        self.assertEqual(snapshot["attempts"], 1)
        self.assertEqual(snapshot["last_error"], "lease_expired_retry_limit")
        self.assertFalse(self.repository.mark_outbox_published(
            outbox_id, first[0]["lease_token"], now=recovered_at
        ))
        self.assertIn(
            "OUTBOX_DEAD_LETTERED",
            [event["type"] for event in self.repository.get_job_snapshot(job_id)["events"]],
        )

    def test_claim_rejects_invalid_attempt_budget(self):
        with self.assertRaisesRegex(ValueError, "max_attempts"):
            self.repository.claim_outbox_batch(
                owner="relay",
                max_attempts=0,
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


@unittest.skipUnless(
    os.environ.get("WORKFLOW_DATABASE_URL"),
    "PostgreSQL concurrency test requires WORKFLOW_DATABASE_URL",
)
class PostgreSQLWorkflowOutboxConcurrencyTests(unittest.TestCase):
    """Exercise SKIP LOCKED with two independent PostgreSQL connections."""

    def test_locked_outbox_row_is_skipped_by_a_concurrent_relay(self):
        database_url = os.environ["WORKFLOW_DATABASE_URL"]
        repository = WorkflowRepository(database_url)
        competing_repository = WorkflowRepository(database_url)
        job_id = str(uuid4())
        plan = WorkflowOutboxRepositoryTests.plan(job_id)
        repository.create_job(
            job_id=job_id,
            idempotency_key="pg-outbox-" + job_id,
            request_hash="c" * 64,
            execution_plan=plan,
            enqueue_dispatch=True,
        )
        with repository._session() as session:
            outbox_id = session.execute(
                select(OutboxRecord.id).where(
                    OutboxRecord.job_id == job_id
                )
            ).scalar_one()

        try:
            with repository._session.begin() as session:
                session.execute(
                    select(OutboxRecord).where(
                        OutboxRecord.id == outbox_id
                    ).with_for_update()
                ).scalar_one()
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        competing_repository.claim_outbox_batch,
                        "relay-concurrent",
                        30,
                        1,
                    )
                    self.assertEqual(future.result(timeout=10), [])
        finally:
            with repository._session.begin() as session:
                job = session.get(JobRecord, job_id)
                if job is not None:
                    session.delete(job)
            competing_repository.close()
            repository.close()


if __name__ == "__main__":
    unittest.main()
