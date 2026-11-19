"""Regression tests ensuring sensitive task and callback values stay out of logs."""

import importlib.util
import logging
import unittest
from unittest.mock import Mock, patch


_HAS_LOGGING_RUNTIME = all(
    importlib.util.find_spec(module_name) is not None
    for module_name in ("celery", "boto3", "botocore", "urllib3")
)

if _HAS_LOGGING_RUNTIME:
    from video_streaming.core.tasks.base import BaseTask
    from video_streaming.core.services.s3 import S3Service
    from video_streaming.ffmpeg.tasks.mixins.call_webhook import CallWebhookMixin

    class _DummyTask(BaseTask):
        name = "tests.sensitive_logging"

        def run(self, *args, **kwargs):
            return "ok"

    class _Cache:
        def get(self, key):
            return {"reference_id": "test-reference"}

    class _WebhookTask(CallWebhookMixin):
        cache = _Cache()
        logger = logging.getLogger("tests.webhook_logging")


@unittest.skipUnless(
    _HAS_LOGGING_RUNTIME,
    "Celery, Boto3 and runtime dependencies are installed in the VOD integration job",
)
class SensitiveLoggingTests(unittest.TestCase):

    def test_celery_task_arguments_are_not_logged(self):
        secret = "signed-storage-secret"
        with self.assertLogs(
                "video_streaming.core.tasks.base", level="INFO") as captured:
            # Test our logging wrapper without depending on a configured
            # Celery request stack or an installed message broker.
            with patch("celery.app.task.Task.__call__", return_value="ok"):
                result = _DummyTask()(
                    "input.mp4",
                    webhook_url="https://callback.invalid/hook?token={}".format(
                        secret
                    ),
                )

        self.assertEqual(result, "ok")
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(
            captured.records[0].task_name,
            "tests.sensitive_logging",
        )
        rendered = "\\n".join(captured.output)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("webhook_url", rendered)

    def test_webhook_query_credentials_are_not_logged(self):
        secret = "callback-token-secret"
        url = "https://callback.invalid/hook?token={}".format(secret)
        pool = Mock()
        pool.request.return_value.status = 204

        with patch(
                "video_streaming.ffmpeg.tasks.mixins.call_webhook.urllib3.PoolManager",
                return_value=pool):
            with self.assertLogs("tests.webhook_logging", level="INFO") as captured:
                result = _WebhookTask().call_webhook("job-123", webhook_url=url)

        self.assertTrue(result)
        rendered = "\\n".join(captured.output)
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].http_status, 204)
        self.assertEqual(captured.records[0].request_id, "job-123")
        self.assertNotIn(secret, rendered)
        self.assertNotIn(url, rendered)

    def test_webhook_transport_is_bounded_and_does_not_follow_redirects(self):
        url = "https://callback.invalid/hook"
        pool = Mock()
        response = Mock()
        response.status = 302
        pool.request.return_value = response

        with patch(
                "video_streaming.ffmpeg.tasks.mixins.call_webhook.urllib3.PoolManager",
                return_value=pool):
            with patch(
                    "video_streaming.ffmpeg.tasks.mixins.call_webhook."
                    "settings.WEBHOOK_CONNECT_TIMEOUT_SECONDS",
                    2.0):
                with patch(
                        "video_streaming.ffmpeg.tasks.mixins.call_webhook."
                        "settings.WEBHOOK_READ_TIMEOUT_SECONDS",
                        4.0):
                    with patch.object(
                            _WebhookTask,
                            "check_http_status",
                            side_effect=ValueError("redirect rejected")):
                        with self.assertRaisesRegex(
                                ValueError, "redirect rejected"):
                            _WebhookTask().call_webhook(
                                "job-456", webhook_url=url
                            )

        kwargs = pool.request.call_args.kwargs
        self.assertFalse(kwargs["redirect"])
        self.assertFalse(kwargs["retries"])
        self.assertFalse(kwargs["preload_content"])
        self.assertEqual(kwargs["timeout"]._connect, 2.0)
        self.assertEqual(kwargs["timeout"]._read, 4.0)
        response.close.assert_called_once_with()
        pool.clear.assert_called_once_with()

    def test_s3_exception_log_omits_provider_exception_message(self):
        secret = "provider-secret-value"
        service = S3Service.__new__(S3Service)

        with self.assertLogs(
                "video_streaming.core.services.s3", level="ERROR") as captured:
            with self.assertRaisesRegex(ValueError, secret):
                service._exception_handler(
                    ValueError("request contained {}".format(secret))
                )

        rendered = "\\n".join(captured.output)
        self.assertIn("ValueError", rendered)
        self.assertNotIn(secret, rendered)


if __name__ == "__main__":
    unittest.main()
