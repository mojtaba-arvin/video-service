import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from video_streaming.grpc.servicers.mixins.get_results import GetResultsMixin
from video_streaming.workflows.application import GetJobStatusService


class GetJobStatusServiceTests(unittest.TestCase):
    def setUp(self):
        self.repository = Mock()
        self.principal = Mock(
            tenant_id="tenant-a", issuer="issuer-a", subject="subject-a"
        )

    def test_authorizes_entire_batch_before_loading_any_snapshot(self):
        events = []
        self.repository.authorize_job_access.side_effect = (
            lambda job_id, principal, scope: events.append(
                ("authorize", job_id)
            )
        )
        self.repository.get_job_snapshot.side_effect = (
            lambda job_id: (
                events.append(("snapshot", job_id)) or {"id": job_id}
            )
        )

        result = GetJobStatusService(self.repository).execute(
            ["job-a", "job-b"], self.principal
        )

        self.assertEqual(
            result,
            {"job-a": {"id": "job-a"}, "job-b": {"id": "job-b"}},
        )
        self.assertEqual(
            events,
            [
                ("authorize", "job-a"),
                ("authorize", "job-b"),
                ("snapshot", "job-a"),
                ("snapshot", "job-b"),
            ],
        )
        self.assertEqual(
            self.repository.authorize_job_access.call_args_list,
            [
                call("job-a", self.principal, "video.jobs:read"),
                call("job-b", self.principal, "video.jobs:read"),
            ],
        )

    def test_denied_job_prevents_all_snapshot_reads(self):
        self.repository.authorize_job_access.side_effect = [
            None,
            PermissionError("denied"),
        ]

        with self.assertRaises(PermissionError):
            GetJobStatusService(self.repository).execute(
                ["job-a", "job-b"], self.principal
            )

        self.repository.get_job_snapshot.assert_not_called()

    @patch(
        "video_streaming.grpc.servicers.mixins.get_results."
        "get_workflow_repository"
    )
    def test_empty_grpc_status_batch_does_not_require_repository(
        self, get_repository
    ):
        mixin = GetResultsMixin()
        mixin.pb2 = Mock()
        response = object()
        mixin.pb2.JobsResultsResponse.return_value = response

        result = mixin._get_results(
            SimpleNamespace(tracking_ids=[]), context=None
        )

        self.assertIs(result, response)
        mixin.pb2.JobsResultsResponse.assert_called_once_with(results=[])
        get_repository.assert_not_called()

    def test_empty_batch_does_not_touch_repository(self):
        self.assertEqual(
            GetJobStatusService(self.repository).execute([], self.principal),
            {},
        )
        self.repository.authorize_job_access.assert_not_called()
        self.repository.get_job_snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
