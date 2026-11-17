"""PostgreSQL-backed tests for durable workflow state and recovery."""

import os
import threading
import unittest
from datetime import timedelta
from uuid import uuid4

from video_streaming.workflows.domain import (
    IdempotencyConflict,
    InvalidStateTransition,
    StageAlreadyRunning,
    StageState,
)
from video_streaming.workflows.repository import WorkflowRepository, utcnow


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
        self.assertEqual(recovered, 1)
        third_id, third_number = self.repository.start_attempt(job_id, "p0")
        self.assertNotEqual(second_id, third_id)
        self.assertEqual(third_number, second_number + 1)

    def test_concurrent_idempotency_key_creates_only_one_job(self):
        job_id = str(uuid4())
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
            threading.Thread(target=submit, args=(job_id,)),
            threading.Thread(target=submit, args=(str(uuid4()),)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(errors, errors)
        self.assertEqual(len(results), 2)
        self.assertEqual({item[0] for item in results}, {job_id})
        self.assertEqual(sum(1 for _, created in results if created), 1)
        self.assertEqual(len(self.repository.get_job_snapshot(job_id)["stages"]), 1)


if __name__ == "__main__":
    unittest.main()
