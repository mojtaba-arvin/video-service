"""Regression tests for the durable VOD operation contract catalogue."""
import unittest

from video_streaming.workflows.execution_plan import PLAN_OPERATIONS
from video_streaming.workflows.operation_contracts import OPERATION_CONTRACTS


class OperationContractTests(unittest.TestCase):
    def test_every_plan_operation_has_exactly_one_contract(self):
        self.assertEqual(set(OPERATION_CONTRACTS), set(PLAN_OPERATIONS))
        self.assertEqual(len(OPERATION_CONTRACTS), 13)

    def test_executor_is_the_only_attempt_owner(self):
        for contract in OPERATION_CONTRACTS.values():
            with self.subTest(operation=contract.operation):
                self.assertEqual(contract.attempt_owner, "workflow_executor")
                self.assertFalse(contract.legacy_task_may_claim_attempt)

    def test_inter_node_outputs_are_durable_artifacts(self):
        for contract in OPERATION_CONTRACTS.values():
            with self.subTest(operation=contract.operation):
                self.assertTrue(contract.outputs_must_be_durable_artifacts)
                self.assertTrue(contract.outputs)
                self.assertFalse(
                    any("local_path" in name or name.endswith(".path")
                        for name in contract.outputs)
                )

    def test_retry_cancellation_and_idempotency_are_explicit(self):
        for contract in OPERATION_CONTRACTS.values():
            with self.subTest(operation=contract.operation):
                self.assertTrue(contract.retry_class)
                self.assertTrue(contract.cancellation)
                self.assertTrue(contract.idempotency)

    def test_watermarked_media_inputs_are_alternatives_not_all_required(self):
        for operation in (
            "video.hls.package",
            "video.dash.package",
            "video.thumbnail.generate",
        ):
            with self.subTest(operation=operation):
                contract = OPERATION_CONTRACTS[operation]
                self.assertEqual(
                    contract.one_of_artifacts,
                    frozenset({"inputs.manifest", "watermark.video"}),
                )
                self.assertFalse(contract.required_artifacts)

    def test_funnel_requires_video_and_allows_optional_watermark_metadata(self):
        contract = OPERATION_CONTRACTS["video.inputs.funnel"]
        self.assertEqual(
            contract.required_artifacts,
            frozenset({
                "input.video.file_artifact",
                "input.video.probe_metadata",
            })
        )
        self.assertEqual(
            contract.optional_artifacts,
            frozenset({
                "input.watermark.file_artifact",
                "input.watermark.probe_metadata",
            }),
        )

    def test_analyze_passes_through_source_artifact_for_funnel(self):
        contract = OPERATION_CONTRACTS["video.input.analyze"]
        self.assertEqual(
            contract.outputs,
            frozenset({
                "input.{input_name}.file_artifact",
                "input.{input_name}.probe_metadata",
            }),
        )

    def test_webhook_secret_is_a_parameter_not_a_dependency_artifact(self):
        contract = OPERATION_CONTRACTS["video.webhook.deliver"]
        self.assertIn("webhook_secret_ref", contract.optional_parameters)
        self.assertFalse(contract.inputs)
        self.assertEqual(contract.outputs, frozenset({"webhook.delivery_receipt"}))

    def test_file_upload_accepts_exactly_one_publishable_media_artifact(self):
        contract = OPERATION_CONTRACTS["video.output.upload_file"]
        self.assertEqual(
            contract.one_of_artifacts,
            frozenset({"thumbnail.image", "watermark.video"}),
        )


if __name__ == "__main__":
    unittest.main()
