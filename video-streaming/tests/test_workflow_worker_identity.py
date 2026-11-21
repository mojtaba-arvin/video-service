import unittest

from video_streaming.workflows.worker_identity import (
    WorkflowWorkerIdentity,
    authenticate_workflow_worker,
)


class _STS:
    def __init__(self, identity=None, error=None):
        self.identity = identity or {}
        self.error = error

    def get_caller_identity(self):
        if self.error:
            raise self.error
        return self.identity


class WorkflowWorkerIdentityTests(unittest.TestCase):
    def test_accepts_only_configured_assumed_role_prefix(self):
        identity = authenticate_workflow_worker(
            _STS({
                "Account": "123456789012",
                "Arn": (
                    "arn:aws:sts::123456789012:assumed-role/"
                    "video-workflow-worker/worker-1"
                ),
            }),
            "arn:aws:sts::123456789012:assumed-role/video-workflow-worker/",
        )
        self.assertIsInstance(identity, WorkflowWorkerIdentity)
        self.assertEqual(identity.account_id, "123456789012")

    def test_rejects_wildcard_role_prefix_and_account_mismatch(self):
        with self.assertRaises(ValueError):
            authenticate_workflow_worker(
                _STS({
                    "Account": "123456789012",
                    "Arn": (
                        "arn:aws:sts::123456789012:assumed-role/"
                        "video-workflow-worker/worker-1"
                    ),
                }),
                "arn:aws:sts::123456789012:assumed-role/*/",
            )
        with self.assertRaises(PermissionError):
            authenticate_workflow_worker(
                _STS({
                    "Account": "999999999999",
                    "Arn": (
                        "arn:aws:sts::123456789012:assumed-role/"
                        "video-workflow-worker/worker-1"
                    ),
                }),
                "arn:aws:sts::123456789012:assumed-role/video-workflow-worker/",
            )

    def test_rejects_unapproved_role(self):
        with self.assertRaises(PermissionError):
            authenticate_workflow_worker(
                _STS({
                    "Account": "123456789012",
                    "Arn": (
                        "arn:aws:sts::123456789012:assumed-role/"
                        "developer/admin"
                    ),
                }),
                "arn:aws:sts::123456789012:assumed-role/video-workflow-worker/",
            )

    def test_fails_closed_when_sts_is_unavailable(self):
        with self.assertRaises(PermissionError):
            authenticate_workflow_worker(
                _STS(error=OSError("network unavailable")),
                "arn:aws:sts::123456789012:assumed-role/video-workflow-worker/",
            )

    def test_rejects_malformed_allowlist_and_forged_identity_scope(self):
        with self.assertRaises(ValueError):
            authenticate_workflow_worker(_STS(), "video-worker")
        with self.assertRaises(ValueError):
            WorkflowWorkerIdentity(
                "123456789012",
                "arn:aws:sts::123456789012:assumed-role/other/worker",
                "arn:aws:sts::123456789012:assumed-role/video-workflow-worker/",
            )


if __name__ == "__main__":
    unittest.main()
