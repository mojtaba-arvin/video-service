"""Tests for lossless playback QoE and cost-coverage gRPC projection."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from video_streaming.grpc.servicers.mixins.playback_qoe import PlaybackQoEMixin


class _Response:
    def __init__(self, **values):
        self.__dict__.update(values)


class _FakePB2:
    PlaybackQoEReportResponse = _Response


class _Context:
    def abort(self, code, message):
        raise AssertionError("{}: {}".format(code, message))


class _Servicer(PlaybackQoEMixin):
    pb2 = _FakePB2()


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
        repository = SimpleNamespace(
            get_playback_qoe_report=lambda **kwargs: report
        )
        request = SimpleNamespace(
            tracking_id="job-1",
            output_id="playlist-0",
        )

        with patch(
            "video_streaming.grpc.servicers.mixins.playback_qoe."
            "get_workflow_repository",
            return_value=repository,
        ):
            response = _Servicer().get_playback_qoe_report(
                request, _Context()
            )

        self.assertEqual(response.total_estimated_cost, 0.0)
        self.assertFalse(response.total_estimated_cost_available)
        self.assertEqual(response.partial_estimated_cost, 12.75)
        self.assertTrue(response.partial_estimated_cost_available)
        self.assertEqual(response.complete_attempt_count, 1)
        self.assertEqual(
            response.missing_cost_dimensions_by_attempt,
            {"gpu_seconds": 2, "egress_bytes": 1},
        )
        self.assertEqual(
            response.excluded_cost_dimensions,
            ["provider_invoice", "worker_idle"],
        )
        self.assertEqual(
            response.pricing_model_versions,
            ["relative-resource-index-v1:1"],
        )
        self.assertEqual(response.priced_attempt_count, 2)
        self.assertEqual(response.total_attempt_count, 3)


if __name__ == "__main__":
    unittest.main()
