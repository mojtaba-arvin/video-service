"""Dependency-free tests for normalized CDN delivery event contracts."""

import unittest
from datetime import datetime, timedelta, timezone

from video_streaming.workflows.delivery import (
    DeliveryEvent,
    DeliveryEventContractError,
)


class DeliveryEventContractTests(unittest.TestCase):

    def make_event(self, **overrides):
        values = {
            "source": "cloudfront",
            "source_event_id": "request-0001",
            "source_timestamp": datetime(
                2026, 11, 26, 10, 0, tzinfo=timezone.utc
            ),
            "artifact_id": "manifest-artifact-01",
            "session_id": "session-01",
            "asset_version": "asset-v7",
            "delivery_config_id": "config-v2",
            "edge_region": "eu-central",
            "cache_status": "HIT",
            "http_status": 200,
            "response_time_ms": 42,
            "bytes_sent": 8192,
        }
        values.update(overrides)
        return DeliveryEvent(**values)

    def test_normalizes_timezone_and_exposes_only_allowlisted_fields(self):
        event = self.make_event(
            source_timestamp=datetime(
                2026, 11, 26, 12, 0,
                tzinfo=timezone(timedelta(hours=2)),
            )
        )

        payload = event.canonical_payload()

        self.assertEqual(payload["source_timestamp"], "2026-11-26T10:00:00.000000Z")
        self.assertEqual(payload["artifact_id"], "manifest-artifact-01")
        self.assertEqual(payload["session_id"], "session-01")
        self.assertEqual(
            set(payload),
            {
                "source", "source_event_id", "source_timestamp",
                "artifact_id", "session_id", "asset_version", "delivery_config_id",
                "edge_region", "cache_status",
                "http_status", "response_time_ms", "bytes_sent",
                "schema_version",
            },
        )

    def test_equivalent_timestamp_offsets_have_same_fingerprint(self):
        first = self.make_event()
        second = self.make_event(
            source_timestamp=datetime(
                2026, 11, 26, 12, 0,
                tzinfo=timezone(timedelta(hours=2)),
            )
        )

        self.assertEqual(first.fingerprint(), second.fingerprint())

    def test_semantic_change_changes_fingerprint(self):
        first = self.make_event()
        changed = self.make_event(bytes_sent=8193)

        self.assertNotEqual(first.fingerprint(), changed.fingerprint())

    def test_requires_timezone_aware_source_timestamp(self):
        with self.assertRaises(DeliveryEventContractError):
            self.make_event(source_timestamp=datetime(2026, 11, 26, 10, 0))

    def test_rejects_unbounded_or_invalid_identifiers(self):
        for values in (
            {"source": "CloudFront"},
            {"source_event_id": " "},
            {"source_event_id": "x" * 129},
            {"artifact_id": "bad\nartifact"},
            {"edge_region": "x" * 65},
            {"edge_region": "192.0.2.1"},
            {"edge_region": "EU-WEST"},
        ):
            with self.subTest(values=values):
                with self.assertRaises(DeliveryEventContractError):
                    self.make_event(**values)

    def test_rejects_invalid_metrics_and_cache_status(self):
        for values in (
            {"cache_status": "PRIVATE"},
            {"cache_status": []},
            {"schema_version": "1"},
            {"schema_version": 1.0},
            {"http_status": 99},
            {"http_status": True},
            {"response_time_ms": -1},
            {"response_time_ms": 86400001},
            {"bytes_sent": -1},
            {"bytes_sent": True},
        ):
            with self.subTest(values=values):
                with self.assertRaises(DeliveryEventContractError):
                    self.make_event(**values)

    def test_optional_session_and_delivery_metrics_are_supported(self):
        event = self.make_event(
            session_id=None,
            edge_region=None,
            cache_status="UNKNOWN",
            http_status=None,
            response_time_ms=None,
            bytes_sent=None,
        )

        payload = event.canonical_payload()

        self.assertIsNone(payload["session_id"])
        self.assertIsNone(payload["edge_region"])
        self.assertIsNone(payload["http_status"])
        self.assertIsNone(payload["response_time_ms"])
        self.assertIsNone(payload["bytes_sent"])


if __name__ == "__main__":
    unittest.main()
