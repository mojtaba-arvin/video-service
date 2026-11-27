"""Regression tests for environment-backed application settings."""

import importlib
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def load_settings(env_values):
    """Import settings against an isolated temporary environment file."""
    env_config = importlib.import_module("video_streaming.env_config")
    settings_name = "video_streaming.settings"
    previous_env_path = env_config.ENV_FILE_PATH

    with tempfile.TemporaryDirectory() as temp_dir:
        env_path = Path(temp_dir) / ".env"
        env_path.write_text(
            "\n".join("{}={}".format(key, value)
                      for key, value in env_values.items()),
            encoding="utf-8",
        )
        env_config.ENV_FILE_PATH = str(env_path)
        sys.modules.pop(settings_name, None)
        try:
            return importlib.import_module(settings_name)
        finally:
            env_config.ENV_FILE_PATH = previous_env_path
            sys.modules.pop(settings_name, None)


class SettingsEnvironmentTests(unittest.TestCase):

    def test_ffmpeg_retry_limit_has_its_own_environment_variable(self):
        settings = load_settings({
            "TASK_RETRY_BACKOFF_MAX": "7",
            "TASK_RETRY_FFMPEG_COMMAND_MAX": "3",
        })

        self.assertEqual(settings.TASK_RETRY_BACKOFF_MAX, 7)
        self.assertEqual(settings.TASK_RETRY_FFMPEG_COMMAND_MAX, 3)

    def test_input_and_output_buckets_can_be_configured_independently(self):
        settings = load_settings({
            "S3_DEFAULT_BUCKET": "fallback",
            "S3_DEFAULT_INPUT_BUCKET_NAME": "input-media",
            "S3_DEFAULT_OUTPUT_BUCKET_NAME": "processed-media",
        })

        self.assertEqual(settings.S3_DEFAULT_INPUT_BUCKET_NAME, "input-media")
        self.assertEqual(settings.S3_DEFAULT_OUTPUT_BUCKET_NAME, "processed-media")

    def test_canonical_playlist_setting_takes_precedence(self):
        settings = load_settings({
            "DEFAULT_PLAYLIST_IS_HLS": "False",
            "DEBUG_PLAYLIST_IS_HLS": "True",
        })

        self.assertFalse(settings.DEFAULT_PLAYLIST_IS_HLS)

    def test_legacy_playlist_setting_remains_supported(self):
        settings = load_settings({"DEBUG_PLAYLIST_IS_HLS": "False"})

        self.assertFalse(settings.DEFAULT_PLAYLIST_IS_HLS)

    def test_package_metadata_probe_timeout_is_configurable(self):
        settings = load_settings({
            "PACKAGE_METADATA_PROBE_TIMEOUT_SECONDS": "4",
        })

        self.assertEqual(
            settings.PACKAGE_METADATA_PROBE_TIMEOUT_SECONDS, 4
        )

    def test_package_metadata_probe_timeout_has_safe_default(self):
        settings = load_settings({})

        self.assertEqual(
            settings.PACKAGE_METADATA_PROBE_TIMEOUT_SECONDS, 10
        )

    def test_thumbnail_execution_timeout_is_configurable(self):
        settings = load_settings({
            "FFMPEG_THUMBNAIL_TIMEOUT_SECONDS": "45",
        })

        self.assertEqual(settings.FFMPEG_THUMBNAIL_TIMEOUT_SECONDS, 45)

    def test_thumbnail_execution_timeout_has_safe_default(self):
        settings = load_settings({})

        self.assertEqual(settings.FFMPEG_THUMBNAIL_TIMEOUT_SECONDS, 300)

    def test_watermark_execution_timeout_is_configurable(self):
        settings = load_settings({
            "FFMPEG_WATERMARK_TIMEOUT_SECONDS": "5400",
        })

        self.assertEqual(settings.FFMPEG_WATERMARK_TIMEOUT_SECONDS, 5400)

    def test_watermark_execution_timeout_has_safe_default(self):
        settings = load_settings({})

        self.assertEqual(settings.FFMPEG_WATERMARK_TIMEOUT_SECONDS, 3600)

    def test_playlist_execution_timeout_is_configurable(self):
        settings = load_settings({
            "FFMPEG_PLAYLIST_TIMEOUT_SECONDS": "5400",
        })
        self.assertEqual(settings.FFMPEG_PLAYLIST_TIMEOUT_SECONDS, 5400)

    def test_playlist_execution_timeout_has_safe_default(self):
        settings = load_settings({})
        self.assertEqual(settings.FFMPEG_PLAYLIST_TIMEOUT_SECONDS, 7200)

    def test_playlist_execution_timeout_must_be_bounded(self):
        with self.assertRaisesRegex(
                ValueError, "FFMPEG_PLAYLIST_TIMEOUT_SECONDS"):
            load_settings({"FFMPEG_PLAYLIST_TIMEOUT_SECONDS": "0"})

    def test_webhook_timeout_defaults_are_bounded(self):
        settings = load_settings({})

        self.assertEqual(settings.WEBHOOK_CONNECT_TIMEOUT_SECONDS, 3.0)
        self.assertEqual(settings.WEBHOOK_READ_TIMEOUT_SECONDS, 10.0)

    def test_webhook_timeouts_are_configurable(self):
        settings = load_settings({
            "WEBHOOK_CONNECT_TIMEOUT_SECONDS": "1.5",
            "WEBHOOK_READ_TIMEOUT_SECONDS": "7",
        })

        self.assertEqual(settings.WEBHOOK_CONNECT_TIMEOUT_SECONDS, 1.5)
        self.assertEqual(settings.WEBHOOK_READ_TIMEOUT_SECONDS, 7.0)

    def test_webhook_timeouts_must_be_positive(self):
        with self.assertRaisesRegex(
                ValueError, "WEBHOOK_CONNECT_TIMEOUT_SECONDS"):
            load_settings({"WEBHOOK_CONNECT_TIMEOUT_SECONDS": "0"})

    def test_custom_price_card_requires_an_explicit_version(self):
        from video_streaming.workflows.metrics import configured_pricing

        settings = load_settings({
            "WORKFLOW_PRICING_MODEL_ID": "infra-us-east",
            "WORKFLOW_CPU_COST_PER_SECOND": "0.001",
        })
        with self.assertRaisesRegex(ValueError, "MODEL_VERSION"):
            configured_pricing(settings)

    def test_custom_price_card_is_versioned_and_preserves_rates(self):
        from video_streaming.workflows.metrics import configured_pricing

        settings = load_settings({
            "WORKFLOW_PRICING_MODEL_ID": "infra-us-east",
            "WORKFLOW_PRICING_MODEL_VERSION": "2026-11-01",
            "WORKFLOW_COST_CURRENCY": "USD",
            "WORKFLOW_CPU_COST_PER_SECOND": "0.001",
            "WORKFLOW_MEMORY_GB_SECOND_COST": "0.0002",
            "WORKFLOW_STORAGE_GB_COST": "0.03",
        })
        pricing = configured_pricing(settings)

        self.assertEqual(pricing["model_id"], "infra-us-east")
        self.assertEqual(pricing["model_version"], "2026-11-01")
        self.assertEqual(pricing["currency"], "USD")
        self.assertEqual(pricing["cpu_per_second"], 0.001)
        self.assertEqual(pricing["memory_gb_second"], 0.0002)
        self.assertEqual(pricing["storage_gb"], 0.03)

    def test_custom_price_card_rejects_negative_rates(self):
        from video_streaming.workflows.metrics import configured_pricing

        settings = load_settings({
            "WORKFLOW_PRICING_MODEL_ID": "infra-us-east",
            "WORKFLOW_PRICING_MODEL_VERSION": "2026-11-01",
            "WORKFLOW_CPU_COST_PER_SECOND": "-0.1",
        })
        with self.assertRaisesRegex(ValueError, "finite and non-negative"):
            configured_pricing(settings)

    def test_unconfigured_price_card_uses_versioned_non_monetary_runit(self):
        from video_streaming.workflows.metrics import configured_pricing

        pricing = configured_pricing(load_settings({}))

        self.assertEqual(pricing["model_id"], "relative-resource-index-v1")
        self.assertEqual(pricing["model_version"], "1")
        self.assertEqual(pricing["currency"], "RUNIT")

    def test_workflow_tasks_are_discovered_but_dispatch_is_disabled_by_default(self):
        settings = load_settings({})
        self.assertIn("video_streaming.workflows", settings.AUTO_DISCOVER_TASKS)
        self.assertFalse(settings.WORKFLOW_DURABLE_DISPATCH_ENABLED)
        self.assertEqual(
            settings.WORKFLOW_HANDLER_FACTORY,
            "video_streaming.workflows.handler_factory:build_workflow_handlers",
        )

    def test_outbox_relay_is_not_scheduled_when_durable_dispatch_is_disabled(self):
        settings = load_settings({})
        self.assertNotIn(
            "relay-durable-workflow-outbox", settings.CELERY_BEAT_SCHEDULE
        )

    def test_outbox_relay_is_scheduled_only_when_durable_dispatch_is_enabled(self):
        settings = load_settings({
            "WORKFLOW_DURABLE_DISPATCH_ENABLED": "True",
        })
        relay = settings.CELERY_BEAT_SCHEDULE[
            "relay-durable-workflow-outbox"
        ]
        self.assertEqual(
            relay["task"], "video_streaming.workflows.relay_outbox"
        )
        self.assertEqual(relay["schedule"], 5.0)
        self.assertEqual(relay["options"]["expires"], 4)
        recovery = settings.CELERY_BEAT_SCHEDULE[
            "reconcile-expired-durable-workflows"
        ]
        self.assertEqual(
            recovery["task"],
            "video_streaming.workflows.reconcile_expired_attempts",
        )
        self.assertEqual(recovery["schedule"], 15.0)
        self.assertEqual(recovery["options"]["expires"], 14)

    def test_workflow_dispatch_can_be_enabled_only_by_explicit_configuration(self):
        settings = load_settings({
            "WORKFLOW_DURABLE_DISPATCH_ENABLED": "True",
            "WORKFLOW_HANDLER_FACTORY": "my_handlers:build_handlers",
        })
        self.assertTrue(settings.WORKFLOW_DURABLE_DISPATCH_ENABLED)
        self.assertEqual(
            settings.WORKFLOW_HANDLER_FACTORY, "my_handlers:build_handlers"
        )

    def test_playlist_setting_defaults_to_hls(self):
        settings = load_settings({})

        self.assertTrue(settings.DEFAULT_PLAYLIST_IS_HLS)


    def test_package_orphan_janitor_is_scheduled_hourly(self):
        settings = load_settings({})

        schedule = settings.CELERY_BEAT_SCHEDULE[
            "cleanup-abandoned-package-attempts"
        ]
        self.assertEqual(schedule["task"], "cleanup_abandoned_packages")
        self.assertEqual(schedule["schedule"], 3600.0)

    def test_package_orphan_retention_has_a_safe_default(self):
        settings = load_settings({})

        self.assertEqual(
            settings.WORKFLOW_PACKAGE_ORPHAN_RETENTION_SECONDS, 86400
        )

    def test_package_orphan_retention_is_configurable(self):
        settings = load_settings({
            "WORKFLOW_PACKAGE_ORPHAN_RETENTION_SECONDS": "172800",
        })

        self.assertEqual(
            settings.WORKFLOW_PACKAGE_ORPHAN_RETENTION_SECONDS, 172800
        )

    def test_package_orphan_retention_rejects_unsafe_values(self):
        for value in ("0", "3599", "2592001"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                        ValueError, "WORKFLOW_PACKAGE_ORPHAN_RETENTION_SECONDS"):
                    load_settings({
                        "WORKFLOW_PACKAGE_ORPHAN_RETENTION_SECONDS": value,
                    })

    def test_cdn_telemetry_retention_has_a_bounded_default(self):
        settings = load_settings({})
        self.assertEqual(settings.CDN_TELEMETRY_RETENTION_DAYS, 30)
        schedule = settings.CELERY_BEAT_SCHEDULE[
            "purge-expired-cdn-delivery-telemetry"
        ]
        self.assertEqual(
            schedule["task"],
            "video_streaming.workflows.purge_delivery_telemetry",
        )
        self.assertEqual(schedule["schedule"], 3600.0)
        self.assertEqual(schedule["options"]["expires"], 1800)

    def test_cdn_telemetry_retention_is_configurable_within_policy_bounds(self):
        settings = load_settings({"CDN_TELEMETRY_RETENTION_DAYS": "14"})
        self.assertEqual(settings.CDN_TELEMETRY_RETENTION_DAYS, 14)

    def test_cdn_telemetry_retention_rejects_unbounded_values(self):
        for value in ("0", "91"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                        ValueError, "CDN_TELEMETRY_RETENTION_DAYS"):
                    load_settings({"CDN_TELEMETRY_RETENTION_DAYS": value})

    def test_attempt_lease_ttl_has_a_safe_default(self):
        settings = load_settings({})

        self.assertEqual(settings.WORKFLOW_ATTEMPT_LEASE_TTL_SECONDS, 60)

    def test_attempt_lease_ttl_is_configurable(self):
        settings = load_settings({
            "WORKFLOW_ATTEMPT_LEASE_TTL_SECONDS": "120",
        })

        self.assertEqual(settings.WORKFLOW_ATTEMPT_LEASE_TTL_SECONDS, 120)

    def test_attempt_lease_ttl_rejects_out_of_range_values(self):
        for value in ("0", "-1", "86401"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                        ValueError, "WORKFLOW_ATTEMPT_LEASE_TTL_SECONDS"):
                    load_settings({
                        "WORKFLOW_ATTEMPT_LEASE_TTL_SECONDS": value,
                    })



if __name__ == "__main__":
    unittest.main()
