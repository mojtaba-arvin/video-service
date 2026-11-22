"""Tests for the gRPC-to-canonical VOD request adapter."""

import unittest

from video_streaming.workflows.vod_plan import build_vod_execution_plan

try:
    from video_streaming.grpc.protos import streaming_pb2
    from video_streaming.grpc.servicers.mixins.create_job import CreateJobMixin
except ImportError:
    # General regression lanes may omit generated gRPC modules. The dedicated
    # integration lane sets VIDEO_STREAMING_REQUIRE_GRPC_TESTS=1 so a missing
    # generated module or a broken adapter import is a hard failure, never a
    # silently skipped acceptance suite.
    import os

    if os.environ.get("VIDEO_STREAMING_REQUIRE_GRPC_TESTS") == "1":
        raise
    streaming_pb2 = None
    CreateJobMixin = None


@unittest.skipIf(
    streaming_pb2 is None or CreateJobMixin is None,
    "gRPC modules are generated in the dedicated integration job",
)
class DurableVODRequestAdapterTests(unittest.TestCase):

    def setUp(self):
        self.adapter = CreateJobMixin()
        self.adapter.pb2 = streaming_pb2

    def make_request(self):
        request = streaming_pb2.JobRequest()
        request.video.s3_input.bucket = "source"
        request.video.s3_input.key = "incoming/source.mp4"
        request.watermark.s3_input.bucket = "assets"
        request.watermark.s3_input.key = "branding/logo.png"

        playlist = request.playlists.add()
        playlist.protocol = streaming_pb2.Protocol.HLS
        playlist.upload_to.bucket = "deliverables"
        playlist.upload_to.key = "hls/master.m3u8"
        playlist.upload_to.create_bucket = True
        playlist.upload_to.dont_replace = True
        playlist.use_watermark = True
        playlist.options.fmp4 = True
        playlist.options.quality_names.extend([
            streaming_pb2.QualityName.R_360P,
            streaming_pb2.QualityName.R_720P,
        ])
        playlist.options.h264.video_codec = streaming_pb2.H264.LIBX264
        playlist.options.h264.audio_codec = streaming_pb2.H264.AAC

        thumbnail = request.thumbnails.add()
        thumbnail.upload_to.bucket = "deliverables"
        thumbnail.upload_to.key = "thumbnails/poster.jpg"
        thumbnail.thumbnail_time = "00:00:02.500"
        thumbnail.options.scale_width = 640
        thumbnail.options.scale_height = 360
        thumbnail.use_watermark = True
        return request

    def test_maps_all_public_fields_without_copying_webhook_url(self):
        request = self.make_request()
        request.webhook_url = "https://callback.example.test/private"
        normalized = self.adapter._build_durable_vod_request(request)

        self.assertEqual(normalized["video"], {
            "bucket": "source", "key": "incoming/source.mp4",
        })
        self.assertEqual(normalized["watermark"]["input_key"], "branding/logo.png")
        self.assertEqual(normalized["playlists"][0]["protocol"], "hls")
        self.assertEqual(normalized["playlists"][0]["quality_names"], ["360p", "720p"])
        self.assertEqual(normalized["playlists"][0]["video_codec"], "libx264")
        self.assertTrue(normalized["playlists"][0]["fragmented"])
        self.assertTrue(normalized["playlists"][0]["use_watermark"])
        self.assertEqual(
            normalized["thumbnails"][0]["thumbnail_time"], "00:00:02.500"
        )
        self.assertNotIn("webhook_url", normalized)
        self.assertNotIn("callback.example.test", repr(normalized))

    def test_compiles_normalized_proto_request_into_complete_dag(self):
        normalized = self.adapter._build_durable_vod_request(self.make_request())
        plan = build_vod_execution_plan("job-adapter-test", normalized)
        operations = [node["operation"] for node in plan["nodes"]]

        self.assertIn("video.input.check", operations)
        self.assertIn("video.input.download", operations)
        self.assertIn("video.input.analyze", operations)
        self.assertIn("video.inputs.funnel", operations)
        self.assertIn("video.watermark.apply", operations)
        self.assertIn("video.hls.package", operations)
        self.assertIn("video.thumbnail.generate", operations)
        self.assertIn("video.output.upload_directory", operations)
        self.assertIn("video.output.upload_file", operations)

    def test_maps_dash_protocol_and_watermarked_video_output(self):
        request = self.make_request()
        request.playlists[0].protocol = streaming_pb2.Protocol.DASH
        request.watermark.upload_to.bucket = "deliverables"
        request.watermark.upload_to.key = "watermarked/source.mp4"
        normalized = self.adapter._build_durable_vod_request(request)

        self.assertEqual(normalized["playlists"][0]["protocol"], "dash")
        self.assertEqual(
            normalized["watermark"]["output_key"], "watermarked/source.mp4"
        )
        plan = build_vod_execution_plan("job-dash-test", normalized)
        operations = [node["operation"] for node in plan["nodes"]]
        self.assertIn("video.dash.package", operations)
        self.assertIn("video.watermark.apply", operations)
        self.assertIn("video.output.upload_file", operations)

    def test_custom_quality_mapping_omits_absent_bitrate_components(self):
        request = self.make_request()
        quality = request.playlists[0].options.custom_qualities.add()
        quality.size.width = 1280
        quality.size.height = 720
        normalized = self.adapter._build_durable_vod_request(request)
        self.assertEqual(
            normalized["playlists"][0]["custom_qualities"][0],
            {"size": [1280, 720], "bitrate": None},
        )

    def test_custom_quality_mapping_preserves_only_configured_bitrates(self):
        request = self.make_request()
        quality = request.playlists[0].options.custom_qualities.add()
        quality.bitrate.video = 900000
        quality.bitrate.overall = 1000000
        normalized = self.adapter._build_durable_vod_request(request)
        self.assertEqual(
            normalized["playlists"][0]["custom_qualities"][0],
            {"size": None, "bitrate": [900000, None, 1000000]},
        )

    def test_rejects_unknown_playlist_protocol_instead_of_treating_it_as_dash(self):
        request = self.make_request()
        request.playlists[0].protocol = 99
        with self.assertRaisesRegex(ValueError, "unsupported VOD playlist protocol"):
            self.adapter._build_durable_vod_request(request)

    def test_full_plan_uses_legacy_output_ids_for_terminal_stage_keys(self):
        request = self.make_request()
        request.playlists[0].use_watermark = False
        request.watermark.upload_to.bucket = "deliverables"
        request.watermark.upload_to.key = "watermarked/source.mp4"
        plan = self.adapter._build_durable_vod_plan("job-output-ids", request)
        output_nodes = [
            node for node in plan["nodes"]
            if node["operation"] in (
                "video.output.upload_directory", "video.output.upload_file"
            )
        ]
        stage_keys = {node["stage_key"] for node in output_nodes}
        self.assertEqual(stage_keys, {"p0", "wt0", "wv0"})
        self.assertEqual(len(stage_keys), len(output_nodes))
        self.assertTrue(all(node["stage_key"] for node in plan["nodes"]))

    def test_full_plan_contains_all_execution_phases_and_disabled_callbacks(self):
        plan = self.adapter._build_durable_vod_plan(
            "job-full-dag", self.make_request()
        )
        operations = [node["operation"] for node in plan["nodes"]]
        for operation in (
            "video.input.check", "video.output.bucket.check",
            "video.input.download", "video.input.analyze",
            "video.inputs.funnel", "video.watermark.apply",
            "video.hls.package", "video.thumbnail.generate",
            "video.output.upload_directory", "video.output.upload_file",
        ):
            self.assertIn(operation, operations)
        callbacks = [
            node for node in plan["nodes"]
            if node["operation"] == "video.webhook.deliver"
        ]
        self.assertTrue(callbacks)
        self.assertTrue(all(
            node["parameters"]["enabled"] is False for node in callbacks
        ))
        self.assertTrue(all(
            "webhook_secret_ref" not in node["parameters"] for node in callbacks
        ))

    def test_durable_output_counts_ignore_internal_and_callback_stages(self):
        from video_streaming.grpc.servicers.mixins.get_results import GetResultsMixin

        stages = [
            {"key": "input-video-check", "status": "FAILED"},
            {"key": "playlist-0-package", "status": "FAILED"},
            {"key": "p0", "status": "FINISHED"},
            {"key": "p1", "status": "CANCELLED"},
            {"key": "p2", "status": "FAILED"},
            {"key": "p0-webhook", "status": "FAILED"},
        ]
        counts = GetResultsMixin._durable_output_status_counts(
            stages,
            {
                "no_watermarked_playlists_ids": ["p0", "p1", "p2"],
                "no_watermarked_thumbnails_ids": [],
                "watermarked_outputs_ids": [],
            },
            plan_backed=True,
        )
        self.assertEqual(counts, {"ready": 1, "revoked": 1, "failed": 1})

    def test_rejects_invalid_thumbnail_timestamp_during_plan_compilation(self):
        normalized = self.adapter._build_durable_vod_request(self.make_request())
        normalized["thumbnails"][0]["thumbnail_time"] = "not-a-timestamp"
        with self.assertRaisesRegex(ValueError, "FFmpeg timestamp"):
            build_vod_execution_plan("job-invalid-time", normalized)


    def test_rejects_incomplete_thumbnail_when_playlist_is_valid(self):
        from video_streaming.grpc import exceptions

        request = self.make_request()
        request.thumbnails[0].upload_to.key = ""
        with self.assertRaises(exceptions.S3KeyCanNotBeEmptyException):
            self.adapter._build_durable_vod_request(request)

    def test_rejects_incomplete_playlist_when_thumbnail_is_valid(self):
        from video_streaming.grpc import exceptions

        request = self.make_request()
        request.playlists[0].upload_to.key = "   "
        with self.assertRaises(exceptions.S3KeyCanNotBeEmptyException):
            self.adapter._build_durable_vod_request(request)

    def test_rejects_omitted_output_between_valid_outputs_without_reindexing(self):
        from video_streaming.grpc import exceptions

        request = self.make_request()
        middle = request.playlists.add()
        middle.protocol = streaming_pb2.Protocol.HLS
        middle.upload_to.bucket = "deliverables"
        middle.upload_to.key = ""
        last = request.playlists.add()
        last.protocol = streaming_pb2.Protocol.HLS
        last.upload_to.bucket = "deliverables"
        last.upload_to.key = "hls/third.m3u8"
        with self.assertRaises(exceptions.S3KeyCanNotBeEmptyException):
            self.adapter._build_durable_vod_request(request)

    def test_rejects_watermarked_output_without_watermark_input(self):
        request = self.make_request()
        request.watermark.s3_input.key = ""
        with self.assertRaisesRegex(ValueError, "watermark input"):
            self.adapter._build_durable_vod_plan("job-missing-watermark", request)

    def test_rejects_duplicate_output_destinations(self):
        request = self.make_request()
        request.thumbnails[0].upload_to.bucket = request.playlists[0].upload_to.bucket
        request.thumbnails[0].upload_to.key = request.playlists[0].upload_to.key
        with self.assertRaisesRegex(ValueError, "duplicate output locations"):
            self.adapter._build_durable_vod_plan("job-duplicate-destination", request)

    def test_rejects_empty_output_collection(self):
        request = self.make_request()
        del request.playlists[:]
        del request.thumbnails[:]
        request.watermark.upload_to.key = ""
        with self.assertRaisesRegex(ValueError, "at least one output"):
            self.adapter._build_durable_vod_plan("job-no-outputs", request)


if __name__ == "__main__":
    unittest.main()
