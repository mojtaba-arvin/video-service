"""PostgreSQL-backed tests for durable workflow state and recovery."""

import os
import threading
import unittest
from datetime import timedelta
from uuid import uuid4

from video_streaming.workflows.domain import (
    AttemptLeaseLost,
    AttemptState,
    IdempotencyConflict,
    InvalidStateTransition,
    StageAlreadyRunning,
    StageState,
)
from video_streaming.workflows.repository import (
    WorkflowRepository, WorkflowRecord, JobRecord, AttemptRecord,
    ArtifactRecord, WorkflowEventRecord, utcnow
)
from video_streaming.security import RequestPrincipal, AuthorizationError
from video_streaming.workflows.execution_plan import execution_plan_sha256
from video_streaming.workflows.vod_plan import build_vod_execution_plan
from video_streaming.workflows.artifacts import attempt_scoped_object_key


@unittest.skipUnless(
    os.environ.get("WORKFLOW_DATABASE_URL"),
    "PostgreSQL workflow integration job supplies WORKFLOW_DATABASE_URL",
)
class WorkflowRepositoryTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import os
        database_url = os.environ.get("WORKFLOW_DATABASE_URL")
        if not database_url:
            raise RuntimeError(
                "WORKFLOW_DATABASE_URL must point at the migrated test database"
            )
        cls.repository = WorkflowRepository(database_url)

    @classmethod
    def tearDownClass(cls):
        cls.repository.close()

    def create_job(self, stage_key=None):
        job_id = str(uuid4())
        stage_specs = []
        if stage_key:
            stage_specs.append({
                "stage_key": stage_key,
                "operation": "video.package_and_publish",
                "input": {"protocol": "hls"},
            })
        returned_id, created = self.repository.create_job(
            job_id=job_id,
            idempotency_key="idem-{}".format(uuid4()),
            request_hash="a" * 64,
            reference_id="ref-{}".format(uuid4()),
            workflow_name="video.vod",
            stage_specs=stage_specs,
        )
        self.assertTrue(created)
        self.assertEqual(returned_id, job_id)
        return job_id


    def make_owner_principal(self, tenant="tenant-a", subject="subject-1"):
        from datetime import datetime, timezone
        return RequestPrincipal(
            issuer="https://issuer.example.test/",
            subject=subject,
            tenant_id=tenant,
            scopes=frozenset(["video.jobs:read", "video.jobs:create"]),
            roles=frozenset(),
            authentication_method="bearer",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

    def test_job_owner_is_persisted_and_authorized_by_tenant_and_subject(self):
        principal = self.make_owner_principal()
        job_id = str(uuid4())
        key = "idem-owned-{}".format(uuid4())
        created = self.repository.create_job(
            job_id, key, "a" * 64, owner_principal=principal
        )
        self.assertEqual(created, (job_id, True))
        with self.repository._session() as session:
            record = session.query(JobRecord).filter_by(id=job_id).one()
            self.assertEqual(record.owner_tenant_id, "tenant-a")
            self.assertEqual(record.owner_issuer, principal.issuer)
            self.assertEqual(record.owner_subject, "subject-1")

        self.assertTrue(self.repository.authorize_job_access(
            job_id, principal, "video.jobs:read"
        ))
        with self.assertRaises(AuthorizationError):
            self.repository.authorize_job_access(
                job_id, self.make_owner_principal(tenant="tenant-b"),
                "video.jobs:read",
            )
        with self.assertRaises(AuthorizationError):
            self.repository.authorize_job_access(
                job_id, self.make_owner_principal(subject="different-subject"),
                "video.jobs:read",
            )
        with self.assertRaises(AuthorizationError):
            self.repository.authorize_job_access(
                job_id, principal, "video.jobs:cancel"
            )

    def test_attempt_lease_is_hashed_exclusive_and_renewable(self):
        job_id = self.create_job("p0")
        attempt_id, attempt_number, token, expires_at = (
            self.repository.claim_attempt(
                job_id,
                "p0",
                lease_owner="worker-a",
                lease_ttl_seconds=30,
                execution_ref="celery-task-1",
            )
        )

        self.assertEqual(attempt_number, 1)
        self.assertTrue(token)
        with self.repository._session() as session:
            attempt = session.get(AttemptRecord, attempt_id)
            self.assertEqual(attempt.lease_owner, "worker-a")
            self.assertNotEqual(attempt.lease_token_hash, token)
            self.assertEqual(attempt.heartbeat_at, attempt.started_at)
            self.assertGreater(attempt.lease_expires_at, attempt.started_at)

        with self.assertRaises(StageAlreadyRunning):
            self.repository.claim_attempt(
                job_id, "p0", lease_owner="worker-b"
            )
        with self.assertRaises(AttemptLeaseLost):
            self.repository.renew_attempt_lease(
                job_id, "p0", attempt_id, "incorrect-token"
            )

        renewed_expiry = self.repository.renew_attempt_lease(
            job_id, "p0", attempt_id, token, lease_ttl_seconds=60
        )
        self.assertGreater(renewed_expiry, expires_at)

    def test_expired_attempt_lease_is_recovered_and_old_worker_is_fenced(self):
        from unittest.mock import patch

        job_id = self.create_job("p0")
        old_id, old_number, old_token, expiry = self.repository.claim_attempt(
            job_id, "p0", lease_owner="worker-old", lease_ttl_seconds=30
        )
        recovery_time = expiry + timedelta(seconds=1)

        with patch(
                "video_streaming.workflows.repository.utcnow",
                return_value=recovery_time):
            self.assertEqual(
                self.repository.recover_expired_attempts(now=recovery_time),
                1,
            )
            new_id, new_number, new_token, _ = self.repository.claim_attempt(
                job_id, "p0", lease_owner="worker-new", lease_ttl_seconds=60
            )

        self.assertEqual(old_number, 1)
        self.assertEqual(new_number, 2)
        self.assertNotEqual(old_id, new_id)
        with self.repository._session() as session:
            old_attempt = session.get(AttemptRecord, old_id)
            self.assertEqual(old_attempt.state, "RETRYABLE")
            self.assertEqual(old_attempt.error_code, "WORKER_LEASE_EXPIRED")

        with self.assertRaises(AttemptLeaseLost):
            self.repository.renew_attempt_lease(
                job_id, "p0", old_id, old_token
            )
        with self.assertRaises(AttemptLeaseLost):
            self.repository.finish_stage(
                job_id, "p0", old_id, lease_token=old_token
            )

        with self.assertRaises(AttemptLeaseLost):
            self.repository.mark_validated(
                job_id, "p0", new_id,
                {"valid": True, "errors": []},
            )
        self.repository.mark_validated(
            job_id, "p0", new_id,
            {"valid": True, "errors": []},
            lease_token=new_token,
        )
        with self.assertRaises(AttemptLeaseLost):
            self.repository.start_publishing(job_id, "p0", new_id)
        self.repository.start_publishing(
            job_id, "p0", new_id, lease_token=new_token
        )
        with self.assertRaises(AttemptLeaseLost):
            self.repository.finish_stage(job_id, "p0", new_id)
        self.repository.finish_stage(
            job_id, "p0", new_id, lease_token=new_token
        )

    def test_age_based_recovery_never_expires_a_live_leased_attempt(self):
        job_id = self.create_job("p0")
        attempt_id, _, token, _ = self.repository.claim_attempt(
            job_id, "p0", lease_owner="worker-live", lease_ttl_seconds=600
        )

        recovered = self.repository.recover_stale_attempts(
            older_than=utcnow() + timedelta(days=1)
        )
        self.assertEqual(recovered, 0)
        self.assertTrue(self.repository.renew_attempt_lease(
            job_id, "p0", attempt_id, token
        ))

    def test_concurrent_attempt_claims_have_one_winner(self):
        job_id = self.create_job("p0")
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def claim(worker_name):
            barrier.wait()
            try:
                result = self.repository.claim_attempt(
                    job_id,
                    "p0",
                    lease_owner=worker_name,
                    lease_ttl_seconds=60,
                    execution_ref="task-{}".format(worker_name),
                )
                outcome = ("claimed", result[0])
            except StageAlreadyRunning:
                outcome = ("busy", worker_name)
            with lock:
                outcomes.append(outcome)

        workers = [
            threading.Thread(target=claim, args=("worker-a",)),
            threading.Thread(target=claim, args=("worker-b",)),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(
            sorted(item[0] for item in outcomes),
            ["busy", "claimed"],
        )

    def test_legacy_unowned_job_remains_unowned_and_is_not_authorized(self):
        job_id = self.create_job()
        with self.repository._session() as session:
            record = session.query(JobRecord).filter_by(id=job_id).one()
            self.assertIsNone(record.owner_tenant_id)
            self.assertIsNone(record.owner_issuer)
            self.assertIsNone(record.owner_subject)

        with self.assertRaises(AuthorizationError):
            self.repository.authorize_job_access(
                job_id, self.make_owner_principal(), "video.jobs:read"
            )

    def test_idempotent_replay_of_owned_job_requires_principal(self):
        principal = self.make_owner_principal()
        job_id = str(uuid4())
        key = "idem-owner-anonymous-replay-{}".format(uuid4())
        self.repository.create_job(
            job_id, key, "c" * 64, owner_principal=principal
        )
        with self.assertRaises(AuthorizationError):
            self.repository.create_job(
                str(uuid4()), key, "c" * 64
            )

    def test_idempotent_replay_cannot_cross_owner_boundary(self):
        principal = self.make_owner_principal()
        job_id = str(uuid4())
        key = "idem-owner-replay-{}".format(uuid4())
        self.repository.create_job(
            job_id, key, "b" * 64, owner_principal=principal
        )
        with self.assertRaises(AuthorizationError):
            self.repository.create_job(
                str(uuid4()), key, "b" * 64,
                owner_principal=self.make_owner_principal(tenant="tenant-b"),
            )

    def test_idempotent_create_returns_same_durable_job(self):
        job_id = str(uuid4())
        key = "idem-{}".format(uuid4())
        specs = [{
            "stage_key": "p0",
            "operation": "video.package_and_publish",
        }]
        first = self.repository.create_job(
            job_id, key, "b" * 64, stage_specs=specs
        )
        replay = self.repository.create_job(
            str(uuid4()), key, "b" * 64, stage_specs=specs
        )
        self.assertEqual(first, (job_id, True))
        self.assertEqual(replay, (job_id, False))

        with self.assertRaises(IdempotencyConflict):
            self.repository.create_job(
                str(uuid4()), key, "c" * 64, stage_specs=specs
            )

        snapshot = self.repository.get_job_snapshot(job_id)
        self.assertEqual(len(snapshot["stages"]), 1)


    def build_plan(self, job_id, output_key="master.m3u8"):
        return build_vod_execution_plan(
            job_id,
            {
                "video": {"bucket": "input-media", "key": "source.mp4"},
                "playlists": [{
                    "bucket": "output-media",
                    "key": output_key,
                    "protocol": "hls",
                    "quality_names": ["360p"],
                    "use_watermark": False,
                    "dont_replace": False,
                    "create_bucket": False,
                }],
                "thumbnails": [],
            },
            workflow_version="vod-v1",
        )

    def test_execution_plan_is_persisted_atomically_with_workflow_and_stages(self):
        job_id = str(uuid4())
        plan = self.build_plan(job_id)
        returned_id, created = self.repository.create_job(
            job_id,
            "idem-plan-{}".format(uuid4()),
            "e" * 64,
            workflow_name="video.vod",
            execution_plan=plan,
            stage_specs=[{
                "stage_key": node["stage_key"],
                "operation": node["operation"],
                "input": {"webhook_url": "https://must-not-be-persisted.invalid"},
            } for node in plan["nodes"]],
        )

        self.assertEqual((returned_id, created), (job_id, True))
        persisted = self.repository.get_execution_plan(job_id)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["plan"], plan)
        self.assertEqual(persisted["schema_version"], plan["schema_version"])
        self.assertEqual(persisted["workflow_version"], "vod-v1")
        self.assertEqual(persisted["fingerprint"], execution_plan_sha256(plan))

        snapshot = self.repository.get_job_snapshot(job_id)
        self.assertEqual(snapshot["workflow"]["execution_plan_fingerprint"], persisted["fingerprint"])
        self.assertNotIn("execution_plan", snapshot["workflow"])
        self.assertEqual(
            [(stage["key"], stage["operation"]) for stage in snapshot["stages"]],
            [(node["stage_key"], node["operation"]) for node in plan["nodes"]],
        )
        self.assertTrue(all(not stage["input"] for stage in snapshot["stages"]))
        created_event = snapshot["events"][0]
        self.assertEqual(created_event["type"], "JOB_CREATED")
        self.assertEqual(created_event["payload"]["execution_plan_fingerprint"], persisted["fingerprint"])

    def test_idempotent_replay_rejects_a_different_execution_plan(self):
        job_id = str(uuid4())
        key = "idem-plan-conflict-{}".format(uuid4())
        self.repository.create_job(
            job_id, key, "f" * 64, execution_plan=self.build_plan(job_id)
        )

        with self.assertRaises(IdempotencyConflict):
            self.repository.create_job(
                job_id,
                key,
                "f" * 64,
                execution_plan=self.build_plan(job_id, "different/master.m3u8"),
            )

        persisted = self.repository.get_execution_plan(job_id)
        self.assertEqual(persisted["fingerprint"], execution_plan_sha256(self.build_plan(job_id)))



    def test_idempotent_plan_replay_rebinds_generated_job_id(self):
        original_job_id = str(uuid4())
        key = "idem-plan-generated-id-{}".format(uuid4())
        self.repository.create_job(
            original_job_id,
            key,
            "4" * 64,
            execution_plan=self.build_plan(original_job_id),
        )

        retry_job_id = str(uuid4())
        replay_id, created = self.repository.create_job(
            retry_job_id,
            key,
            "4" * 64,
            execution_plan=self.build_plan(retry_job_id),
        )

        self.assertEqual((replay_id, created), (original_job_id, False))
        self.assertEqual(
            self.repository.get_execution_plan(original_job_id)["fingerprint"],
            execution_plan_sha256(self.build_plan(original_job_id)),
        )

    def test_execution_plan_fingerprint_detects_persisted_mutation(self):
        job_id = str(uuid4())
        self.repository.create_job(
            job_id,
            "idem-plan-tamper-{}".format(uuid4()),
            "3" * 64,
            execution_plan=self.build_plan(job_id),
        )

        with self.repository._session.begin() as session:
            workflow = session.query(WorkflowRecord).filter_by(
                job_id=job_id
            ).one()
            mutated_plan = dict(workflow.execution_plan_json)
            mutated_plan["workflow_version"] = "tampered"
            workflow.execution_plan_json = mutated_plan

        with self.assertRaisesRegex(
                RuntimeError, "execution plan fingerprint mismatch"):
            self.repository.get_execution_plan(job_id)

        with self.assertRaisesRegex(
                RuntimeError, "execution plan fingerprint mismatch"):
            self.repository.get_job_snapshot(job_id)


    def test_execution_plan_denormalized_metadata_tampering_is_detected(self):
        job_id = str(uuid4())
        self.repository.create_job(
            job_id,
            "idem-plan-metadata-tamper-{}".format(uuid4()),
            "5" * 64,
            execution_plan=self.build_plan(job_id),
        )

        with self.repository._session.begin() as session:
            workflow = session.query(WorkflowRecord).filter_by(
                job_id=job_id
            ).one()
            workflow.workflow_version = "tampered"

        with self.assertRaisesRegex(
                RuntimeError, "execution plan metadata mismatch"):
            self.repository.get_execution_plan(job_id)

        with self.assertRaisesRegex(
                RuntimeError, "execution plan metadata mismatch"):
            self.repository.get_job_snapshot(job_id)

    def test_legacy_idempotent_replay_does_not_mutate_accepted_workflow(self):
        job_id = str(uuid4())
        key = "idem-legacy-plan-{}".format(uuid4())
        self.repository.create_job(job_id, key, "1" * 64)

        replay_id, created = self.repository.create_job(
            str(uuid4()),
            key,
            "1" * 64,
            execution_plan=self.build_plan(job_id),
        )

        self.assertEqual((replay_id, created), (job_id, False))
        self.assertIsNone(self.repository.get_execution_plan(job_id))

    def test_execution_plan_and_stage_projection_must_match(self):
        job_id = str(uuid4())
        plan = self.build_plan(job_id)
        with self.assertRaises(ValueError):
            self.repository.create_job(
                job_id,
                "idem-plan-stage-mismatch-{}".format(uuid4()),
                "2" * 64,
                execution_plan=plan,
                stage_specs=[{
                    "stage_key": "wrong-stage",
                    "operation": "video.hls.package",
                }],
            )
        self.assertIsNone(self.repository.get_job_snapshot(job_id))

    def test_state_machine_persists_validation_artifact_and_cost_metrics(self):
        job_id = self.create_job("p0")
        attempt_id, attempt_number = self.repository.start_attempt(
            job_id, "p0", execution_backend="test-worker", execution_ref="task-1"
        )
        with self.assertRaises(StageAlreadyRunning):
            self.repository.start_attempt(
                job_id, "p0",
                execution_backend="test-worker",
                execution_ref="duplicate-task",
            )

        report = {
            "protocol": "hls",
            "valid": True,
            "renditions": ["variant_360p.m3u8"],
            "segments": 3,
            "errors": [],
        }
        self.repository.mark_validated(job_id, "p0", attempt_id, report)
        self.repository.start_publishing(job_id, "p0", attempt_id)
        self.repository.finish_stage(
            job_id,
            "p0",
            attempt_id,
            artifacts=[{
                "artifact_type": "streaming_manifest",
                "logical_name": "master-playlist",
                "uri": "s3://video-output/jobs/{}/master.m3u8".format(job_id),
                "media_type": "application/vnd.apple.mpegurl",
                "size_bytes": 512,
            }],
            metrics={
                "cpu_seconds": 4.0,
                "duration_ms": 5000,
                "peak_memory_bytes": 256 * 1024 * 1024,
                "input_bytes": 1024,
                "output_bytes": 2048,
            },
            pricing={
                "model_id": "test-pricing-v1",
                "currency": "USD",
                "cpu_per_second": 0.001,
                "memory_gb_second": 0.0001,
                "storage_gb": 0.02,
            },
        )

        snapshot = self.repository.get_job_snapshot(job_id)
        self.assertEqual(snapshot["status"], "FINISHED")
        stage = snapshot["stages"][0]
        self.assertEqual(stage["status"], StageState.FINISHED.value)
        self.assertEqual(stage["attempts"][0]["backend"], "test-worker")
        self.assertGreater(stage["attempts"][0]["duration_ms"], 0)
        self.assertEqual(stage["attempts"][0]["cpu_seconds"], 4.0)
        self.assertEqual(stage["attempts"][0]["input_bytes"], 1024)
        self.assertEqual(stage["attempts"][0]["output_bytes"], 2048)
        self.assertGreater(stage["attempts"][0]["estimated_cost"], 0)
        reports = [
            artifact for artifact in stage["artifacts"]
            if artifact["type"] == "validation_report"
        ]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["metadata"], report)
        event_types = [event["type"] for event in snapshot["events"]]
        self.assertEqual(
            event_types,
            [
                "JOB_CREATED",
                "STAGE_ATTEMPT_STARTED",
                "STAGE_VALIDATED",
                "STAGE_PUBLISHING",
                "STAGE_FINISHED",
                "JOB_FINISHED",
            ],
        )

    def test_forbidden_transition_cannot_skip_validation(self):
        job_id = self.create_job("p0")
        attempt_id, _ = self.repository.start_attempt(job_id, "p0")
        with self.assertRaises(InvalidStateTransition):
            self.repository.start_publishing(job_id, "p0", attempt_id)

    def test_retry_creates_a_new_attempt_and_recovery_survives_worker_loss(self):
        job_id = self.create_job("p0")
        first_id, first_number = self.repository.start_attempt(job_id, "p0")
        self.repository.fail_attempt(
            job_id, "p0", first_id, retryable=True,
            error_code="TRANSIENT_UPLOAD_FAILURE",
        )
        second_id, second_number = self.repository.start_attempt(job_id, "p0")
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(second_number, first_number + 1)

        recovered = self.repository.recover_stale_attempts(
            utcnow() + timedelta(seconds=1)
        )
        # The PostgreSQL CI database is shared by tests in this process; other
        # tests can intentionally leave a running attempt to test transitions.
        # Assert recovery for this job, not a global count across all test jobs.
        self.assertGreaterEqual(recovered, 1)
        recovered_stage = self.repository.get_stage_snapshot(job_id, "p0")
        self.assertEqual(recovered_stage["status"], "RETRY_WAIT")
        third_id, third_number = self.repository.start_attempt(job_id, "p0")
        self.assertNotEqual(second_id, third_id)
        self.assertEqual(third_number, second_number + 1)

        snapshot = self.repository.get_job_snapshot(job_id)
        event_types = [event["type"] for event in snapshot["events"]]
        self.assertIn("ATTEMPT_RETRYABLE", event_types)
        self.assertIn("ATTEMPT_RECOVERED", event_types)
        self.assertEqual(
            event_types.count("STAGE_ATTEMPT_STARTED"), 3
        )

    def test_playback_qoe_events_are_idempotent_and_correlate_with_stage_cost(self):
        job_id = self.create_job("p0")
        attempt_id, _ = self.repository.start_attempt(job_id, "p0")
        self.repository.mark_validated(
            job_id, "p0", attempt_id,
            {"valid": True, "errors": []},
        )
        self.repository.start_publishing(job_id, "p0", attempt_id)
        self.repository.finish_stage(
            job_id,
            "p0",
            attempt_id,
            artifacts=[{
                "artifact_type": "streaming_manifest",
                "logical_name": "master.m3u8",
                "uri": "s3://vod-output/qoe/master.m3u8",
                "media_type": "application/vnd.apple.mpegurl",
                "size_bytes": 128,
            }],
            metrics={
                "cpu_seconds": 3.0,
                "duration_ms": 4200,
                "peak_memory_bytes": 128 * 1024 * 1024,
                "input_bytes": 4096,
                "output_bytes": 8192,
                "artifact_count": 1,
            },
            pricing={
                "model_id": "relative-resource-index-v1",
                "currency": "RUNIT",
                "cpu_per_second": 1.0,
                "memory_gb_second": 0.25,
                "storage_gb": 0.01,
            },
        )

        session = self.repository.start_playback_session(
            job_id, "p0", client_version="player-test/1.0"
        )
        self.assertTrue(session["session_id"])
        self.assertTrue(session["session_token"])
        snapshot = self.repository.get_job_snapshot(job_id)
        manifest_artifact = next(
            artifact
            for artifact in snapshot["stages"][0]["artifacts"]
            if artifact["type"] == "streaming_manifest"
        )
        self.assertEqual(
            session["manifest_artifact_id"], manifest_artifact["id"]
        )

        with self.assertRaises(PermissionError):
            self.repository.record_playback_event(
                session["session_id"],
                "incorrect-session-token",
                "bad-token-event",
                "PLAYBACK_STARTED",
                startup_ms=700,
            )
        with self.assertRaises(ValueError):
            self.repository.record_playback_event(
                session["session_id"],
                session["session_token"],
                "buffer-end-without-start",
                "BUFFERING_ENDED",
            )

        started = self.repository.record_playback_event(
            session["session_id"],
            session["session_token"],
            "player-event-1",
            "PLAYBACK_STARTED",
            startup_ms=700,
            client_version="player-test/1.0",
        )
        self.assertFalse(started["duplicate"])
        replayed = self.repository.record_playback_event(
            session["session_id"],
            session["session_token"],
            "player-event-1",
            "PLAYBACK_STARTED",
            startup_ms=700,
            client_version="player-test/1.0",
        )
        self.assertTrue(replayed["duplicate"])
        with self.assertRaises(ValueError):
            self.repository.record_playback_event(
                session["session_id"],
                session["session_token"],
                "player-event-1",
                "PLAYBACK_STARTED",
                startup_ms=701,
                client_version="player-test/1.0",
            )
        with self.assertRaises(ValueError):
            self.repository.record_playback_event(
                session["session_id"],
                session["session_token"],
                "player-event-1",
                "DECODER_ERROR",
                error_code="TEST_ERROR",
            )

        self.repository.record_playback_event(
            session["session_id"],
            session["session_token"],
            "player-event-2",
            "BUFFERING_STARTED",
        )
        buffering = self.repository.record_playback_event(
            session["session_id"],
            session["session_token"],
            "player-event-3",
            "BUFFERING_ENDED",
        )
        self.assertEqual(buffering["rebuffer_count"], 1)

        decoder = self.repository.record_playback_event(
            session["session_id"],
            session["session_token"],
            "player-event-4",
            "DECODER_ERROR",
            error_code="DECODE_H264_REFERENCE",
            decoder="test-decoder",
        )
        self.assertEqual(decoder["decoder_error_count"], 1)

        ended = self.repository.record_playback_event(
            session["session_id"],
            session["session_token"],
            "player-event-5",
            "PLAYBACK_ENDED",
        )
        self.assertEqual(ended["status"], "ENDED")
        self.assertTrue(
            self.repository.record_playback_event(
                session["session_id"],
                session["session_token"],
                "player-event-5",
                "PLAYBACK_ENDED",
            )["duplicate"]
        )

        report = self.repository.get_playback_qoe_report(job_id, "p0")
        self.assertEqual(report["session_count"], 1)
        self.assertEqual(report["playback_started_count"], 1)
        self.assertEqual(report["average_startup_ms"], 700)
        self.assertEqual(report["rebuffer_count"], 1)
        self.assertGreaterEqual(report["total_buffering_ms"], 0)
        self.assertEqual(report["decoder_error_count"], 1)
        self.assertEqual(report["total_cpu_seconds"], 3.0)
        self.assertGreater(report["total_estimated_cost"], 0)
        self.assertEqual(report["cost_currency"], "RUNIT")
        self.assertEqual(
            report["pricing_model_id"], "relative-resource-index-v1"
        )


    def test_active_buffering_interval_is_included_in_live_qoe_report(self):
        from datetime import timedelta
        from unittest.mock import patch

        job_id = self.create_job("p0")
        attempt_id, _ = self.repository.start_attempt(job_id, "p0")
        self.repository.mark_validated(
            job_id, "p0", attempt_id, {"valid": True, "errors": []}
        )
        self.repository.start_publishing(job_id, "p0", attempt_id)
        self.repository.finish_stage(
            job_id, "p0", attempt_id,
            artifacts=[{
                "artifact_type": "streaming_manifest",
                "logical_name": "master.m3u8",
                "uri": "s3://vod-output/live-qoe/master.m3u8",
                "media_type": "application/vnd.apple.mpegurl",
                "size_bytes": 128,
            }],
        )
        playback = self.repository.start_playback_session(job_id, "p0")
        base = utcnow() + timedelta(seconds=5)

        with patch("video_streaming.workflows.repository.utcnow",
                   return_value=base):
            self.repository.record_playback_event(
                playback["session_id"], playback["session_token"],
                "live-start", "PLAYBACK_STARTED", startup_ms=400,
            )
        with patch("video_streaming.workflows.repository.utcnow",
                   return_value=base + timedelta(seconds=10)):
            self.repository.record_playback_event(
                playback["session_id"], playback["session_token"],
                "live-buffer-start", "BUFFERING_STARTED",
            )
        with patch("video_streaming.workflows.repository.utcnow",
                   return_value=base + timedelta(seconds=15)):
            report = self.repository.get_playback_qoe_report(job_id, "p0")

        self.assertEqual(report["total_buffering_ms"], 5000)
        self.assertEqual(report["rebuffer_count"], 1)
        self.assertAlmostEqual(
            report["buffering_ratio_percent"], 100.0 / 3.0, places=2
        )

    def test_cost_total_is_unknown_when_any_attempt_is_unpriced(self):
        job_id = self.create_job("p0")
        first_attempt, _ = self.repository.start_attempt(job_id, "p0")
        self.repository.fail_attempt(
            job_id, "p0", first_attempt, retryable=True,
            error_code="WORKER_LOST",
            metrics={"cpu_seconds": 2.0},
        )
        second_attempt, _ = self.repository.start_attempt(job_id, "p0")
        self.repository.mark_validated(
            job_id, "p0", second_attempt, {"valid": True, "errors": []}
        )
        self.repository.start_publishing(job_id, "p0", second_attempt)
        self.repository.finish_stage(
            job_id, "p0", second_attempt,
            artifacts=[{
                "artifact_type": "streaming_manifest",
                "logical_name": "master.m3u8",
                "uri": "s3://vod-output/partial-cost/master.m3u8",
                "media_type": "application/vnd.apple.mpegurl",
                "size_bytes": 128,
            }],
            metrics={"cpu_seconds": 3.0, "duration_ms": 4000},
            pricing={
                "model_id": "test-price-v1",
                "currency": "USD",
                "cpu_per_second": 0.01,
                "memory_gb_second": 0.0,
                "storage_gb": 0.0,
            },
        )

        report = self.repository.get_playback_qoe_report(job_id, "p0")
        self.assertFalse(report["cost_estimate_complete"])
        self.assertEqual(report["priced_attempt_count"], 1)
        self.assertEqual(report["total_attempt_count"], 2)
        self.assertIsNone(report["total_estimated_cost"])
        self.assertIsNone(report["cost_currency"])
        self.assertIsNone(report["pricing_model_id"])

    def test_versioned_price_card_snapshot_and_component_breakdown_are_persisted(self):
        job_id = self.create_job("p0")
        attempt_id, _ = self.repository.start_attempt(job_id, "p0")
        self.repository.mark_validated(
            job_id, "p0", attempt_id, {"valid": True, "errors": []}
        )
        self.repository.start_publishing(job_id, "p0", attempt_id)
        pricing = {
            "model_id": "infra-us-east",
            "model_version": "2026-11-01",
            "currency": "USD",
            "cpu_per_second": 0.01,
            "memory_gb_second": 0.02,
            "storage_gb": 0.03,
        }
        self.repository.finish_stage(
            job_id,
            "p0",
            attempt_id,
            artifacts=[{
                "artifact_type": "streaming_manifest",
                "logical_name": "master.m3u8",
                "uri": "s3://vod-output/versioned-cost/master.m3u8",
                "size_bytes": 1024,
            }],
            metrics={
                "cpu_seconds": 4.0,
                "peak_memory_bytes": 512 * 1024 * 1024,
                "memory_duration_ms": 2000,
                "input_bytes": 4096,
                "output_bytes": 1024 * 1024 * 1024,
            },
            pricing=pricing,
        )

        stage = self.repository.get_stage_snapshot(job_id, "p0")
        attempt = stage["attempts"][0]
        snapshot = attempt["pricing_snapshot"]
        accounting = attempt["cost_accounting"]

        self.assertEqual(snapshot["model_id"], "infra-us-east")
        self.assertEqual(snapshot["model_version"], "2026-11-01")
        self.assertEqual(snapshot["currency"], "USD")
        self.assertEqual(snapshot["rates"], {
            "cpu_per_second": 0.01,
            "memory_gb_second": 0.02,
            "storage_gb": 0.03,
        })
        self.assertEqual(attempt["pricing_model_version"], "2026-11-01")
        self.assertTrue(accounting["coverage"]["complete"])
        self.assertEqual(
            accounting["coverage"]["missing_dimensions"], []
        )
        self.assertEqual(
            set(accounting["components"]),
            {"cpu_compute", "memory", "storage"},
        )
        self.assertAlmostEqual(
            accounting["components"]["memory"]["quantity"], 1.0, places=6
        )
        self.assertAlmostEqual(
            accounting["components"]["memory"]["amount"], 0.02, places=6
        )
        self.assertGreater(attempt["estimated_cost"], 0)
        self.assertIn("network_egress", snapshot["excluded_dimensions"])
        self.assertIn("provider_invoice_reconciliation",
                      snapshot["excluded_dimensions"])

        report = self.repository.get_playback_qoe_report(job_id, "p0")
        self.assertTrue(report["cost_estimate_complete"])
        self.assertEqual(report["pricing_model_id"], "infra-us-east")
        self.assertEqual(report["cost_coverage"]["pricing_model_versions"],
                         ["2026-11-01"])

    def test_failed_attempt_retains_partial_spend_and_missing_metric_coverage(self):
        job_id = self.create_job("p0")
        attempt_id, _ = self.repository.start_attempt(job_id, "p0")
        pricing = {
            "model_id": "worker-cost",
            "model_version": "2026-11-18",
            "currency": "USD",
            "cpu_per_second": 0.02,
            "memory_gb_second": 0.01,
            "storage_gb": 0.04,
        }
        self.repository.fail_attempt(
            job_id,
            "p0",
            attempt_id,
            retryable=True,
            error_code="ENCODER_FAILED",
            metrics={"cpu_seconds": 2.5, "input_bytes": 5000},
            pricing=pricing,
        )

        stage = self.repository.get_stage_snapshot(job_id, "p0")
        attempt = stage["attempts"][0]
        coverage = attempt["cost_accounting"]["coverage"]
        self.assertEqual(attempt["status"], "RETRYABLE")
        self.assertGreater(attempt["estimated_cost"], 0)
        self.assertFalse(coverage["complete"])
        self.assertEqual(coverage["measured_dimensions"], ["cpu_compute"])
        self.assertEqual(
            set(coverage["missing_dimensions"]), {"memory", "storage"}
        )

        report = self.repository.get_playback_qoe_report(job_id, "p0")
        self.assertFalse(report["cost_estimate_complete"])
        self.assertIsNone(report["total_estimated_cost"])
        self.assertGreater(report["partial_estimated_cost"], 0)
        self.assertEqual(
            report["cost_coverage"]["missing_dimensions_by_attempt"],
            {"memory": 1, "storage": 1},
        )

    def test_cancelled_attempt_accounts_for_measured_compute(self):
        job_id = self.create_job("p0")
        attempt_id, _ = self.repository.start_attempt(job_id, "p0")
        self.repository.cancel_attempt(
            job_id,
            "p0",
            attempt_id,
            reason="USER_CANCELLED",
            metrics={"cpu_seconds": 1.25},
            pricing={
                "model_id": "cpu-only",
                "model_version": "v2",
                "currency": "USD",
                "cpu_per_second": 0.1,
                "memory_gb_second": 0,
                "storage_gb": 0,
            },
        )

        stage = self.repository.get_stage_snapshot(job_id, "p0")
        attempt = stage["attempts"][0]
        self.assertEqual(attempt["status"], "CANCELLED")
        self.assertAlmostEqual(attempt["estimated_cost"], 0.125, places=6)
        self.assertTrue(attempt["cost_accounting"]["coverage"]["complete"])
        self.assertEqual(
            attempt["cost_accounting"]["coverage"]["not_priced_dimensions"],
            ["memory_gb_second", "storage_gb"],
        )

    def test_mixed_price_card_versions_never_produce_complete_total(self):
        job_id = self.create_job("p0")
        first_attempt, _ = self.repository.start_attempt(job_id, "p0")
        self.repository.fail_attempt(
            job_id,
            "p0",
            first_attempt,
            retryable=True,
            metrics={"cpu_seconds": 1.0},
            pricing={
                "model_id": "infra",
                "model_version": "v1",
                "currency": "USD",
                "cpu_per_second": 0.01,
                "memory_gb_second": 0,
                "storage_gb": 0,
            },
        )
        second_attempt, _ = self.repository.start_attempt(job_id, "p0")
        self.repository.mark_validated(
            job_id, "p0", second_attempt, {"valid": True, "errors": []}
        )
        self.repository.start_publishing(job_id, "p0", second_attempt)
        self.repository.finish_stage(
            job_id,
            "p0",
            second_attempt,
            metrics={"cpu_seconds": 2.0},
            pricing={
                "model_id": "infra",
                "model_version": "v2",
                "currency": "USD",
                "cpu_per_second": 0.01,
                "memory_gb_second": 0,
                "storage_gb": 0,
            },
        )

        report = self.repository.get_playback_qoe_report(job_id, "p0")
        self.assertFalse(report["cost_estimate_complete"])
        self.assertIsNone(report["total_estimated_cost"])
        self.assertEqual(
            report["cost_coverage"]["pricing_model_versions"], ["v1", "v2"]
        )

    def test_worker_loss_records_price_version_but_never_fabricates_usage(self):
        job_id = self.create_job("p0")
        attempt_id, _ = self.repository.start_attempt(job_id, "p0")

        recovered = self.repository.recover_stale_attempts(
            utcnow() + timedelta(seconds=1),
            pricing={
                "model_id": "worker-infra",
                "model_version": "2026-11-18",
                "currency": "USD",
                "cpu_per_second": 0.01,
                "memory_gb_second": 0.02,
                "storage_gb": 0.03,
            },
        )

        # The PostgreSQL test database may contain other intentionally stale
        # attempts from earlier tests in this same process. Assert the target
        # job is included without depending on unrelated recovery counts.
        self.assertGreaterEqual(recovered, 1)
        attempt = self.repository.get_stage_snapshot(job_id, "p0")["attempts"][0]
        self.assertEqual(attempt["id"], attempt_id)
        self.assertEqual(attempt["status"], "RETRYABLE")
        self.assertEqual(attempt["error_code"], "WORKER_LOST")
        self.assertEqual(attempt["pricing_model_version"], "2026-11-18")
        self.assertIsNone(attempt["estimated_cost"])
        coverage = attempt["cost_accounting"]["coverage"]
        self.assertFalse(coverage["complete"])
        self.assertEqual(
            set(coverage["missing_dimensions"]),
            {"cpu_compute", "memory", "storage"},
        )

        report = self.repository.get_playback_qoe_report(job_id, "p0")
        self.assertFalse(report["cost_estimate_complete"])
        self.assertIsNone(report["total_estimated_cost"])
        self.assertEqual(report["cost_coverage"]["priced_attempt_count"], 0)
        self.assertEqual(report["cost_coverage"]["total_attempt_count"], 1)

    def test_playback_session_requires_a_published_manifest(self):
        job_id = self.create_job("p0")
        with self.assertRaises(ValueError):
            self.repository.start_playback_session(job_id, "p0")

    def test_concurrent_idempotency_key_creates_only_one_job(self):
        candidate_ids = [str(uuid4()), str(uuid4())]
        key = "idem-concurrent-{}".format(uuid4())
        request_hash = "d" * 64
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def submit(candidate_id):
            try:
                barrier.wait(timeout=5)
                result = self.repository.create_job(
                    candidate_id, key, request_hash,
                    stage_specs=[{
                        "stage_key": "p0",
                        "operation": "video.package_and_publish",
                    }],
                )
                results.append(result)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=submit, args=(candidate_ids[0],)),
            threading.Thread(target=submit, args=(candidate_ids[1],)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(errors, errors)
        self.assertEqual(len(results), 2)
        # Either racing request may win the initial insert; both callers must
        # receive that same durable identity regardless of scheduling order.
        returned_ids = {item[0] for item in results}
        self.assertEqual(len(returned_ids), 1)
        durable_job_id = returned_ids.pop()
        self.assertIn(durable_job_id, candidate_ids)
        self.assertEqual(sum(1 for _, created in results if created), 1)
        self.assertEqual(
            len(self.repository.get_job_snapshot(durable_job_id)["stages"]),
            1,
        )


    def test_processing_completion_closes_lease_but_keeps_stage_validated(self):
        job_id = self.create_job("p0")
        attempt_id, number, token, _ = self.repository.claim_attempt(
            job_id, "p0", lease_owner="worker-processing-complete"
        )
        report = {"valid": True, "errors": []}

        self.assertTrue(self.repository.complete_processing_attempt(
            job_id,
            "p0",
            attempt_id,
            report,
            lease_token=token,
            metrics={"cpu_seconds": 0.25, "input_bytes": 4096},
        ))

        stage = self.repository.get_stage_snapshot(job_id, "p0")
        self.assertEqual(stage["status"], "VALIDATED")
        self.assertEqual(stage["attempts"][0]["number"], number)
        self.assertEqual(stage["attempts"][0]["status"], AttemptState.SUCCEEDED.value)
        self.assertEqual(len(stage["artifacts"]), 1)
        self.assertEqual(stage["artifacts"][0]["metadata"], report)

        with self.repository._session() as session:
            record = session.get(AttemptRecord, attempt_id)
            self.assertIsNone(record.lease_owner)
            self.assertIsNone(record.lease_token_hash)
            self.assertIsNone(record.lease_expires_at)
            self.assertIsNone(record.heartbeat_at)

        with self.assertRaises(AttemptLeaseLost):
            self.repository.renew_attempt_lease(
                job_id, "p0", attempt_id, token
            )

    def test_publication_claim_uses_a_new_attempt_and_new_token(self):
        job_id = self.create_job("p0")
        processing_id, _, processing_token, _ = self.repository.claim_attempt(
            job_id, "p0", lease_owner="worker-processing"
        )
        self.repository.complete_processing_attempt(
            job_id, "p0", processing_id,
            {"valid": True, "errors": []},
            lease_token=processing_token,
        )

        stage_version_before_claim = self.repository.get_stage_snapshot(
            job_id, "p0"
        )["version"]
        with self.assertRaises(InvalidStateTransition):
            self.repository.claim_publication_attempt(
                job_id,
                "p0",
                lease_owner="worker-wrong-processing-proof",
                processing_attempt_id="not-the-processing-attempt",
            )

        publication_id, number, publication_token, _ = (
            self.repository.claim_publication_attempt(
                job_id,
                "p0",
                lease_owner="worker-publisher",
                processing_attempt_id=processing_id,
            )
        )
        self.assertNotEqual(processing_id, publication_id)
        self.assertNotEqual(processing_token, publication_token)
        self.assertEqual(number, 2)
        self.assertGreater(
            self.repository.get_stage_snapshot(job_id, "p0")["version"],
            stage_version_before_claim,
        )

        self.repository.start_publishing(
            job_id, "p0", publication_id, lease_token=publication_token
        )
        stage = self.repository.get_stage_snapshot(job_id, "p0")
        self.assertEqual(stage["status"], "PUBLISHING")
        self.assertEqual(
            [item["status"] for item in stage["attempts"]],
            ["SUCCEEDED", "RUNNING"],
        )

        with self.assertRaises(AttemptLeaseLost):
            self.repository.start_publishing(
                job_id, "p0", publication_id, lease_token=processing_token
            )

    def test_publication_claim_requires_a_persisted_valid_report(self):
        job_id = self.create_job("p0")
        with self.assertRaises(InvalidStateTransition):
            self.repository.claim_publication_attempt(
                job_id, "p0", lease_owner="worker-without-report"
            )

        processing_id, _, token, _ = self.repository.claim_attempt(
            job_id, "p0", lease_owner="worker-processing"
        )
        with self.assertRaises(InvalidStateTransition):
            self.repository.claim_publication_attempt(
                job_id, "p0", lease_owner="worker-too-early"
            )

        self.repository.complete_processing_attempt(
            job_id, "p0", processing_id,
            {"valid": True, "errors": []},
            lease_token=token,
        )
        stage = self.repository.get_stage_snapshot(job_id, "p0")
        stage["artifacts"][0]["metadata"]["valid"] = False
        # The detached snapshot is not authoritative; the stored proof remains valid.
        publication = self.repository.claim_publication_attempt(
            job_id, "p0", lease_owner="worker-valid-proof"
        )
        self.assertEqual(publication[1], 2)

    def test_publication_retry_reuses_validation_proof_without_reprocessing(self):
        job_id = self.create_job("p0")
        processing_id, _, processing_token, _ = self.repository.claim_attempt(
            job_id, "p0", lease_owner="worker-processing"
        )
        self.repository.complete_processing_attempt(
            job_id, "p0", processing_id,
            {"valid": True, "errors": []},
            lease_token=processing_token,
            metrics={"cpu_seconds": 1.0},
        )

        first_publish_id, _, first_token, _ = (
            self.repository.claim_publication_attempt(
                job_id, "p0", lease_owner="publisher-first"
            )
        )
        self.repository.start_publishing(
            job_id, "p0", first_publish_id, lease_token=first_token
        )
        self.assertTrue(self.repository.fail_attempt(
            job_id, "p0", first_publish_id,
            retryable=True,
            error_code="UPLOAD_TIMEOUT",
            lease_token=first_token,
        ))

        with self.assertRaises(InvalidStateTransition):
            self.repository.claim_attempt(
                job_id, "p0", lease_owner="wrong-processing-retry"
            )

        second_publish_id, second_number, second_token, _ = (
            self.repository.claim_publication_attempt(
                job_id, "p0", lease_owner="publisher-retry"
            )
        )
        self.assertNotEqual(first_publish_id, second_publish_id)
        self.assertEqual(second_number, 3)
        self.repository.start_publishing(
            job_id, "p0", second_publish_id, lease_token=second_token
        )
        stage = self.repository.get_stage_snapshot(job_id, "p0")
        self.assertEqual(stage["status"], "PUBLISHING")
        self.assertEqual(len([
            artifact for artifact in stage["artifacts"]
            if artifact["logical_name"] == "validation-report"
        ]), 1)

    def test_concurrent_publication_claim_has_exactly_one_database_winner(self):
        job_id = self.create_job("p0")
        processing_id, _, token, _ = self.repository.claim_attempt(
            job_id, "p0", lease_owner="worker-processing"
        )
        self.repository.complete_processing_attempt(
            job_id, "p0", processing_id,
            {"valid": True, "errors": []},
            lease_token=token,
        )

        barrier = threading.Barrier(2)
        results = []
        errors = []

        def claim(owner):
            try:
                barrier.wait(timeout=5)
                results.append(self.repository.claim_publication_attempt(
                    job_id, "p0", lease_owner=owner
                ))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=claim, args=("publisher-a",)),
            threading.Thread(target=claim, args=("publisher-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StageAlreadyRunning)
        stage = self.repository.get_stage_snapshot(job_id, "p0")
        self.assertEqual(stage["status"], "VALIDATED")
        self.assertEqual(len(stage["attempts"]), 2)



    def test_processing_completion_rejects_wrong_or_expired_lease(self):
        from unittest.mock import patch

        job_id = self.create_job("p0")
        attempt_id, _, token, expires_at = self.repository.claim_attempt(
            job_id, "p0", lease_owner="worker-completion-fence"
        )
        report = {"valid": True, "errors": []}

        with self.assertRaises(AttemptLeaseLost):
            self.repository.complete_processing_attempt(
                job_id, "p0", attempt_id, report,
                lease_token="not-the-owner-token",
            )

        with patch(
                "video_streaming.workflows.repository.utcnow",
                return_value=expires_at):
            with self.assertRaises(AttemptLeaseLost):
                self.repository.complete_processing_attempt(
                    job_id, "p0", attempt_id, report,
                    lease_token=token,
                )

        stage = self.repository.get_stage_snapshot(job_id, "p0")
        self.assertEqual(stage["status"], "PROCESSING")
        self.assertEqual(stage["attempts"][0]["status"], "RUNNING")
        self.assertEqual(stage["artifacts"], [])

    def test_processing_completion_rejects_invalid_validation_report(self):
        job_id = self.create_job("p0")
        attempt_id, _, token, _ = self.repository.claim_attempt(
            job_id, "p0", lease_owner="worker-invalid-completion-report"
        )
        for report in (
                {"valid": False, "errors": []},
                {"valid": True, "errors": [{"code": "BROKEN"}]},
                {"valid": True},
                None):
            with self.subTest(report=report):
                with self.assertRaises(ValueError):
                    self.repository.complete_processing_attempt(
                        job_id, "p0", attempt_id, report,
                        lease_token=token,
                    )

        stage = self.repository.get_stage_snapshot(job_id, "p0")
        self.assertEqual(stage["status"], "PROCESSING")
        self.assertEqual(stage["attempts"][0]["status"], "RUNNING")
        self.assertEqual(stage["artifacts"], [])

    def test_publication_claim_event_never_contains_raw_token(self):
        job_id = self.create_job("p0")
        processing_id, _, processing_token, _ = self.repository.claim_attempt(
            job_id, "p0", lease_owner="worker-processing"
        )
        self.repository.complete_processing_attempt(
            job_id, "p0", processing_id,
            {"valid": True, "errors": []},
            lease_token=processing_token,
        )
        publication_id, _, publication_token, _ = (
            self.repository.claim_publication_attempt(
                job_id, "p0", lease_owner="worker-event-redaction"
            )
        )

        with self.repository._session() as session:
            event = session.query(WorkflowEventRecord).filter_by(
                attempt_id=publication_id,
                event_type="STAGE_PUBLICATION_ATTEMPT_CLAIMED",
            ).one()

        self.assertNotIn(publication_token, repr(event.payload_json))
        self.assertNotIn("lease_token", event.payload_json)
        self.assertEqual(
            event.payload_json["lease_owner"], "worker-event-redaction"
        )



    def test_expired_publication_lease_recovers_and_fences_stale_publisher(self):
        from unittest.mock import patch

        job_id = self.create_job("p0")
        processing_id, _, processing_token, _ = self.repository.claim_attempt(
            job_id, "p0", lease_owner="worker-processing"
        )
        self.repository.complete_processing_attempt(
            job_id, "p0", processing_id,
            {"valid": True, "errors": []},
            lease_token=processing_token,
        )
        first_id, _, first_token, first_expiry = (
            self.repository.claim_publication_attempt(
                job_id, "p0", lease_owner="publisher-stale"
            )
        )
        self.repository.start_publishing(
            job_id, "p0", first_id, lease_token=first_token
        )

        second_id = None
        with patch(
                "video_streaming.workflows.repository.utcnow",
                return_value=first_expiry):
            second_id, second_number, second_token, _ = (
                self.repository.claim_publication_attempt(
                    job_id, "p0", lease_owner="publisher-recovery"
                )
            )

        self.assertNotEqual(first_id, second_id)
        self.assertEqual(second_number, 3)
        with self.assertRaises(AttemptLeaseLost):
            self.repository.finish_stage(
                job_id, "p0", first_id,
                artifacts=[],
                lease_token=first_token,
            )

        self.repository.start_publishing(
            job_id, "p0", second_id, lease_token=second_token
        )
        stage = self.repository.get_stage_snapshot(job_id, "p0")
        self.assertEqual(stage["status"], "PUBLISHING")
        self.assertEqual(
            [item["status"] for item in stage["attempts"]],
            ["SUCCEEDED", "RETRYABLE", "RUNNING"],
        )



    def test_publication_claim_rejects_legacy_nonterminal_processing_attempt(self):
        job_id = self.create_job("p0")
        processing_id, _ = self.repository.start_attempt(
            job_id, "p0", execution_ref="legacy-processing-task"
        )
        self.repository.mark_validated(
            job_id, "p0", processing_id,
            {"valid": True, "errors": []},
        )

        with self.assertRaises(StageAlreadyRunning):
            self.repository.claim_publication_attempt(
                job_id, "p0", lease_owner="publisher-before-terminal"
            )

        stage = self.repository.get_stage_snapshot(job_id, "p0")
        self.assertEqual(stage["status"], "VALIDATED")
        self.assertEqual(stage["attempts"][0]["status"], "RUNNING")



    def test_processing_completion_is_idempotent_for_existing_validation_artifact(self):
        job_id = self.create_job("p0")
        attempt_id, _, token, _ = self.repository.claim_attempt(
            job_id, "p0", lease_owner="worker-idempotent-validation"
        )
        report = {"valid": True, "errors": []}
        self.assertTrue(self.repository.mark_validated(
            job_id, "p0", attempt_id, report, lease_token=token
        ))

        self.assertFalse(self.repository.complete_processing_attempt(
            job_id, "p0", attempt_id, report,
            lease_token=token,
            metrics={"cpu_seconds": 0.5},
        ))
        stage = self.repository.get_stage_snapshot(job_id, "p0")
        self.assertEqual(stage["status"], "VALIDATED")
        self.assertEqual(stage["attempts"][0]["status"], "SUCCEEDED")
        self.assertEqual(len(stage["artifacts"]), 1)

        with self.repository._session() as session:
            events = session.query(WorkflowEventRecord).filter_by(
                attempt_id=attempt_id,
                event_type="STAGE_PROCESSING_ATTEMPT_COMPLETED",
            ).all()
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].payload_json["idempotent_validation_replay"])



    def test_cancel_attempt_is_lease_fenced_and_clears_lease_tuple(self):
        job_id = self.create_job("p0")
        attempt_id, _, token, _ = self.repository.claim_attempt(
            job_id, "p0", lease_owner="cancel-worker"
        )

        with self.assertRaises(AttemptLeaseLost):
            self.repository.cancel_attempt(
                job_id, "p0", attempt_id, lease_token="wrong-token"
            )

        self.assertTrue(self.repository.cancel_attempt(
            job_id, "p0", attempt_id, lease_token=token
        ))
        with self.repository._session() as session:
            attempt = session.get(AttemptRecord, attempt_id)
            self.assertEqual(attempt.state, AttemptState.CANCELLED.value)
            self.assertIsNone(attempt.lease_owner)
            self.assertIsNone(attempt.lease_token_hash)
            self.assertIsNone(attempt.lease_expires_at)
            self.assertIsNone(attempt.heartbeat_at)

    def test_failed_attempt_clears_lease_tuple_for_safe_reclamation(self):
        job_id = self.create_job("p0")
        attempt_id, _, token, _ = self.repository.claim_attempt(
            job_id, "p0", lease_owner="failed-worker"
        )
        self.assertTrue(self.repository.fail_attempt(
            job_id, "p0", attempt_id,
            retryable=False,
            error_code="SYNTHETIC_FAILURE",
            error_message="test",
            lease_token=token,
        ))
        with self.repository._session.begin() as session:
            attempt = session.get(AttemptRecord, attempt_id)
            attempt.finished_at = utcnow() - timedelta(days=2)
            self.assertIsNone(attempt.lease_owner)
            self.assertIsNone(attempt.lease_token_hash)
            self.assertIsNone(attempt.lease_expires_at)
            self.assertIsNone(attempt.heartbeat_at)

    def test_available_artifact_blocks_attempt_prefix_reclamation(self):
        job_id = self.create_job("p0")
        processing_id, _, processing_token, _ = self.repository.claim_attempt(
            job_id, "p0", lease_owner="processing"
        )
        self.repository.complete_processing_attempt(
            job_id, "p0", processing_id,
            {"valid": True, "errors": []},
            lease_token=processing_token,
        )
        attempt_id, _, token, _ = self.repository.claim_publication_attempt(
            job_id, "p0",
            lease_owner="failed-with-object",
            processing_attempt_id=processing_id,
            package_output_key="vod/master.m3u8",
            package_output_bucket="vod-output",
        )
        self.repository.fail_attempt(
            job_id, "p0", attempt_id,
            retryable=False,
            error_code="SYNTHETIC_FAILURE",
            error_message="test",
            lease_token=token,
        )
        with self.repository._session.begin() as session:
            attempt = session.get(AttemptRecord, attempt_id)
            attempt.finished_at = utcnow() - timedelta(days=2)
            session.add(ArtifactRecord(
                id=str(uuid4()),
                job_id=job_id,
                stage_id=attempt.stage_id,
                attempt_id=attempt_id,
                artifact_type="streaming_manifest",
                logical_name="master.m3u8",
                uri="s3://vod-output/vod/.attempts/{}/master.m3u8".format(
                    attempt_id
                ),
                status="AVAILABLE",
                metadata_json={},
            ))

        self.assertFalse(self.repository.is_attempt_reclaimable(
            attempt_id,
            "vod-output",
            "vod/.attempts/{}".format(attempt_id),
            minimum_age_seconds=86400,
        ))

    def test_recovered_publication_attempt_is_reclaimable_only_after_replacement(self):
        job_id = self.create_job("p0")
        processing_id, _, processing_token, _ = self.repository.claim_attempt(
            job_id, "p0", lease_owner="processing"
        )
        self.repository.complete_processing_attempt(
            job_id, "p0", processing_id,
            {"valid": True, "errors": []},
            lease_token=processing_token,
        )
        old_id, _, _, expiry = self.repository.claim_publication_attempt(
            job_id, "p0", lease_owner="publisher-crashed",
            processing_attempt_id=processing_id,
            package_output_key="vod/master.m3u8",
            package_output_bucket="vod-output",
        )
        self.repository.recover_expired_attempts(now=expiry)
        with self.repository._session.begin() as session:
            old_attempt = session.get(AttemptRecord, old_id)
            old_attempt.finished_at = utcnow() - timedelta(days=2)

        prefix = "vod/.attempts/{}".format(old_id)
        self.assertFalse(self.repository.is_attempt_reclaimable(
            old_id, "vod-output", prefix, minimum_age_seconds=86400
        ))

        new_id, _, _, _ = self.repository.claim_publication_attempt(
            job_id, "p0", lease_owner="replacement-publisher",
            processing_attempt_id=processing_id,
        )
        self.assertNotEqual(old_id, new_id)
        self.assertTrue(self.repository.is_attempt_reclaimable(
            old_id, "vod-output", prefix, minimum_age_seconds=86400
        ))

    def test_crash_after_package_upload_retries_with_a_new_namespace(self):
        job_id = self.create_job("p0")
        processing_id, _, processing_token, _ = self.repository.claim_attempt(
            job_id, "p0", lease_owner="processing"
        )
        self.repository.complete_processing_attempt(
            job_id, "p0", processing_id,
            {"valid": True, "errors": []},
            lease_token=processing_token,
        )

        old_id, _, old_token, old_expiry = (
            self.repository.claim_publication_attempt(
                job_id, "p0", lease_owner="publisher-before-db-commit",
                processing_attempt_id=processing_id,
                package_output_key="vod/master.m3u8",
                package_output_bucket="vod-output",
            )
        )
        old_key = attempt_scoped_object_key(
            "vod/master.m3u8", old_id
        )
        self.repository.start_publishing(
            job_id, "p0", old_id, lease_token=old_token
        )

        # Model a worker crash after remote upload/readback but before the
        # Artifact-pointer transaction commits.
        self.repository.recover_expired_attempts(now=old_expiry)
        with self.repository._session.begin() as session:
            old_attempt = session.get(AttemptRecord, old_id)
            old_attempt.finished_at = utcnow() - timedelta(days=2)

        new_id, _, new_token, _ = self.repository.claim_publication_attempt(
            job_id, "p0", lease_owner="replacement-after-crash",
            processing_attempt_id=processing_id,
            package_output_key="vod/master.m3u8",
            package_output_bucket="vod-output",
        )
        new_key = attempt_scoped_object_key(
            "vod/master.m3u8", new_id
        )
        self.assertNotEqual(old_key, new_key)

        with self.assertRaises(AttemptLeaseLost):
            self.repository.finish_stage(
                job_id, "p0", old_id,
                artifacts=[{
                    "artifact_type": "streaming_manifest",
                    "logical_name": "master",
                    "uri": "s3://vod-output/{}".format(old_key),
                }],
                lease_token=old_token,
            )

        self.repository.start_publishing(
            job_id, "p0", new_id, lease_token=new_token
        )
        self.repository.finish_stage(
            job_id, "p0", new_id,
            artifacts=[{
                "artifact_type": "streaming_manifest",
                "logical_name": "master",
                "uri": "s3://vod-output/{}".format(new_key),
            }],
            lease_token=new_token,
        )
        stage = self.repository.get_stage_snapshot(job_id, "p0")
        available_manifests = [
            item["uri"] for item in stage["artifacts"]
            if item["type"] == "streaming_manifest"
            and item["status"] == "AVAILABLE"
        ]
        self.assertEqual(
            available_manifests, ["s3://vod-output/{}".format(new_key)]
        )
        self.assertTrue(self.repository.is_attempt_reclaimable(
            old_id,
            "vod-output",
            "vod/.attempts/{}".format(old_id),
            minimum_age_seconds=86400,
        ))

    def test_package_namespace_survives_failure_and_is_listed_for_janitor(self):
        job_id = self.create_job("p0")
        processing_id, _, processing_token, _ = self.repository.claim_attempt(
            job_id, "p0", lease_owner="processing"
        )
        self.repository.complete_processing_attempt(
            job_id, "p0", processing_id,
            {"valid": True, "errors": []},
            lease_token=processing_token,
        )
        publication_id, _, publication_token, _ = (
            self.repository.claim_publication_attempt(
                job_id, "p0",
                lease_owner="package-publisher",
                processing_attempt_id=processing_id,
                package_output_key="vod/master.m3u8",
                package_output_bucket="vod-output",
            )
        )
        self.repository.fail_attempt(
            job_id, "p0", publication_id,
            retryable=False,
            error_code="PACKAGE_UPLOAD_FAILED",
            error_message="synthetic failure",
            metrics={"output_bytes": 12},
            lease_token=publication_token,
        )
        with self.repository._session.begin() as session:
            attempt = session.get(AttemptRecord, publication_id)
            attempt.finished_at = utcnow() - timedelta(days=2)
            self.assertEqual(
                attempt.metrics_json["package_publication"]["prefix"],
                "vod/.attempts/{}".format(publication_id),
            )
            self.assertEqual(attempt.metrics_json["output_bytes"], 12)

        candidates = self.repository.list_reclaimable_package_attempts(
            minimum_age_seconds=86400,
            limit=10,
        )
        candidate = next(
            item for item in candidates
            if item["attempt_id"] == publication_id
        )
        self.assertEqual(candidate["bucket"], "vod-output")
        self.assertEqual(
            candidate["inventory_key"],
            "vod/.attempts/{}/.master.m3u8.artifacts.json".format(
                publication_id
            ),
        )

        self.assertTrue(self.repository.mark_package_attempt_cleaned(
            publication_id,
            "vod-output",
            "vod/.attempts/{}".format(publication_id),
        ))
        with self.repository._session() as session:
            cleaned_attempt = session.get(AttemptRecord, publication_id)
            self.assertTrue(
                cleaned_attempt.metrics_json["package_publication"][
                    "cleanup_completed_at"
                ]
            )
        self.assertNotIn(
            publication_id,
            [
                item["attempt_id"]
                for item in self.repository.list_reclaimable_package_attempts(
                    minimum_age_seconds=86400,
                    limit=100,
                )
            ],
        )

    def test_single_file_publication_namespace_is_registered_for_janitor(self):
        job_id = self.create_job("p0")
        processing_id, _, processing_token, _ = self.repository.claim_attempt(
            job_id, "p0", lease_owner="processing"
        )
        self.repository.complete_processing_attempt(
            job_id, "p0", processing_id,
            {"valid": True, "errors": []},
            lease_token=processing_token,
            metrics={
                "cpu_seconds": 0.1,
                "peak_memory_bytes": 1024,
                "memory_duration_ms": 100,
                "output_bytes": 0,
            },
            pricing={
                "model_id": "test-model",
                "model_version": "1",
                "currency": "RUNIT",
                "cpu_per_second": 1,
                "memory_gb_second": 0.25,
                "storage_gb": 0.01,
            },
        )
        publication_id, _, publication_token, _ = (
            self.repository.claim_publication_attempt(
                job_id, "p0",
                lease_owner="file-publisher",
                processing_attempt_id=processing_id,
                package_output_key="vod/thumbnail.jpg",
                package_output_bucket="vod-output",
                package_output_kind="single_file",
            )
        )
        namespace = self.repository.get_attempt_publication_namespace(
            publication_id
        )
        self.assertEqual(namespace["kind"], "single_file")
        self.assertEqual(namespace["bucket"], "vod-output")
        self.assertEqual(
            namespace["object_key"],
            "vod/.attempts/{}/thumbnail.jpg".format(publication_id),
        )
        self.assertNotIn("inventory_key", namespace)

        self.repository.fail_attempt(
            job_id, "p0", publication_id,
            retryable=False,
            error_code="FILE_PUBLICATION_FAILED",
            error_message="synthetic failure",
            metrics={
                "cpu_seconds": 0.01,
                "peak_memory_bytes": 1024,
                "memory_duration_ms": 10,
                "output_bytes": 0,
            },
            pricing={
                "model_id": "test-model",
                "model_version": "1",
                "currency": "RUNIT",
                "cpu_per_second": 1,
                "memory_gb_second": 0.25,
                "storage_gb": 0.01,
            },
            lease_token=publication_token,
        )
        with self.repository._session.begin() as session:
            attempt = session.get(AttemptRecord, publication_id)
            attempt.finished_at = utcnow() - timedelta(days=2)

        candidates = self.repository.list_reclaimable_package_attempts(
            minimum_age_seconds=86400, limit=100
        )
        candidate = next(
            item for item in candidates
            if item["attempt_id"] == publication_id
        )
        self.assertEqual(candidate["kind"], "single_file")
        self.assertEqual(
            candidate["object_key"],
            namespace["object_key"],
        )
        self.assertIsNone(candidate["inventory_key"])

    def test_missing_local_package_invalidates_validation_and_allows_reprocessing(self):
        job_id = self.create_job("p0")
        processing_id, _, token, _ = self.repository.claim_attempt(
            job_id, "p0", lease_owner="playlist-worker"
        )
        self.repository.complete_processing_attempt(
            job_id, "p0", processing_id,
            {"valid": True, "errors": []},
            lease_token=token,
            metrics={
                "local_package": {
                    "directory": "/missing/package",
                    "manifest_path": "/missing/package/master.m3u8",
                    "expected_renditions": ["master_144p.m3u8"],
                    "package_type": "hls",
                },
            },
        )
        self.assertEqual(
            self.repository.get_attempt_local_package(processing_id)[
                "directory"
            ],
            "/missing/package",
        )

        validation_snapshot = self.repository.get_stage_snapshot(
            job_id, "p0"
        )["artifacts"][0]["metadata"]
        self.assertNotIn("local_output_directory", validation_snapshot)
        self.assertNotIn("manifest_path", validation_snapshot)

        self.assertTrue(
            self.repository.requeue_processing_after_local_package_loss(
                job_id,
                "p0",
                processing_id,
                reason="VALIDATED_LOCAL_PACKAGE_MISSING_ON_TASK_REPLAY",
            )
        )
        stage = self.repository.get_stage_snapshot(job_id, "p0")
        self.assertEqual(stage["status"], "RETRY_WAIT")
        validation = next(
            item for item in stage["artifacts"]
            if item["type"] == "validation_report"
        )
        self.assertEqual(validation["status"], "UNAVAILABLE")
        self.assertEqual(
            validation["metadata"]["invalidated_reason"],
            "VALIDATED_LOCAL_PACKAGE_MISSING_ON_TASK_REPLAY",
        )

        replacement_id, _, replacement_token, _ = (
            self.repository.claim_attempt(
                job_id, "p0", lease_owner="playlist-reprocessor"
            )
        )
        self.assertNotEqual(processing_id, replacement_id)
        self.assertTrue(replacement_token)

    def test_expired_publication_claim_recovers_before_publishing_transition(self):
        from unittest.mock import patch

        job_id = self.create_job("p0")
        processing_id, _, processing_token, _ = self.repository.claim_attempt(
            job_id, "p0", lease_owner="worker-processing"
        )
        self.repository.complete_processing_attempt(
            job_id, "p0", processing_id,
            {"valid": True, "errors": []},
            lease_token=processing_token,
        )
        first_id, _, first_token, first_expiry = (
            self.repository.claim_publication_attempt(
                job_id, "p0", lease_owner="publisher-crashed-before-start"
            )
        )

        with patch(
                "video_streaming.workflows.repository.utcnow",
                return_value=first_expiry):
            second_id, number, second_token, _ = (
                self.repository.claim_publication_attempt(
                    job_id, "p0", lease_owner="publisher-after-expiry"
                )
            )

        self.assertNotEqual(first_id, second_id)
        self.assertEqual(number, 3)
        with self.assertRaises(AttemptLeaseLost):
            self.repository.start_publishing(
                job_id, "p0", first_id, lease_token=first_token
            )
        self.repository.start_publishing(
            job_id, "p0", second_id, lease_token=second_token
        )
        stage = self.repository.get_stage_snapshot(job_id, "p0")
        self.assertEqual(stage["status"], "PUBLISHING")
        self.assertEqual(
            [attempt["status"] for attempt in stage["attempts"]],
            ["SUCCEEDED", "RETRYABLE", "RUNNING"],
        )



if __name__ == "__main__":
    unittest.main()
