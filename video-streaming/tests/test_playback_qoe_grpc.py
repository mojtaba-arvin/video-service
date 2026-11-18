"""Dependency-free tests for the playback QoE response projection."""

import unittest

from video_streaming.workflows.qoe import playback_qoe_report_payload


class PlaybackQoEContractTests(unittest.TestCase):

    def test_report_exposes_partial_and_missing_cost_coverage(self):
        report = {
            "job_id": "job-1",
            "stage_key": "playlist-0",
            "stage_status": "FINISHED",
            "session_count": 3,
            "playback_started_count": 2,
            "average_startup_ms": 125.5,
            "rebuffer_count": 1,
            "total_buffering_ms": 850,
            "decoder_error_count": 0,
            "buffering_ratio_percent": 0.8,
            "total_cpu_seconds": 42.5,
            "total_estimated_cost": None,
            "partial_estimated_cost": 12.75,
            "cost_estimate_complete": False,
            "cost_coverage": {
                "complete_attempt_count": 1,
                "missing_dimensions_by_attempt": {
                    "gpu_seconds": 2,
                    "egress_bytes": 1,
                },
                "excluded_dimensions": [
                    "provider_invoice",
                    "worker_idle",
                ],
                "pricing_model_versions": ["relative-resource-index-v1:1"],
            },
            "cost_currency": None,
            "pricing_model_id": None,
            "priced_attempt_count": 2,
            "total_attempt_count": 3,
        }

        payload = playback_qoe_report_payload(report)

        self.assertEqual(payload["total_estimated_cost"], 0.0)
        self.assertFalse(payload["total_estimated_cost_available"])
        self.assertEqual(payload["partial_estimated_cost"], 12.75)
        self.assertTrue(payload["partial_estimated_cost_available"])
        self.assertEqual(payload["complete_attempt_count"], 1)
        self.assertEqual(
            payload["missing_cost_dimensions_by_attempt"],
            {"gpu_seconds": 2, "egress_bytes": 1},
        )
        self.assertEqual(
            payload["excluded_cost_dimensions"],
            ["provider_invoice", "worker_idle"],
        )
        self.assertEqual(
            payload["pricing_model_versions"],
            ["relative-resource-index-v1:1"],
        )
        self.assertEqual(payload["priced_attempt_count"], 2)
        self.assertEqual(payload["total_attempt_count"], 3)

    def test_complete_total_is_marked_available(self):
        report = {
            "job_id": "job-2",
            "stage_key": "playlist-0",
            "stage_status": "FINISHED",
            "session_count": 1,
            "playback_started_count": 1,
            "average_startup_ms": 10,
            "rebuffer_count": 0,
            "total_buffering_ms": 0,
            "decoder_error_count": 0,
            "buffering_ratio_percent": 0.0,
            "total_cpu_seconds": 5.0,
            "total_estimated_cost": 3.25,
            "partial_estimated_cost": 3.25,
            "cost_estimate_complete": True,
            "cost_coverage": {
                "complete_attempt_count": 1,
                "missing_dimensions_by_attempt": {},
                "excluded_dimensions": ["provider_invoice"],
                "pricing_model_versions": ["custom-card-v2"],
            },
            "cost_currency": "USD",
            "pricing_model_id": "production-prices",
            "priced_attempt_count": 1,
            "total_attempt_count": 1,
        }

        payload = playback_qoe_report_payload(report)

        self.assertTrue(payload["total_estimated_cost_available"])
        self.assertEqual(payload["total_estimated_cost"], 3.25)
        self.assertTrue(payload["partial_estimated_cost_available"])
        self.assertEqual(payload["cost_currency"], "USD")


