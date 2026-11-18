"""Adversarial tests for manifest-last package publication and retries."""

import json
import tempfile
import unittest
from pathlib import Path

from video_streaming.ffmpeg.package_publication import (
    PackagePublicationError,
    publish_package,
)


class FakeObjectStorage:
    def __init__(self):
        self.objects = {}
        self.upload_order = []
        self.fail_upload_key = None
        self.partial_fail_upload_key = None
        self.unreadable_key = None
        self.events = []

    def read_object_bytes(self, key, bucket_name=None):
        value = self.objects.get(key)
        return value

    def put_object_bytes(self, key, content, bucket_name=None,
                         content_type="application/json"):
        self.objects[key] = content
        self.events.append(("put", key))
        return {"Key": key}

    def delete_object(self, key, bucket_name=None, **kwargs):
        self.events.append(("delete", key))
        self.objects.pop(key, None)
        return {"Deleted": key}

    def upload_file_by_path(self, key, file_path, bucket_name=None,
                            callback=None, **kwargs):
        self.events.append(("upload", key))
        if key == self.partial_fail_upload_key:
            self.objects[key] = b"partial-upload"
            raise IOError("simulated partial object upload failure")
        if key == self.fail_upload_key:
            raise IOError("simulated object upload failure")
        value = Path(file_path).read_bytes()
        self.objects[key] = value
        self.upload_order.append(key)
        if callback:
            callback(len(value))

    def head(self, key, bucket_name=None, **kwargs):
        self.events.append(("head", key))
        if key not in self.objects:
            return None
        if key == self.unreadable_key:
            return None
        return {"ContentLength": len(self.objects[key])}

    def read_range(self, key, bucket_name=None, start=0, end=0):
        self.events.append(("read", key))
        if key == self.unreadable_key:
            return None
        value = self.objects.get(key)
        return None if value is None else value[start:end + 1]


class PackagePublicationTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.variant = self.root / "variant.m3u8"
        self.master = self.root / "master.m3u8"
        self.segment_a = self.root / "segment_000.ts"
        self.segment_b = self.root / "segment_001.ts"
        self.segment_a.write_bytes(b"segment-a")
        self.segment_b.write_bytes(b"segment-b")
        self.variant.write_text(
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-TARGETDURATION:1\n"
            "#EXTINF:1.0,\n"
            "segment_000.ts\n"
            "#EXTINF:1.0,\n"
            "segment_001.ts\n"
            "#EXT-X-ENDLIST\n",
            encoding="utf-8",
        )
        self.master.write_text(
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=100000,RESOLUTION=320x180\n"
            "variant.m3u8\n",
            encoding="utf-8",
        )
        self.storage = FakeObjectStorage()
        self.bucket = "vod-output"
        self.root_key = "vod/master.m3u8"

    def tearDown(self):
        self.temp_dir.cleanup()

    def publish(self):
        return publish_package(
            directory=str(self.root),
            manifest_path=str(self.master),
            protocol="hls",
            expected_renditions=["variant.m3u8"],
            s3_output_key=self.root_key,
            s3_output_bucket=self.bucket,
            storage=self.storage,
        )

    def test_every_dependency_is_uploaded_and_readable_before_master(self):
        result = self.publish()
        self.assertEqual(result["files"], 4)
        self.assertEqual(self.storage.upload_order[-1], self.root_key)
        self.assertEqual(
            set(self.storage.objects),
            {
                "vod/segment_000.ts",
                "vod/segment_001.ts",
                "vod/variant.m3u8",
                self.root_key,
                "vod/.master.m3u8.artifacts.json",
            },
        )
        self.assertEqual(self.storage.objects[self.root_key],
                         self.master.read_bytes())

        master_upload = self.storage.events.index(("upload", self.root_key))
        for key in (
                "vod/segment_000.ts",
                "vod/segment_001.ts",
                "vod/variant.m3u8"):
            self.assertLess(
                self.storage.events.index(("read", key)),
                master_upload,
            )

    def test_segment_upload_failure_withdraws_old_master(self):
        self.storage.objects[self.root_key] = b"stale-master"
        self.storage.fail_upload_key = "vod/segment_000.ts"
        with self.assertRaises(IOError):
            self.publish()
        self.assertNotIn(self.root_key, self.storage.objects)
        self.assertNotIn("vod/variant.m3u8", self.storage.objects)

    def test_root_manifest_failure_removes_child_playlist(self):
        self.storage.fail_upload_key = "vod/master.m3u8"
        self.storage.objects["vod/master.m3u8"] = b"old"
        self.storage.objects["vod/variant.m3u8"] = b"old-variant"
        with self.assertRaises(IOError):
            self.publish()
        self.assertNotIn("vod/master.m3u8", self.storage.objects)
        self.assertNotIn("vod/variant.m3u8", self.storage.objects)

    def test_partial_segment_upload_is_removed_after_failure(self):
        self.storage.partial_fail_upload_key = "vod/segment_000.ts"
        with self.assertRaises(IOError):
            self.publish()
        self.assertNotIn("vod/segment_000.ts", self.storage.objects)
        self.assertNotIn("vod/segment_001.ts", self.storage.objects)
        self.assertNotIn("vod/variant.m3u8", self.storage.objects)
        self.assertNotIn(self.root_key, self.storage.objects)

    def test_unreadable_segment_prevents_variant_and_master_publication(self):
        self.storage.unreadable_key = "vod/segment_000.ts"
        with self.assertRaises(PackagePublicationError):
            self.publish()
        self.assertNotIn("vod/variant.m3u8", self.storage.objects)
        self.assertNotIn(self.root_key, self.storage.objects)

    def test_retry_overwrites_deterministic_keys_without_duplicate_segments(self):
        first = self.publish()
        second = self.publish()
        self.assertEqual(first["files"], second["files"])
        self.assertEqual(len(self.storage.objects), 5)
        self.assertEqual(
            len([key for key in self.storage.upload_order
                 if key.endswith(".ts")]),
            4,
        )
        self.assertEqual(self.storage.upload_order[-1], self.root_key)
        self.assertEqual(self.storage.objects["vod/segment_000.ts"], b"segment-a")

    def test_invalid_local_retry_preserves_last_known_good_publication(self):
        self.storage.objects[self.root_key] = b"last-known-good"
        self.master.write_text(
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=100000\n"
            "missing-variant.m3u8\n",
            encoding="utf-8",
        )
        with self.assertRaises(Exception):
            self.publish()
        self.assertEqual(self.storage.objects[self.root_key], b"last-known-good")

    def test_retry_cleans_only_objects_owned_by_its_previous_inventory(self):
        first = self.publish()
        inventory_key = first["inventory_key"]
        payload = json.loads(self.storage.objects[inventory_key].decode("utf-8"))
        payload["artifacts"].append("orphan-from-failed-attempt.ts")
        self.storage.objects[inventory_key] = json.dumps(payload).encode("utf-8")
        self.storage.objects["vod/orphan-from-failed-attempt.ts"] = b"orphan"

        self.publish()

        self.assertNotIn(
            "vod/orphan-from-failed-attempt.ts", self.storage.objects
        )
        self.assertEqual(
            self.storage.objects["vod/segment_000.ts"], b"segment-a"
        )
        self.assertIn(inventory_key, self.storage.objects)


if __name__ == "__main__":
    unittest.main()
