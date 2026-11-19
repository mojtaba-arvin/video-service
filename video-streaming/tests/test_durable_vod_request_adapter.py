"""Tests for the gRPC-to-canonical VOD request adapter."""

import unittest

from video_streaming.grpc.protos import streaming_pb2
from video_streaming.grpc.servicers.mixins.create_job import CreateJobMixin
from video_streaming.workflows.vod_plan import build_vod_execution_plan


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

    def test_rejects_invalid_thumbnail_timestamp_during_plan_compilation(self):
        normalized = self.adapter._build_durable_vod_request(self.make_request())
        normalized["thumbnails"][0]["thumbnail_time"] = "not-a-timestamp"
        with self.assertRaisesRegex(ValueError, "FFmpeg timestamp"):
            build_vod_execution_plan("job-invalid-time", normalized)


if __name__ == "__main__":
    unittest.main()
