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


@unittest.skipUnless(
    _HAS_LOGGING_RUNTIME,
    "Celery, Boto3 and runtime dependencies are installed in the VOD integration job",
)
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


class SensitiveLoggingTests(unittest.TestCase):

    def test_celery_task_arguments_are_not_logged(self):
        secret = "signed-storage-secret"
        with self.assertLogs(
                "video_streaming.core.tasks.base", level="INFO") as captured:
            result = _DummyTask()(
                "input.mp4",
                webhook_url="https://callback.invalid/hook?token={}".format(
                    secret
                ),
            )

        self.assertEqual(result, "ok")
        rendered = "\n".join(captured.output)
        self.assertIn("tests.sensitive_logging", rendered)
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
        rendered = "\n".join(captured.output)
        self.assertIn("204", rendered)
        self.assertIn("job-123", rendered)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(url, rendered)

    def test_s3_exception_log_omits_provider_exception_message(self):
        secret = "provider-secret-value"
        service = S3Service.__new__(S3Service)

        with self.assertLogs(
                "video_streaming.core.services.s3", level="ERROR") as captured:
            with self.assertRaisesRegex(ValueError, secret):
                service._exception_handler(
                    ValueError("request contained {}".format(secret))
                )

        rendered = "\n".join(captured.output)
        self.assertIn("ValueError", rendered)
        self.assertNotIn(secret, rendered)


if __name__ == "__main__":
    unittest.main()
