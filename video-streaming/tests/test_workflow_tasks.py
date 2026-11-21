import unittest
from unittest.mock import patch

from video_streaming import settings
from video_streaming.workflows import tasks


class WorkflowTaskConsumerTests(unittest.TestCase):
    def test_dispatch_is_disabled_by_default_and_fails_closed(self):
        with patch.object(
            settings, "WORKFLOW_DURABLE_DISPATCH_ENABLED", False
        ):
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                tasks.dispatch_plan.run(
                    "job", "workflow", "1", "a" * 64
                )

    def test_dispatch_rejects_plan_identity_mismatch_before_fanout(self):
        class Repository:
            def get_execution_plan(self, job_id):
                return {
                    "workflow_id": "workflow-actual",
                    "workflow_version": "1",
                    "fingerprint": "b" * 64,
                    "plan": {"nodes": []},
                }

        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            tasks._assert_dispatch_identity(
                Repository(), "job", "workflow-expected", "1", "a" * 64
            )

    def test_handler_factory_is_mandatory_and_explicit(self):
        with patch.object(settings, "WORKFLOW_HANDLER_FACTORY", ""):
            with self.assertRaisesRegex(RuntimeError, "module:callable"):
                tasks._load_handler_factory()


if __name__ == "__main__":
    unittest.main()
