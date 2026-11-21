"""Operator CLI tests for fail-closed outbox relay preflight."""

import sys
import types
import unittest
from unittest.mock import Mock, patch

from video_streaming.workflows import outbox_admin


class _Repository:
    def close(self):
        pass


class WorkflowOutboxAdminTests(unittest.TestCase):
    def test_manual_relay_refuses_when_durable_dispatch_is_disabled(self):
        with patch.object(
            outbox_admin, "get_workflow_repository", return_value=_Repository()
        ):
            with patch(
                "video_streaming.settings.WORKFLOW_DURABLE_DISPATCH_ENABLED",
                False,
            ):
                with self.assertRaisesRegex(SystemExit, "disabled"):
                    outbox_admin.main(["relay-once"])

    def test_manual_relay_preflights_complete_handler_registry(self):
        with patch.object(
            outbox_admin, "get_workflow_repository", return_value=_Repository()
        ):
            with patch(
                "video_streaming.settings.WORKFLOW_DURABLE_DISPATCH_ENABLED",
                True,
            ):
                fake_tasks = types.ModuleType("video_streaming.workflows.tasks")
                fake_tasks._build_executor = Mock(
                    side_effect=RuntimeError("handler registry unavailable")
                )
                with patch.dict(
                    sys.modules,
                    {"video_streaming.workflows.tasks": fake_tasks},
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "handler registry unavailable"
                    ):
                        outbox_admin.main(["relay-once"])
                fake_tasks._build_executor.assert_called_once()


if __name__ == "__main__":
    unittest.main()
