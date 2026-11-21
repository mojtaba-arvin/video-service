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


if __name__ == "__main__":
    unittest.main()
