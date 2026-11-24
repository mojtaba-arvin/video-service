import importlib.util
import unittest
from unittest.mock import patch

if importlib.util.find_spec("celery"):
    from video_streaming import settings
    from video_streaming.workflows import tasks
else:
    settings = None
    tasks = None


@unittest.skipUnless(
    importlib.util.find_spec("celery"),
    "Celery is installed only in the integration runtime",
)
class WorkflowTaskConsumerTests(unittest.TestCase):
    def test_dispatch_is_disabled_by_default_and_fails_closed(self):
        with patch.object(
            settings, "WORKFLOW_DURABLE_DISPATCH_ENABLED", False
        ):
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                tasks.dispatch_plan.run(
                    "job", "workflow", "1", "a" * 64
                )

    def test_outbox_relay_is_disabled_with_the_durable_feature_gate(self):
        with patch.object(
            settings, "WORKFLOW_DURABLE_DISPATCH_ENABLED", False
        ):
            self.assertEqual(
                tasks.relay_workflow_outbox.run(),
                {"status": "disabled"},
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

    def test_dispatch_fans_out_only_repository_ready_nodes(self):
        class Repository:
            def get_execution_plan(self, job_id):
                return {
                    "workflow_id": "workflow",
                    "workflow_version": "1",
                    "fingerprint": "a" * 64,
                    "plan": {"nodes": [
                        {"node_id": "root"},
                        {"node_id": "blocked-child"},
                    ]},
                }

            def get_ready_node_ids(self, job_id):
                return ["root"]

        with patch.object(
                settings, "WORKFLOW_DURABLE_DISPATCH_ENABLED", True):
            with patch.object(
                    tasks, "get_workflow_repository",
                    return_value=Repository()):
                with patch.object(tasks, "_build_executor"):
                    with patch.object(
                            tasks.execute_node, "apply_async") as publish:
                        result = tasks.dispatch_plan.run(
                            "job", "workflow", "1", "a" * 64
                        )

        self.assertEqual(result, {"status": "dispatched", "nodes": 1})
        publish.assert_called_once_with(
            args=("job", "root", "workflow", "1", "a" * 64)
        )

    def test_expired_attempt_reconciler_is_disabled_by_default(self):
        with patch.object(
            settings, "WORKFLOW_DURABLE_DISPATCH_ENABLED", False
        ):
            with patch.object(tasks, "get_workflow_repository") as repository:
                self.assertEqual(
                    tasks.reconcile_expired_workflow_attempts.run(),
                    {"status": "disabled", "jobs": 0, "dispatched": 0},
                )
                repository.assert_not_called()

    def test_expired_attempt_reconciler_redispatches_only_jobs_with_ready_nodes(self):
        class Repository:
            def list_jobs_with_expired_attempts(self, limit):
                self.asserted_limit = limit
                return ["expired-job", "terminal-job"]

            def get_execution_plan(self, job_id):
                if job_id == "terminal-job":
                    return None
                return {
                    "workflow_id": "workflow",
                    "workflow_version": "1",
                    "fingerprint": "a" * 64,
                    "plan": {"nodes": []},
                }

            def get_ready_node_ids(self, job_id):
                return ["root"] if job_id == "expired-job" else []

        repository = Repository()
        with patch.object(
            settings, "WORKFLOW_DURABLE_DISPATCH_ENABLED", True
        ):
            with patch.object(
                tasks, "get_workflow_repository", return_value=repository
            ):
                with patch.object(tasks.dispatch_plan, "apply_async") as publish:
                    result = tasks.reconcile_expired_workflow_attempts.run()

        self.assertEqual(result, {
            "status": "reconciled", "jobs": 2, "dispatched": 1,
        })
        self.assertEqual(repository.asserted_limit, 100)
        publish.assert_called_once_with(
            args=("expired-job", "workflow", "1", "a" * 64),
            expires=45,
        )

    def test_handler_factory_is_mandatory_and_explicit(self):
        with patch.object(settings, "WORKFLOW_HANDLER_FACTORY", ""):
            with self.assertRaisesRegex(RuntimeError, "module:callable"):
                tasks._load_handler_factory()


if __name__ == "__main__":
    unittest.main()
