"""PostgreSQL/SQLite contract tests for durable CDN telemetry correlation."""

import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select

from video_streaming.workflows.delivery import DeliveryEvent
from video_streaming.workflows.delivery_repository import (
    DeliveryEventConflictError,
    DeliveryEventRecord,
    DeliveryTelemetryRepository,
    ProviderNeutralCDNAdapter,
)
from video_streaming.workflows.repository import (
    ArtifactRecord,
    JobRecord,
    PlaybackEventRecord,
    PlaybackSessionRecord,
    StageRecord,
    WorkflowRecord,
    WorkflowRepository,
    utcnow,
)


class DeliveryTelemetryRepositoryTests(unittest.TestCase):

    def setUp(self):
        self.workflow = WorkflowRepository("sqlite://", create_schema=True)
        self.telemetry = DeliveryTelemetryRepository(
            self.workflow, retention_days=2
        )
        self.job_id = str(uuid4())
        self.workflow.create_job(
            job_id=self.job_id,
            idempotency_key="delivery-test-" + str(uuid4()),
            request_hash="a" * 64,
            reference_id="delivery-test",
            workflow_name="video.vod",
            stage_specs=[{
                "stage_key": "output-0",
                "operation": "video.package_and_publish",
                "input": {"protocol": "hls"},
            }],
        )
        with self.workflow._session.begin() as session:
            self.job = session.get(JobRecord, self.job_id)
            self.workflow_record = session.execute(
                select(WorkflowRecord).where(
                    WorkflowRecord.job_id == self.job_id
                )
            ).scalar_one()
            self.stage = session.execute(
                select(StageRecord).where(
                    StageRecord.workflow_id == self.workflow_record.id
                )
            ).scalar_one()
            self.stage.status = "FINISHED"
            self.artifact_id = str(uuid4())
            session.add(ArtifactRecord(
                id=self.artifact_id,
                job_id=self.job_id,
                stage_id=self.stage.id,
                attempt_id=None,
                artifact_type="streaming_manifest",
                logical_name="master.m3u8",
                uri="s3://test-bucket/output/master.m3u8",
                media_type="application/vnd.apple.mpegurl",
                size_bytes=128,
                checksum_sha256="b" * 64,
                status="AVAILABLE",
                metadata_json={"asset_version": "asset-v7"},
                created_at=utcnow(),
            ))
            self.session_id = str(uuid4())
            session.add(PlaybackSessionRecord(
                id=self.session_id,
                job_id=self.job_id,
                stage_id=self.stage.id,
                manifest_artifact_id=self.artifact_id,
                token_hash="c" * 64,
                client_version="test-player",
                status="ACTIVE",
                started_at=utcnow(),
                last_event_at=utcnow(),
            ))
            self.session_without_delivery = str(uuid4())
            session.add(PlaybackSessionRecord(
                id=self.session_without_delivery,
                job_id=self.job_id,
                stage_id=self.stage.id,
                manifest_artifact_id=self.artifact_id,
                token_hash="d" * 64,
                client_version="test-player",
                status="ACTIVE",
                started_at=utcnow(),
                last_event_at=utcnow(),
            ))
            session.add(PlaybackEventRecord(
                id=str(uuid4()),
                session_id=self.session_id,
                event_id="player-start-1",
                event_type="PLAYBACK_STARTED",
                payload_hash="e" * 64,
                received_at=utcnow(),
                startup_ms=320,
                metadata_json={},
            ))

    def tearDown(self):
        self.workflow.close()

    def event(self, **overrides):
        values = {
            "source": "cdn-a",
            "source_event_id": "request-1",
            "source_timestamp": datetime.now(timezone.utc),
            "artifact_id": self.artifact_id,
            "session_id": self.session_id,
            "asset_version": "asset-v7",
            "delivery_config_id": "delivery-config-v3",
            "edge_region": "eu-central",
            "cache_status": "HIT",
            "http_status": 200,
            "response_time_ms": 38,
            "bytes_sent": 4096,
        }
        values.update(overrides)
        return DeliveryEvent(**values)

    def test_ingestion_is_idempotent_and_conflicting_replay_is_rejected(self):
        event = self.event()
        adapter = ProviderNeutralCDNAdapter(self.telemetry, "cdn-a")
        first = adapter.ingest(event)
        replay = adapter.ingest(event)

        self.assertFalse(first["duplicate"])
        self.assertTrue(replay["duplicate"])
        self.assertEqual(first["event_id"], replay["event_id"])
        with self.workflow._session.begin() as session:
            session.get(ArtifactRecord, self.artifact_id).status = "RETIRED"
        self.assertTrue(adapter.ingest(event)["duplicate"])
        with self.assertRaises(DeliveryEventConflictError):
            adapter.ingest(self.event(bytes_sent=4097))
        with self.assertRaises(ValueError):
            ProviderNeutralCDNAdapter(self.telemetry, "cdn-a").ingest(
                self.event(source="cdn-b", source_event_id="request-2")
            )

    def test_correlation_uses_exact_published_manifest_and_session(self):
        result = self.telemetry.ingest(self.event())
        report = self.telemetry.query(self.job_id, stage_key="output-0")

        self.assertTrue(result["accepted"])
        observed = report["observed_events"][0]
        self.assertEqual(observed["artifact_id"], self.artifact_id)
        self.assertEqual(observed["manifest_version"], self.artifact_id)
        self.assertEqual(observed["session_id"], self.session_id)
        self.assertEqual(observed["stage_id"], self.stage.id)
        self.assertEqual(observed["workflow_id"], self.workflow_record.id)
        self.assertEqual(observed["asset_version"], "asset-v7")
        self.assertEqual(observed["delivery_config_id"], "delivery-config-v3")

    def test_session_must_reference_the_same_manifest(self):
        with self.assertRaises(ValueError):
            self.telemetry.ingest(self.event(session_id="unrelated-session"))

    def test_incomplete_delivery_and_player_signals_remain_missing_not_zero(self):
        self.telemetry.ingest(self.event(
            session_id=None,
            asset_version=None,
            cache_status="UNKNOWN",
            http_status=None,
            response_time_ms=None,
            bytes_sent=None,
        ))
        report = self.telemetry.query(self.job_id, stage_key="output-0")
        coverage = report["coverage"]

        self.assertEqual(coverage["delivery_event_count"], 1)
        self.assertEqual(coverage["delivery_events_without_session"], 1)
        self.assertEqual(coverage["missing_asset_version_count"], 1)
        self.assertEqual(coverage["missing_http_status_count"], 1)
        self.assertEqual(coverage["missing_edge_region_count"], 1)
        self.assertEqual(coverage["missing_response_time_count"], 1)
        self.assertEqual(coverage["missing_bytes_count"], 1)
        self.assertEqual(coverage["unknown_cache_outcome_count"], 1)
        self.assertEqual(coverage["sessions_with_player_signals"], 1)
        self.assertEqual(coverage["sessions_without_player_signals"], 1)
        self.assertEqual(coverage["sessions_without_delivery_signals"], 2)
        aggregate = report["derived_aggregates"][0]
        self.assertIsNone(aggregate["average_response_time_ms"])
        self.assertIsNone(aggregate["bytes_sent"])
        self.assertEqual(aggregate["event_count"], 1)

    def test_query_filters_and_aggregation_windows_are_provider_neutral(self):
        self.telemetry.ingest(self.event())
        report = self.telemetry.query(
            self.job_id,
            stage_key="output-0",
            source="cdn-a",
            edge_region="eu-central",
            cache_status="HIT",
            asset_version="asset-v7",
            delivery_config_id="delivery-config-v3",
            aggregation_window_seconds=60,
            limit=10,
        )

        self.assertEqual(len(report["observed_events"]), 1)
        self.assertEqual(report["aggregation_window_seconds"], 60)
        self.assertEqual(report["derived_aggregates"][0]["event_count"], 1)
        self.assertEqual(report["derived_aggregates"][0]["source"], "cdn-a")
        self.assertEqual(report["derived_aggregates"][0]["manifest_version"], self.artifact_id)
        self.assertEqual(report["derived_aggregates"][0]["asset_version"], "asset-v7")
        self.assertEqual(report["derived_aggregates"][0]["delivery_config_id"], "delivery-config-v3")
        self.assertEqual(
            report["derived_aggregates"][0]["average_response_time_ms"], 38
        )
        self.assertEqual(report["derived_aggregates"][0]["bytes_sent"], 4096)

    def test_query_rejects_unbounded_windows_limits_and_naive_timestamps(self):
        for kwargs in (
            {"aggregation_window_seconds": 1},
            {"aggregation_window_seconds": 86401},
            {"limit": 1001},
            {"start_time": datetime(2026, 11, 28, 0, 0)},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.telemetry.query(self.job_id, **kwargs)

    def test_retention_purge_is_bounded_and_repeatable(self):
        self.telemetry.ingest(self.event())
        self.telemetry.ingest(self.event(source_event_id="request-2"))
        after_expiry = utcnow() + timedelta(days=3)

        self.assertEqual(
            self.telemetry.purge_expired(now=after_expiry, limit=1), 1
        )
        self.assertEqual(
            self.telemetry.purge_expired(now=after_expiry, limit=1), 1
        )
        self.assertEqual(
            self.telemetry.purge_expired(now=after_expiry, limit=1), 0
        )
        self.assertEqual(
            self.telemetry.query(self.job_id)["coverage"]["delivery_event_count"],
            0,
        )

    def test_retention_policy_has_a_hard_upper_bound(self):
        for days in (0, 91, True, 2.5):
            with self.subTest(days=days):
                with self.assertRaises(ValueError):
                    DeliveryTelemetryRepository(self.workflow, retention_days=days)


if __name__ == "__main__":
    unittest.main()
