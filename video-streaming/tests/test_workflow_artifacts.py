"""Tests for immutable, attempt-scoped storage object identities."""

import unittest

from video_streaming.workflows.artifacts import (
    attempt_scoped_local_output_path,
    attempt_scoped_object_key,
)


class AttemptScopedObjectKeyTests(unittest.TestCase):

    def test_local_output_path_is_isolated_by_processing_attempt(self):
        first_path, first_directory = attempt_scoped_local_output_path(
            "/tmp/vod/job-1/master.m3u8", "processing-a"
        )
        second_path, second_directory = attempt_scoped_local_output_path(
            "/tmp/vod/job-1/master.m3u8", "processing-b"
        )

        self.assertNotEqual(first_path, second_path)
        self.assertNotEqual(first_directory, second_directory)
        self.assertTrue(first_path.endswith(
            "/.attempts/processing-a/master.m3u8"
        ))
        self.assertTrue(second_path.endswith(
            "/.attempts/processing-b/master.m3u8"
        ))

    def test_local_output_path_rejects_invalid_attempt_ids_and_names(self):
        for base_path, attempt_id in (
                ("", "attempt-1"),
                ("/tmp/vod/", "attempt-1"),
                ("/tmp/vod/master.m3u8", "../escape")):
            with self.subTest(base_path=base_path, attempt_id=attempt_id):
                with self.assertRaises(ValueError):
                    attempt_scoped_local_output_path(base_path, attempt_id)

    def test_namespaced_key_preserves_logical_prefix_and_filename(self):
        self.assertEqual(
            attempt_scoped_object_key(
                "tenant-a/jobs/job-1/output/master.m3u8",
                "attempt-0001",
            ),
            "tenant-a/jobs/job-1/output/.attempts/attempt-0001/master.m3u8",
        )

    def test_root_level_key_uses_attempt_namespace(self):
        self.assertEqual(
            attempt_scoped_object_key("thumbnail.jpg", "attempt_2"),
            ".attempts/attempt_2/thumbnail.jpg",
        )

    def test_different_attempts_never_share_an_object_key(self):
        first = attempt_scoped_object_key("media/output.mp4", "attempt-a")
        second = attempt_scoped_object_key("media/output.mp4", "attempt-b")
        self.assertNotEqual(first, second)

    def test_rejects_absolute_traversal_and_ambiguous_keys(self):
        for key in (
                "", "/absolute/file.mp4", "../file.mp4",
                "tenant/../file.mp4", "tenant//file.mp4",
                "tenant/./file.mp4", "tenant\\file.mp4",
                "tenant/file.mp4\x00"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    attempt_scoped_object_key(key, "attempt-1")

    def test_rejects_invalid_attempt_identifiers(self):
        for attempt_id in ("", "../other", "a/b", "x" * 129, None):
            with self.subTest(attempt_id=attempt_id):
                with self.assertRaises(ValueError):
                    attempt_scoped_object_key("file.mp4", attempt_id)


if __name__ == "__main__":
    unittest.main()
