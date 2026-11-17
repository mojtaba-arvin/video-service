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

    def head(self, key, bucket_name=None, **kwargs):
        bucket = self.buckets.get(bucket_name or "", {})
        value = bucket.get(key)
        if value is None:
            return None
        return {"ContentLength": len(value), "ETag": '"test-etag"'}

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
            video=self.pb2.Video(
                s3_input=self.pb2.S3Input(
                    key="source.mp4",
                    bucket="vod-input",
                )
            ),
            playlists=[playlist],
        )

        accepted = self.stub.create_job(request, timeout=120)
        self.assertTrue(accepted.tracking_id)

        results = self.stub.get_results(
            self.pb2.JobsResultsRequest(
                tracking_ids=[accepted.tracking_id]
            ),
            timeout=30,
        )
        self.assertEqual(len(results.results), 1)
        result = results.results[0]

        self.assertEqual(result.status, self.pb2.PrimaryStatus.FINISHED)
        self.assertEqual(result.ready_outputs, 1)
        self.assertEqual(result.failed_outputs, 0)
        self.assertEqual(len(result.playlists.outputs), 1)
        self.assertEqual(
            result.playlists.outputs[0].status,
            self.pb2.OutputStatus.UPLOADING_FINISHED,
        )

        output_objects = self.storage.buckets["vod-output"]
        master_key = "vod/e2e/master.m3u8"
        variant_key = "vod/e2e/master_144p.m3u8"
        self.assertIn(master_key, output_objects)
        self.assertIn(variant_key, output_objects)
        self.assertTrue(
            any(key.endswith(".ts") for key in output_objects)
        )
        self.assertEqual(self.storage.upload_order[-1], master_key)
        self.assertTrue(self.webhook_handler.received)
        self.assertEqual(
            json.loads(self.webhook_handler.received[0])["reference_id"],
            "vod-e2e-reference",
        )


if __name__ == "__main__":
    unittest.main()
