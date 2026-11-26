import unittest
from unittest.mock import Mock, call, patch

from video_streaming.workflows.application import (
    SubmitJobCommand,
    SubmitJobService,
)


class SubmitJobServiceTests(unittest.TestCase):
    def setUp(self):
        self.principal = Mock(tenant_id="tenant-a", issuer="issuer-a", subject="subject-a")
        self.repository = Mock()
        self.command = SubmitJobCommand(
            job_id="job-new",
            idempotency_key="client-key",
            request_hash="request-hash",
            reference_id="reference-1",
            execution_plan={"schema_version": 1, "nodes": [{"key": "probe"}]},
            execution_backend="legacy_celery",
            enqueue_dispatch=False,
            outbox_dedupe_key=None,
            principal=self.principal,
            project_id=None,
            project_revision_id=None,
            secret_envelopes=(),
            quota_enforcement_enabled=False,
            metadata={"result_details": {"total_outputs": 1}},
        )

    @patch("video_streaming.workflows.application.require_scope")
    def test_existing_idempotent_job_is_returned_without_creating_another(self, require_scope):
        self.repository.find_idempotent_job.return_value = "job-existing"

        result = SubmitJobService(self.repository).execute(self.command)

        self.assertEqual(result.job_id, "job-existing")
        self.assertFalse(result.created)
        require_scope.assert_called_once_with(self.principal, "video.jobs:create")
        self.repository.create_job.assert_not_called()

    @patch("video_streaming.workflows.application.require_scope")
    def test_new_job_is_persisted_with_dispatch_and_security_options(self, require_scope):
        self.repository.find_idempotent_job.return_value = None
        self.repository.create_job.return_value = ("job-new", True)

        result = SubmitJobService(self.repository).execute(self.command)

        self.assertEqual(result.job_id, "job-new")
        self.assertTrue(result.created)
        kwargs = self.repository.create_job.call_args.kwargs
        self.assertEqual(kwargs["execution_backend"], "legacy_celery")
        self.assertFalse(kwargs["enqueue_dispatch"])
        self.assertIsNone(kwargs["outbox_dedupe_key"])
        self.assertIs(kwargs["owner_principal"], self.principal)
        self.assertEqual(kwargs["metadata"], self.command.metadata)
        require_scope.assert_called_once_with(self.principal, "video.jobs:create")

    @patch("video_streaming.workflows.application.require_scope")
    def test_project_revision_requires_project_read_scope(self, require_scope):
        self.repository.find_idempotent_job.return_value = None
        self.repository.create_job.return_value = ("job-new", True)
        command = SubmitJobCommand(
            **{
                **self.command.__dict__,
                "project_id": "project-a",
                "project_revision_id": "revision-a",
            }
        )

        SubmitJobService(self.repository).execute(command)

        self.assertEqual(
            require_scope.call_args_list,
            [
                call(self.principal, "video.jobs:create"),
                call(self.principal, "video.projects:read"),
            ],
        )

    @patch("video_streaming.workflows.application.require_scope")
    def test_partial_project_reference_is_rejected_before_repository_access(self, require_scope):
        command = SubmitJobCommand(
            **{**self.command.__dict__, "project_id": "project-a"}
        )

        with self.assertRaisesRegex(ValueError, "must be supplied together"):
            SubmitJobService(self.repository).execute(command)

        self.repository.find_idempotent_job.assert_not_called()
        self.repository.create_job.assert_not_called()

    @patch("video_streaming.workflows.application.require_scope")
    def test_scope_failure_prevents_repository_access(self, require_scope):
        require_scope.side_effect = PermissionError("denied")

        with self.assertRaises(PermissionError):
            SubmitJobService(self.repository).execute(self.command)

        self.repository.find_idempotent_job.assert_not_called()
        self.repository.create_job.assert_not_called()


if __name__ == "__main__":
    unittest.main()
