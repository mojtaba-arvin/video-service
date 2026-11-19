"""Transactional outbox persistence, lease fencing and replay tests."""

import unittest
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

from video_streaming.workflows.repository import (
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

    def create_job(self, job_id=None, key=None, stage_specs=None, payload=None):
        job_id = job_id or str(uuid4())
        return self.repository.create_job(
            job_id=job_id,
            idempotency_key=key or "idem-" + str(uuid4()),
            request_hash="a" * 64,
            stage_specs=stage_specs,
            outbox_payload=payload or {
                "task": "test.noop",
                "args": [],
                "kwargs": {},
                "options": {},
            },
            outbox_dedupe_key="workflow.dispatch:" + job_id,
        )

    def test_job_and_dispatch_intent_are_committed_atomically(self):
        job_id = str(uuid4())
        self.assertEqual(self.create_job(job_id=job_id), (job_id, True))

        snapshot = self.repository.get_outbox_snapshot(job_id)
        self.assertEqual(snapshot["state"], "PENDING")
        self.assertEqual(snapshot["aggregate_id"], job_id)
        self.assertEqual(snapshot["dedupe_key"], "workflow.dispatch:" + job_id)

    def test_job_stage_failure_rolls_back_dispatch_intent(self):
        job_id = str(uuid4())
        duplicate_stages = [
            {"stage_key": "duplicate", "operation": "video.probe"},
            {"stage_key": "duplicate", "operation": "video.thumbnail.generate"},
        ]
        with self.assertRaises(IntegrityError):
            self.create_job(job_id=job_id, stage_specs=duplicate_stages)

        self.assertIsNone(self.repository.get_outbox_snapshot(job_id))
        with self.repository._session() as session:
            self.assertIsNone(session.get(OutboxRecord, job_id))

    def test_idempotent_job_replay_does_not_create_a_second_dispatch(self):
        job_id = str(uuid4())
        key = "stable-idempotency-key-" + str(uuid4())
        self.assertEqual(
            self.create_job(job_id=job_id, key=key),
            (job_id, True),
        )
        replay_id = str(uuid4())
        self.assertEqual(
            self.create_job(
                job_id=replay_id,
                key=key,
                payload={"task": "test.must_not_replace"},
            ),
            (job_id, False),
        )
        self.assertIsNone(self.repository.get_outbox_snapshot(replay_id))
        self.assertEqual(
            self.repository.get_outbox_snapshot(job_id)["state"],
            "PENDING",
        )

    def test_claim_lease_prevents_parallel_claim_and_fences_stale_owner(self):
        job_id = str(uuid4())
        self.create_job(job_id=job_id)
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
            job_id, first[0]["lease_token"], now=expired_at
        ))
        self.assertTrue(self.repository.mark_outbox_published(
            job_id, second[0]["lease_token"], now=expired_at
        ))
        self.assertEqual(
            self.repository.get_outbox_snapshot(job_id)["state"],
            "PUBLISHED",
        )

    def test_retry_is_bounded_and_dead_letters_after_attempt_limit(self):
        job_id = str(uuid4())
        self.create_job(job_id=job_id)
        now = utcnow() + timedelta(seconds=1)
        claim = self.repository.claim_outbox_batch(
            owner="relay", lease_ttl_seconds=10, limit=1, now=now
        )[0]
        self.assertFalse(self.repository.retry_outbox(
            job_id,
            claim["lease_token"],
            "BrokerUnavailable",
            delay_seconds=0,
            max_attempts=1,
            now=now,
        ))
        snapshot = self.repository.get_outbox_snapshot(job_id)
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
