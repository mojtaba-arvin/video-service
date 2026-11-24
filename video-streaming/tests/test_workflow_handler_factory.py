"""The configured factory must provide exactly the persisted plan allow-list."""
import unittest
from unittest.mock import Mock

from video_streaming.workflows.execution_plan import PLAN_OPERATIONS
from video_streaming.workflows.handler_factory import build_workflow_handlers


class DurableWorkflowHandlerFactoryTests(unittest.TestCase):
    def test_factory_composes_exactly_all_thirteen_handlers(self):
        artifact_store = Mock()
        artifact_store.client = Mock()
        repository = Mock()

        handlers = build_workflow_handlers(
            repository=repository,
            artifact_store=artifact_store,
        )

        self.assertEqual(set(handlers), set(PLAN_OPERATIONS))
        self.assertEqual(len(handlers), 13)
        self.assertTrue(all(callable(handler) for handler in handlers.values()))


if __name__ == "__main__":
    unittest.main()
