"""Contract tests for provider-neutral workload classification."""

import unittest

from video_streaming.workflows.operations import OperationKind
from video_streaming.workflows.workload import (
    WorkloadClass,
    classify_operation,
)


class WorkloadClassificationTests(unittest.TestCase):

    def test_every_typed_operation_has_an_explicit_classification(self):
        expected = {
            OperationKind.PROBE: WorkloadClass.METADATA,
            OperationKind.VALIDATE_PACKAGE: WorkloadClass.METADATA,
            OperationKind.PUBLISH_PACKAGE: WorkloadClass.STORAGE_IO,
            OperationKind.HLS_PACKAGE_AND_PUBLISH: WorkloadClass.PACKAGING,
            OperationKind.DASH_PACKAGE_AND_PUBLISH: WorkloadClass.PACKAGING,
            OperationKind.THUMBNAIL_AND_PUBLISH: WorkloadClass.MEDIA_TRANSFORM,
            OperationKind.WATERMARK_AND_PUBLISH: WorkloadClass.MEDIA_TRANSFORM,
            OperationKind.TRIM: WorkloadClass.MEDIA_TRANSFORM,
            OperationKind.TRANSCODE: WorkloadClass.MEDIA_TRANSFORM,
        }

        self.assertEqual(set(expected), set(OperationKind))
        for operation, workload_class in expected.items():
            with self.subTest(operation=operation.value):
                self.assertIs(classify_operation(operation), workload_class)

    def test_all_thirteen_durable_vod_operations_are_classified(self):
        from video_streaming.workflows.execution_plan import PLAN_OPERATIONS

        expected = {
            "video.input.check": WorkloadClass.METADATA,
            "video.input.download": WorkloadClass.STORAGE_IO,
            "video.input.analyze": WorkloadClass.METADATA,
            "video.inputs.funnel": WorkloadClass.METADATA,
            "video.output.bucket.check": WorkloadClass.METADATA,
            "video.output.key.check": WorkloadClass.METADATA,
            "video.hls.package": WorkloadClass.PACKAGING,
            "video.dash.package": WorkloadClass.PACKAGING,
            "video.thumbnail.generate": WorkloadClass.MEDIA_TRANSFORM,
            "video.watermark.apply": WorkloadClass.MEDIA_TRANSFORM,
            "video.output.upload_file": WorkloadClass.STORAGE_IO,
            "video.output.upload_directory": WorkloadClass.STORAGE_IO,
            "video.webhook.deliver": WorkloadClass.STORAGE_IO,
        }

        self.assertEqual(set(expected), set(PLAN_OPERATIONS))
        for operation, workload_class in expected.items():
            with self.subTest(operation=operation):
                self.assertIs(classify_operation(operation), workload_class)

    def test_exact_wire_value_is_accepted(self):
        self.assertIs(
            classify_operation("video.probe"),
            WorkloadClass.METADATA,
        )

    def test_unknown_or_untyped_operations_fail_closed(self):
        for operation in ("video.unknown", None, 123):
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError):
                    classify_operation(operation)


if __name__ == "__main__":
    unittest.main()
