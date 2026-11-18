"""Tests for complete, provider-neutral VOD plan construction."""

import unittest

from video_streaming.workflows.execution_plan import execution_plan_sha256
from video_streaming.workflows.vod_plan import (
    VODPlanBuildError,
    build_vod_execution_plan,
)


def request_with_all_branches():
    return {
        "video": {"bucket": "source", "key": "incoming/source.mp4"},
        "watermark": {
            "input_bucket": "assets",
            "input_key": "branding/watermark.png",
            "output_bucket": "deliverables",
            "output_key": "watermarked/source.mp4",
            "dont_replace": True,
        },
        "playlists": [
            {
                "bucket": "deliverables",
                "key": "hls/master.m3u8",
                "protocol": "hls",
                "quality_names": ["360p", "720p"],
                "use_watermark": False,
                "dont_replace": True,
                "create_bucket": True,
                "fragmented": True,
            },
            {
                "bucket": "deliverables",
                "key": "dash/manifest.mpd",
                "protocol": "dash",
                "quality_names": ["720p"],
                "use_watermark": True,
            },
        ],
        "thumbnails": [
            {
                "bucket": "deliverables",
                "key": "thumbnails/poster.jpg",
                "thumbnail_time": 2.5,
                "scale_width": 640,
                "scale_height": 360,
                "use_watermark": True,
            }
        ],
    }


class VODPlanBuilderTests(unittest.TestCase):

    def test_builds_complete_graph_for_input_checks_fan_in_and_outputs(self):
        plan = build_vod_execution_plan(
            "job-123", request_with_all_branches(),
            webhook_secret_ref="secret-store://callbacks/job-123",
        )
        nodes = {node["node_id"]: node for node in plan["nodes"]}

        expected = {
            "input-video-check",
            "input-watermark-check",
            "input-video-download",
            "input-video-analyze",
            "input-watermark-download",
            "input-watermark-analyze",
            "output-bucket-0-check",
            "output-playlist-0-key-check",
            "output-watermarked-video-key-check",
            "inputs-funnel",
            "watermark-apply",
            "playlist-0-package",
            "playlist-0-upload",
            "playlist-0-webhook",
            "playlist-1-package",
            "playlist-1-upload",
            "playlist-1-webhook",
            "thumbnail-0-generate",
            "thumbnail-0-upload",
            "thumbnail-0-webhook",
            "watermarked-video-upload",
            "watermarked-video-webhook",
        }
        self.assertTrue(expected.issubset(set(nodes)))

        preflight = {
            "input-video-check",
            "input-watermark-check",
            "output-bucket-0-check",
            "output-playlist-0-key-check",
            "output-watermarked-video-key-check",
        }
        self.assertEqual(set(nodes["input-video-download"]["depends_on"]), preflight)
        self.assertEqual(set(nodes["input-watermark-download"]["depends_on"]), preflight)
        self.assertEqual(
            set(nodes["inputs-funnel"]["depends_on"]),
            {"input-video-analyze", "input-watermark-analyze"},
        )
        self.assertEqual(nodes["playlist-0-package"]["depends_on"], ["inputs-funnel"])
        self.assertEqual(nodes["playlist-1-package"]["depends_on"], ["watermark-apply"])
        self.assertEqual(nodes["thumbnail-0-generate"]["depends_on"], ["watermark-apply"])
        self.assertEqual(nodes["watermark-apply"]["depends_on"], ["inputs-funnel"])
        self.assertEqual(nodes["playlist-0-webhook"]["depends_on"], ["playlist-0-upload"])
        self.assertEqual(
            nodes["playlist-0-webhook"]["parameters"]["webhook_secret_ref"],
            "secret-store://callbacks/job-123",
        )

    def test_plan_is_deterministic_for_same_normalized_request(self):
        request = request_with_all_branches()
        first = build_vod_execution_plan("job-123", request)
        second = build_vod_execution_plan("job-123", request)
        self.assertEqual(execution_plan_sha256(first), execution_plan_sha256(second))

    def test_no_watermark_request_does_not_create_watermark_nodes(self):
        request = {
            "video": {"bucket": "source", "key": "input.mp4"},
            "playlists": [{
                "bucket": "output", "key": "master.m3u8",
                "protocol": "hls", "quality_names": [],
            }],
        }
        plan = build_vod_execution_plan("job-456", request)
        operations = [node["operation"] for node in plan["nodes"]]
        self.assertNotIn("video.watermark.apply", operations)
        self.assertIn("video.hls.package", operations)

    def test_watermark_output_requires_watermark_input(self):
        request = {
            "video": {"bucket": "source", "key": "input.mp4"},
            "watermark": {
                "output_bucket": "output", "output_key": "watermarked.mp4",
            },
            "playlists": [],
        }
        with self.assertRaisesRegex(VODPlanBuildError, "requires a watermark input"):
            build_vod_execution_plan("job-789", request)

    def test_watermarked_playlist_requires_watermark_input(self):
        request = {
            "video": {"bucket": "source", "key": "input.mp4"},
            "playlists": [{
                "bucket": "output", "key": "master.m3u8",
                "protocol": "hls", "use_watermark": True,
            }],
        }
        with self.assertRaisesRegex(VODPlanBuildError, "require a watermark input"):
            build_vod_execution_plan("job-789", request)

    def test_rejects_duplicate_outputs_and_invalid_protocol(self):
        request = request_with_all_branches()
        request["playlists"][1]["key"] = request["playlists"][0]["key"]
        with self.assertRaisesRegex(VODPlanBuildError, "duplicate output"):
            build_vod_execution_plan("job-123", request)

        request = request_with_all_branches()
        request["playlists"][0]["protocol"] = "smooth"
        with self.assertRaisesRegex(VODPlanBuildError, "must be hls or dash"):
            build_vod_execution_plan("job-123", request)

    def test_rejects_non_finite_thumbnail_time_and_non_boolean_flags(self):
        request = {
            "video": {"bucket": "source", "key": "input.mp4"},
            "thumbnails": [{
                "bucket": "output", "key": "poster.jpg",
                "thumbnail_time": float("nan"),
            }],
        }
        with self.assertRaisesRegex(VODPlanBuildError, "must be non-negative"):
            build_vod_execution_plan("job-123", request)

        request["thumbnails"][0]["thumbnail_time"] = 1
        request["thumbnails"][0]["use_watermark"] = "false"
        with self.assertRaisesRegex(VODPlanBuildError, "must be a boolean"):
            build_vod_execution_plan("job-123", request)

    def test_rejects_raw_webhook_url_instead_of_secret_reference(self):
        request = {
            "video": {"bucket": "source", "key": "input.mp4"},
            "playlists": [{
                "bucket": "output", "key": "master.m3u8", "protocol": "hls",
            }],
        }
        with self.assertRaisesRegex(VODPlanBuildError, "opaque secret reference"):
            build_vod_execution_plan(
                "job-123", request, webhook_secret_ref="https://example.test/hook"
            )

    def test_watermarked_video_output_alone_still_runs_watermark_operation(self):
        request = {
            "video": {"bucket": "source", "key": "input.mp4"},
            "watermark": {
                "input_bucket": "assets", "input_key": "logo.png",
                "output_bucket": "output", "output_key": "watermarked.mp4",
            },
            "playlists": [],
            "thumbnails": [],
        }
        plan = build_vod_execution_plan("job-watermark", request)
        nodes = {node["node_id"]: node for node in plan["nodes"]}
        self.assertEqual(nodes["watermark-apply"]["depends_on"], ["inputs-funnel"])
        self.assertEqual(nodes["watermarked-video-upload"]["depends_on"], ["watermark-apply"])

    def test_watermark_output_key_check_is_in_preflight(self):
        request = request_with_all_branches()
        plan = build_vod_execution_plan("job-123", request)
        nodes = {node["node_id"]: node for node in plan["nodes"]}
        self.assertIn(
            "output-watermarked-video-key-check",
            nodes["input-video-download"]["depends_on"],
        )

if __name__ == "__main__":
    unittest.main()
