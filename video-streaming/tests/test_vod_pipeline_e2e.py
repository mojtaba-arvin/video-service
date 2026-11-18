"""Full gRPC -> Celery -> FFmpeg -> S3-compatible storage smoke test.

The broker, Redis cache, and S3-compatible object store are replaced with
in-process deterministic test doubles. gRPC, Celery task signatures/chains,
FFmpeg/FFprobe, HLS packaging, and the HTTP webhook execute for real.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from concurrent import futures
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import uuid4


_REQUIRED_MODULES = (
    "boto3",
    "celery",
    "ffmpeg_streaming",
    "grpc",
    "grpc_health",
    "psutil",
    "redis",
)
_HAS_RUNTIME = all(
    importlib.util.find_spec(module_name) is not None
    for module_name in _REQUIRED_MODULES
)


class InMemoryCache:
    def __init__(self):
        self.values = {}

    def set(self, key, value, timeout=None):
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        self.values[key] = value

    def get(self, key, decode=True):
        value = self.values.get(key)
        if not decode or not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value

    def incr(self, key, amount=1):
        value = int(self.get(key) or 0) + amount
        self.values[key] = value
        return value

    def incr_by_float(self, key, amount=1.0):
        value = float(self.get(key) or 0) + amount
        self.values[key] = value
        return value

    def delete(self, *keys):
        deleted = 0
        for key in keys:
            if key in self.values:
                del self.values[key]
                deleted += 1
        return deleted


class InMemoryS3:
    def __init__(self, input_bytes):
        self.buckets = {"vod-input": {"source.mp4": input_bytes}}
        self.upload_order = []

    def read_object_bytes(self, key, bucket_name=None):
        return self.buckets.get(bucket_name or "", {}).get(key)

    def put_object_bytes(self, key, content, bucket_name=None,
                         content_type="application/json"):
        self.buckets.setdefault(bucket_name, {})[key] = content
        return {"Key": key}

    def head(self, key, bucket_name=None, **kwargs):
        bucket = self.buckets.get(bucket_name or "", {})
        value = bucket.get(key)
        if value is None:
            return None
        return {"ContentLength": len(value), "ETag": '"test-etag"'}

    def delete_object(self, key, bucket_name=None, **kwargs):
        self.buckets.setdefault(bucket_name, {}).pop(key, None)
        return {"Deleted": key}

    def read_range(self, key, bucket_name=None, start=0, end=0):
        value = self.buckets.get(bucket_name or "", {}).get(key)
        if value is None:
            return None
        return value[start:end + 1]

    def head_bucket(self, bucket_name=None, **kwargs):
        if bucket_name in self.buckets:
            return {"Bucket": bucket_name}
        return None

    def create_bucket(self, bucket_name=None, **kwargs):
        self.buckets.setdefault(bucket_name, {})
        return {"Bucket": bucket_name}

    def download(self, key, destination_path=None, bucket_name=None,
                 callback=None, **kwargs):
        value = self.buckets[bucket_name][key]
        os.makedirs(os.path.dirname(destination_path), exist_ok=True)
        with open(destination_path, "wb") as destination:
            destination.write(value)
        if callback:
            callback(len(value))
        return destination_path

    def upload_file_by_path(self, key, file_path, bucket_name=None,
                            callback=None, **kwargs):
        with open(file_path, "rb") as source:
            value = source.read()
        self.buckets.setdefault(bucket_name, {})[key] = value
        self.upload_order.append(key)
        if callback:
            callback(len(value))


@unittest.skipUnless(
    _HAS_RUNTIME and shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "Python 3.9 runtime dependencies and FFmpeg/FFprobe are required",
)
class GrpcCeleryVodEndToEndTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp_dir.name)
        cls.download_dir = cls.root / "downloads"
        cls.processed_dir = cls.root / "processed"
        cls.download_dir.mkdir()
        cls.processed_dir.mkdir()

        cls.env_path = (
            Path(__file__).resolve().parents[1]
            / "video_streaming"
            / ".env"
        )
        cls.previous_env = (
            cls.env_path.read_bytes() if cls.env_path.exists() else None
        )
        cls.env_path.write_text(
            "\n".join([
                "CELERY_BROKER_URL=memory://",
                "CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/1",
                "WORKFLOW_DATABASE_URL={}".format(
                    os.environ["WORKFLOW_DATABASE_URL"]
                ),
                "WORKFLOW_DATABASE_POOL_SIZE=2",
                "WORKFLOW_DATABASE_MAX_OVERFLOW=2",
                "TASK_RETRY_BACKOFF_MAX=2",
                "TASK_RETRY_FFMPEG_COMMAND_MAX=0",
                "DEFAULT_PLAYLIST_IS_HLS=True",
                "DEFAULT_SEGMENT_TYPE_IS_FMP4=False",
                "DONT_REPLACE_OUTPUT=False",
                "CREATE_OUTPUT_BUCKET=True",
                "DEFAULT_ENCODE_FORMAT=h264",
                "S3_ENDPOINT_URL=http://127.0.0.1:9000",
                "S3_ACCESS_KEY_ID=test",
                "S3_SECRET_ACCESS_KEY=test",
                "S3_REGION_NAME=us-east-1",
                "S3_IS_SECURE=False",
                "S3_DEFAULT_BUCKET=vod-input",
                "S3_DEFAULT_INPUT_BUCKET_NAME=vod-input",
                "S3_DEFAULT_OUTPUT_BUCKET_NAME=vod-output",
                "TMP_DOWNLOADED_DIR={}/".format(cls.download_dir),
                "TMP_PROCESSED_DIR={}/".format(cls.processed_dir),
                "FFPROBE_BIN_PATH={}".format(shutil.which("ffprobe")),
                "FFMPEG_BIN_PATH={}".format(shutil.which("ffmpeg")),
                "REDIS_TIMEOUT_SECOND=3600",
                "REDIS_URL=redis://127.0.0.1:6379/0",
            ]) + "\n",
            encoding="utf-8",
        )

        os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
        os.environ["AWS_ACCESS_KEY_ID"] = "test"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "test"
        os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

        cls.input_path = cls.root / "source.mp4"
        subprocess.run(
            [
                shutil.which("ffmpeg"),
                "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i",
                "testsrc2=size=320x180:rate=24:duration=3",
                "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=44100:duration=3",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", "-movflags", "+faststart",
                "-y", str(cls.input_path),
            ],
            check=True, capture_output=True, text=True,
        )

        # Import the application only after its local environment is present.
        cls.grpc = __import__("grpc")
        from video_streaming.celery import celery_app
        from video_streaming.ffmpeg.tasks.base import BaseStreamingTask
        from video_streaming.grpc.protos import (
            streaming_pb2,
            streaming_pb2_grpc,
        )
        from video_streaming.grpc.servicers.streaming_servicer import Streaming

        cls.pb2 = streaming_pb2
        cls.pb2_grpc = streaming_pb2_grpc
        cls.Streaming = Streaming
        cls.BaseStreamingTask = BaseStreamingTask
        cls.celery_app = celery_app

        cls.cache = InMemoryCache()
        cls.storage = InMemoryS3(cls.input_path.read_bytes())
        BaseStreamingTask.cache = cls.cache
        BaseStreamingTask.s3_service = cls.storage
        BaseStreamingTask.TMP_DOWNLOADED_DIR = str(cls.download_dir) + "/"
        BaseStreamingTask.TMP_PROCESSED_DIR = str(cls.processed_dir) + "/"
        Streaming.cache = cls.cache

        celery_app.conf.update(
            task_always_eager=True,
            task_eager_propagates=True,
            task_store_eager_result=True,
            broker_url="memory://",
            result_backend="redis://127.0.0.1:6379/1",
        )
        # The application may have cached DisabledBackend while importing
        # its production defaults before the test environment was overridden.
        from celery.backends.redis import RedisBackend
        celery_app._backend_cache = None
        celery_app._local.backend = RedisBackend(
            app=celery_app,
            url="redis://127.0.0.1:6379/1",
        )
        celery_app.set_default()
        celery_app.set_current()
        if celery_app.backend.__class__.__name__ == "DisabledBackend":
            raise RuntimeError("Celery chord result backend was not configured")

        class WebhookHandler(BaseHTTPRequestHandler):
            received = []

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.received.append(self.rfile.read(length))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        cls.webhook_handler = WebhookHandler
        cls.webhook_server = HTTPServer(("127.0.0.1", 0), WebhookHandler)
        cls.webhook_thread = threading.Thread(
            target=cls.webhook_server.serve_forever,
            daemon=True,
        )
        cls.webhook_thread.start()
        cls.webhook_url = "http://127.0.0.1:{}/callback".format(
            cls.webhook_server.server_port
        )

        cls.grpc_server = cls.grpc.server(
            futures.ThreadPoolExecutor(max_workers=4)
        )
        cls.servicer = Streaming()
        cls.servicer._add_to_server(cls.grpc_server)
        cls.grpc_port = cls.grpc_server.add_insecure_port("127.0.0.1:0")
        cls.grpc_server.start()
        cls.channel = cls.grpc.insecure_channel(
            "127.0.0.1:{}".format(cls.grpc_port)
        )
        cls.stub = cls.pb2_grpc.StreamingStub(cls.channel)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "channel"):
            cls.channel.close()
        if hasattr(cls, "grpc_server"):
            cls.grpc_server.stop(0)
        if hasattr(cls, "webhook_server"):
            cls.webhook_server.shutdown()
            cls.webhook_server.server_close()
        if hasattr(cls, "webhook_thread"):
            cls.webhook_thread.join(timeout=2)
        if cls.previous_env is None:
            try:
                cls.env_path.unlink()
            except FileNotFoundError:
                pass
        else:
            cls.env_path.write_bytes(cls.previous_env)
        cls.temp_dir.cleanup()

    def test_qoe_proto_preserves_partial_cost_coverage_fields(self):
        from video_streaming.workflows.qoe import playback_qoe_report_payload

        report = {
            "job_id": "qoe-proto-job",
            "stage_key": "playlist-0",
            "stage_status": "FINISHED",
            "session_count": 2,
            "playback_started_count": 2,
            "average_startup_ms": 120,
            "rebuffer_count": 1,
            "total_buffering_ms": 350,
            "decoder_error_count": 0,
            "buffering_ratio_percent": 0.5,
            "total_cpu_seconds": 8.5,
            "total_estimated_cost": None,
            "partial_estimated_cost": 1.75,
            "cost_estimate_complete": False,
            "cost_currency": None,
            "pricing_model_id": None,
            "priced_attempt_count": 1,
            "total_attempt_count": 2,
            "cost_coverage": {
                "complete_attempt_count": 0,
                "missing_dimensions_by_attempt": {"gpu_seconds": 2},
                "excluded_dimensions": ["provider_invoice"],
                "pricing_model_versions": ["relative-resource-index-v1:1"],
            },
        }

        payload = playback_qoe_report_payload(report)
        response = self.pb2.PlaybackQoEReportResponse(**payload)

        self.assertFalse(response.total_estimated_cost_available)
        self.assertTrue(response.partial_estimated_cost_available)
        self.assertEqual(response.partial_estimated_cost, 1.75)
        self.assertEqual(response.complete_attempt_count, 0)
        self.assertEqual(
            dict(response.missing_cost_dimensions_by_attempt),
            {"gpu_seconds": 2},
        )
        self.assertEqual(
            list(response.excluded_cost_dimensions),
            ["provider_invoice"],
        )
        self.assertEqual(
            list(response.pricing_model_versions),
            ["relative-resource-index-v1:1"],
        )

    def test_processing_finished_requires_durable_publication_completion(self):
        from video_streaming.core.constants import (
            CacheKeysTemplates,
            OutputStatus,
        )
        from video_streaming.ffmpeg.tasks.mixins.output import BaseOutputMixin
        from video_streaming.workflows.service import get_workflow_repository

        class StatusProbe(BaseOutputMixin):
            def __init__(self):
                self.cache = InMemoryCache()
                self.output_status = OutputStatus

            def can_set_output_status(self, output_id, request_id):
                return False

        repository = get_workflow_repository()
        job_id = str(uuid4())
        repository.create_job(
            job_id=job_id,
            idempotency_key="status-gate-{}".format(job_id),
            request_hash="e" * 64,
            stage_specs=[{
                "stage_key": "p0",
                "operation": "video.hls.package_and_publish",
            }],
        )
        attempt_id, _ = repository.start_attempt(job_id, "p0")
        repository.mark_validated(
            job_id, "p0", attempt_id,
            {"valid": True, "errors": []},
        )

        probe = StatusProbe()
        probe.cache.set(
            CacheKeysTemplates.JOB_DETAILS.format(request_id=job_id),
            {"no_watermarked_playlists_ids": ["p0"],
             "watermarked_playlists_ids": []},
        )
        with self.assertRaisesRegex(RuntimeError, "durable workflow stage"):
            probe.save_output_status(
                OutputStatus.PROCESSING_FINISHED, "p0", job_id
            )

        repository.start_publishing(job_id, "p0", attempt_id)
        repository.finish_stage(
            job_id, "p0", attempt_id,
            artifacts=[{
                "artifact_type": "streaming_manifest",
                "logical_name": "master",
                "uri": "s3://video/master.m3u8",
                "size_bytes": 12,
            }],
            metrics={"input_bytes": 100, "output_bytes": 12},
        )
        probe.save_output_status(
            OutputStatus.PROCESSING_FINISHED, "p0", job_id
        )
        probe.save_output_status(
            OutputStatus.UPLOADING_FINISHED, "p0", job_id
        )

    def test_grpc_job_runs_through_celery_ffmpeg_and_storage(self):
        options = self.pb2.ConvertOptions(
            fmp4=False,
            quality_names=[self.pb2.QualityName.R_144P],
            h264=self.pb2.H264(
                video_codec=self.pb2.H264.LIBX264,
                audio_codec=self.pb2.H264.AAC,
            ),
        )
        playlist = self.pb2.PlaylistOutput(
            protocol=self.pb2.Protocol.HLS,
            upload_to=self.pb2.S3Output(
                key="vod/e2e/master.m3u8",
                bucket="vod-output",
                create_bucket=True,
            ),
            options=options,
        )
        request = self.pb2.JobRequest(
            webhook_url=self.webhook_url,
            reference_id="vod-e2e-reference",
            idempotency_key="vod-e2e-idempotency-key",
            video=self.pb2.Video(
                s3_input=self.pb2.S3Input(
                    key="source.mp4",
                    bucket="vod-input",
                )
            ),
            playlists=[playlist],
            thumbnails=[
                self.pb2.ThumbnailOutput(
                    thumbnail_time="00:00:01",
                    upload_to=self.pb2.S3Output(
                        key="vod/e2e/thumbnail.jpg",
                        bucket="vod-output",
                        create_bucket=True,
                    ),
                    options=self.pb2.ThumbnailOptions(
                        scale_width=160,
                        scale_height=0,
                    ),
                )
            ],
        )

        accepted = self.stub.create_job(request, timeout=120)
        self.assertTrue(accepted.tracking_id)

        replayed = self.stub.create_job(request, timeout=120)
        self.assertEqual(replayed.tracking_id, accepted.tracking_id)

        from video_streaming.workflows.service import get_workflow_repository
        snapshot = get_workflow_repository().get_job_snapshot(
            accepted.tracking_id
        )
        self.assertIsNotNone(snapshot)
        self.assertEqual(len(snapshot["stages"]), 2)
        self.assertEqual(
            {stage["key"] for stage in snapshot["stages"]},
            {"p0", "t0"},
        )
        for durable_stage in snapshot["stages"]:
            self.assertEqual(durable_stage["status"], "FINISHED")
            self.assertEqual(len(durable_stage["attempts"]), 1)
            attempt = durable_stage["attempts"][0]
            self.assertEqual(attempt["status"], "SUCCEEDED")
            self.assertGreater(attempt["duration_ms"], 0)
            self.assertEqual(
                attempt["input_bytes"], self.input_path.stat().st_size
            )
            self.assertGreater(attempt["output_bytes"], 0)
            self.assertGreater(attempt["artifact_count"], 0)
            self.assertGreater(attempt["estimated_cost"], 0)
            self.assertEqual(attempt["cost_currency"], "RUNIT")
            self.assertEqual(
                attempt["pricing_model_id"],
                "relative-resource-index-v1",
            )
        playlist_stage = next(
            stage for stage in snapshot["stages"] if stage["key"] == "p0"
        )
        thumbnail_stage = next(
            stage for stage in snapshot["stages"] if stage["key"] == "t0"
        )
        playlist_artifacts = [
            item["type"] for item in playlist_stage["artifacts"]
        ]
        thumbnail_artifacts = [
            item["type"] for item in thumbnail_stage["artifacts"]
        ]
        self.assertIn("validation_report", playlist_artifacts)
        self.assertIn("streaming_manifest", playlist_artifacts)
        self.assertIn("package_inventory", playlist_artifacts)
        self.assertIn("validation_report", thumbnail_artifacts)
        self.assertIn("image_thumbnail", thumbnail_artifacts)

        results = self.stub.get_results(
            self.pb2.JobsResultsRequest(
                tracking_ids=[accepted.tracking_id]
            ),
            timeout=30,
        )
        self.assertEqual(len(results.results), 1)
        result = results.results[0]

        self.assertEqual(result.status, self.pb2.PrimaryStatus.FINISHED)
        self.assertEqual(result.ready_outputs, 2)
        self.assertEqual(result.failed_outputs, 0)
        self.assertEqual(len(result.playlists.outputs), 1)
        self.assertEqual(
            result.playlists.outputs[0].status,
            self.pb2.OutputStatus.UPLOADING_FINISHED,
        )
        self.assertEqual(
            result.thumbnails.outputs[0].status,
            self.pb2.OutputStatus.UPLOADING_FINISHED,
        )

        output_objects = self.storage.buckets["vod-output"]
        master_key = "vod/e2e/master.m3u8"
        variant_key = "vod/e2e/master_144p.m3u8"
        self.assertIn(master_key, output_objects)
        self.assertIn(variant_key, output_objects)
        self.assertIn("vod/e2e/thumbnail.jpg", output_objects)
        self.assertIn(
            "vod/e2e/.master.m3u8.artifacts.json", output_objects
        )
        self.assertTrue(
            any(key.endswith(".ts") for key in output_objects)
        )
        self.assertGreater(
            self.storage.upload_order.index(master_key),
            self.storage.upload_order.index(variant_key),
        )
        self.assertTrue(self.webhook_handler.received)
        self.assertEqual(
            json.loads(self.webhook_handler.received[0])["reference_id"],
            "vod-e2e-reference",
        )

        # Redis is a progress/cache projection, not the durable source of truth.
        self.cache.values.clear()
        recovered = self.stub.get_results(
            self.pb2.JobsResultsRequest(
                tracking_ids=[accepted.tracking_id]
            ),
            timeout=30,
        ).results[0]
        self.assertEqual(recovered.status, self.pb2.PrimaryStatus.FINISHED)
        self.assertEqual(recovered.ready_outputs, 2)
        self.assertEqual(len(recovered.playlists.outputs), 1)
        self.assertEqual(
            recovered.playlists.outputs[0].status,
            self.pb2.OutputStatus.UPLOADING_FINISHED,
        )
        self.assertEqual(len(recovered.thumbnails.outputs), 1)
        self.assertEqual(
            recovered.thumbnails.outputs[0].status,
            self.pb2.OutputStatus.UPLOADING_FINISHED,
        )

        # QoE events are session-scoped, idempotent, and correlated to the
        # same durable playlist stage and its measured resource-cost record.
        playback = self.stub.start_playback_session(
            self.pb2.StartPlaybackSessionRequest(
                tracking_id=accepted.tracking_id,
                output_id="p0",
                client_version="synthetic-player/1.0",
            ),
            timeout=30,
        )
        self.assertTrue(playback.session_id)
        self.assertTrue(playback.session_token)
        self.assertTrue(playback.manifest_artifact_id)

        started_event = self.pb2.RecordPlaybackEventRequest(
            session_id=playback.session_id,
            session_token=playback.session_token,
            event_id="e2e-playback-start",
            event_type=self.pb2.PlaybackEventType.PLAYBACK_STARTED,
            startup_ms=640,
            client_version="synthetic-player/1.0",
        )
        started = self.stub.record_playback_event(
            started_event, timeout=30
        )
        self.assertTrue(started.accepted)
        self.assertFalse(started.duplicate)
        replayed_start = self.stub.record_playback_event(
            started_event, timeout=30
        )
        self.assertTrue(replayed_start.duplicate)

        self.stub.record_playback_event(
            self.pb2.RecordPlaybackEventRequest(
                session_id=playback.session_id,
                session_token=playback.session_token,
                event_id="e2e-buffering-start",
                event_type=self.pb2.PlaybackEventType.BUFFERING_STARTED,
            ),
            timeout=30,
        )
        buffering_end = self.stub.record_playback_event(
            self.pb2.RecordPlaybackEventRequest(
                session_id=playback.session_id,
                session_token=playback.session_token,
                event_id="e2e-buffering-end",
                event_type=self.pb2.PlaybackEventType.BUFFERING_ENDED,
            ),
            timeout=30,
        )
        self.assertEqual(buffering_end.rebuffer_count, 1)

        decoder_error = self.stub.record_playback_event(
            self.pb2.RecordPlaybackEventRequest(
                session_id=playback.session_id,
                session_token=playback.session_token,
                event_id="e2e-decoder-error",
                event_type=self.pb2.PlaybackEventType.DECODER_ERROR,
                error_code="SYNTHETIC_DECODE_FAILURE",
                decoder="synthetic-decoder",
            ),
            timeout=30,
        )
        self.assertEqual(decoder_error.decoder_error_count, 1)

        ended_event = self.pb2.RecordPlaybackEventRequest(
            session_id=playback.session_id,
            session_token=playback.session_token,
            event_id="e2e-playback-ended",
            event_type=self.pb2.PlaybackEventType.PLAYBACK_ENDED,
        )
        ended = self.stub.record_playback_event(ended_event, timeout=30)
        self.assertTrue(ended.ended)
        self.assertTrue(
            self.stub.record_playback_event(
                ended_event, timeout=30
            ).duplicate
        )

        qoe = self.stub.get_playback_qoe_report(
            self.pb2.GetPlaybackQoEReportRequest(
                tracking_id=accepted.tracking_id,
                output_id="p0",
            ),
            timeout=30,
        )
        self.assertEqual(qoe.session_count, 1)
        self.assertEqual(qoe.playback_started_count, 1)
        self.assertEqual(qoe.average_startup_ms, 640)
        self.assertEqual(qoe.rebuffer_count, 1)
        self.assertEqual(qoe.decoder_error_count, 1)
        self.assertGreaterEqual(qoe.total_cpu_seconds, 0)
        self.assertGreater(qoe.total_estimated_cost, 0)
        self.assertEqual(qoe.cost_currency, "RUNIT")
        self.assertEqual(
            qoe.pricing_model_id, "relative-resource-index-v1"
        )


if __name__ == "__main__":
    unittest.main()
