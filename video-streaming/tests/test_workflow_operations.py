"""Tests for the typed internal operation contract."""

import unittest

from video_streaming.workflows.operations import (
    OperationArtifact,
    OperationError,
    OperationKind,
    OperationRequest,
    OperationResult,
)


class WorkflowOperationContractTests(unittest.TestCase):

    def test_request_requires_identity_and_typed_operation(self):
        request = OperationRequest(
            job_id="job-1",
            stage_key="p0",
            operation=OperationKind.HLS_PACKAGE_AND_PUBLISH,
            parameters={"protocol": "hls", "quality_names": ["360p"]},
            input_artifacts=["s3://input/source.mp4"],
        )
        self.assertEqual(request.operation.value, "video.hls.package_and_publish")
        with self.assertRaises(ValueError):
            OperationRequest("", "p0", OperationKind.PROBE)
        with self.assertRaises(TypeError):
            OperationRequest("job-1", "p0", "video.probe")

    def test_result_carries_artifacts_metrics_and_structured_errors(self):
        result = OperationResult(
            job_id="job-1",
            stage_key="p0",
            status="FINISHED",
            artifacts=[
                OperationArtifact(
                    artifact_type="streaming_manifest",
                    logical_name="master.m3u8",
                    uri="s3://output/master.m3u8",
                    media_type="application/vnd.apple.mpegurl",
                    size_bytes=128,
                )
            ],
            metrics={"duration_ms": 1200, "output_bytes": 1024},
            errors=[
                OperationError(
                    code="RESOLUTION_MISMATCH",
                    message="Manifest and rendition dimensions differ",
                    location="variant_720p.m3u8",
                )
            ],
        )
        self.assertEqual(result.artifacts[0].size_bytes, 128)
        self.assertEqual(result.metrics["duration_ms"], 1200)
        self.assertEqual(result.errors[0].code, "RESOLUTION_MISMATCH")


if __name__ == "__main__":
    unittest.main()
