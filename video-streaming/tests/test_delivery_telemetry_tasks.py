"""Tests for scheduled bounded CDN telemetry retention cleanup."""

import unittest
from unittest.mock import patch

from video_streaming.workflows import tasks


class DeliveryTelemetryTaskTests(unittest.TestCase):

    def test_retention_task_deletes_one_bounded_batch(self):
        with patch.object(tasks.settings, "WORKFLOW_DATABASE_URL", "postgresql://test"), \
                patch.object(tasks.settings, "CDN_TELEMETRY_RETENTION_DAYS", 14), \
                patch.object(tasks, "get_workflow_repository") as get_repository, \
                patch.object(tasks, "DeliveryTelemetryRepository") as telemetry_type:
            get_repository.return_value = object()
            telemetry_type.return_value.purge_expired.return_value = 23

            result = tasks.purge_delivery_telemetry.run()

        telemetry_type.assert_called_once_with(
            get_repository.return_value, retention_days=14
        )
        telemetry_type.return_value.purge_expired.assert_called_once_with(
            limit=5000
        )
        self.assertEqual(result, {
            "status": "purged",
            "deleted": 23,
            "retention_days": 14,
        })

    def test_retention_task_is_inert_without_workflow_database(self):
        with patch.object(tasks.settings, "WORKFLOW_DATABASE_URL", ""), \
                patch.object(tasks, "get_workflow_repository") as get_repository:
            result = tasks.purge_delivery_telemetry.run()

        get_repository.assert_not_called()
        self.assertEqual(result, {"status": "disabled", "deleted": 0})


if __name__ == "__main__":
    unittest.main()
