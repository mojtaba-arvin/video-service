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

    def test_playlist_setting_defaults_to_hls(self):
        settings = load_settings({})

        self.assertTrue(settings.DEFAULT_PLAYLIST_IS_HLS)


if __name__ == "__main__":
    unittest.main()
