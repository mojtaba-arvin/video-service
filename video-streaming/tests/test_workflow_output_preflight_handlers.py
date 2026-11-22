"""Tests for durable S3 output preflight handlers."""
import hashlib
import json
import unittest

from video_streaming.workflows.domain import AttemptLeaseLost
from video_streaming.workflows.errors import PermanentOperationError
from video_streaming.workflows.output_preflight_handlers import (
    build_output_preflight_handlers,
)


class _S3Error(Exception):
    def __init__(self, code, status):
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class _S3:
    def __init__(self):
        self.buckets = {"existing-output"}
        self.objects = set()
        self.created = []
        self.head_bucket_calls = []
        self.head_object_calls = []

    def head_bucket(self, **kwargs):
        bucket = kwargs["Bucket"]
        self.head_bucket_calls.append(kwargs)
        if bucket == "denied-output":
            raise _S3Error("AccessDenied", 403)
        if bucket not in self.buckets:
            raise _S3Error("404", 404)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def create_bucket(self, **kwargs):
        self.created.append(kwargs)
        self.buckets.add(kwargs["Bucket"])
        return {}

    def head_object(self, **kwargs):
        self.head_object_calls.append(kwargs)
        location = (kwargs["Bucket"], kwargs["Key"])
        if location in self.objects:
            return {"ContentLength": 42, "ETag": '"existing"'}
        if kwargs["Bucket"] not in self.buckets:
            raise _S3Error("NoSuchBucket", 404)
        raise _S3Error("404", 404)


class _ArtifactStore:
    def __init__(self):
        self.objects = {}

    def put_bytes(self, payload, logical_key, attempt_id, media_type=None):
        digest = hashlib.sha256(payload).hexdigest()
        uri = "s3://workflow-artifacts/{}/{}".format(
            attempt_id, logical_key.replace("/", "_")
        )
        self.objects[uri] = payload
        return {
            "uri": uri,
            "size_bytes": len(payload),
            "checksum_sha256": digest,
            "media_type": media_type,
        }


class _Context:
    def __init__(self, node_id, heartbeat_result=True):
        self.node_id = node_id
        self.attempt_id = "attempt-{}".format(node_id)
        self.heartbeat_result = heartbeat_result
        self.heartbeat_calls = 0
        self.cancelled = False

    def heartbeat(self):
        self.heartbeat_calls += 1
        return self.heartbeat_result

    def is_cancelled(self):
        return self.cancelled

    def cancel(self, reason=None):
        self.cancelled = True


class OutputPreflightHandlerTests(unittest.TestCase):
    def setUp(self):
        self.s3 = _S3()
        self.store = _ArtifactStore()
        self.handlers = build_output_preflight_handlers(
            self.s3, self.store, region_name="eu-central-1"
        )

    @staticmethod
    def _node(parameters):
        return {"parameters": parameters}

    def test_existing_bucket_produces_durable_ready_receipt(self):
        context = _Context("bucket-check")
        result = self.handlers["video.output.bucket.check"](
            self._node({
                "bucket": "existing-output",
                "create_bucket": False,
            }),
            context,
        )
        self.assertTrue(result["validation_report"]["valid"])
        self.assertEqual(len(result["artifacts"]), 1)
        artifact = result["artifacts"][0]
        receipt = json.loads(self.store.objects[artifact["uri"]].decode("utf-8"))
        self.assertTrue(receipt["ready"])
        self.assertFalse(receipt["created_by_attempt"])
        self.assertEqual(context.heartbeat_calls, 2)

    def test_missing_bucket_is_created_in_configured_region(self):
        result = self.handlers["video.output.bucket.check"](
            self._node({
                "bucket": "new-output",
                "create_bucket": True,
            }),
            _Context("create-bucket"),
        )
        self.assertIn("new-output", self.s3.buckets)
        self.assertEqual(
            self.s3.created[0]["CreateBucketConfiguration"],
            {"LocationConstraint": "eu-central-1"},
        )
        receipt = json.loads(
            self.store.objects[result["artifacts"][0]["uri"]].decode("utf-8")
        )
        self.assertTrue(receipt["created_by_attempt"])

    def test_missing_bucket_without_create_permission_fails(self):
        with self.assertRaises(PermanentOperationError):
            self.handlers["video.output.bucket.check"](
                self._node({
                    "bucket": "missing-output",
                    "create_bucket": False,
                }),
                _Context("missing-bucket"),
            )
        self.assertEqual(self.s3.created, [])

    def test_bucket_access_denial_is_not_treated_as_missing(self):
        with self.assertRaises(PermanentOperationError):
            self.handlers["video.output.bucket.check"](
                self._node({
                    "bucket": "denied-output",
                    "create_bucket": True,
                }),
                _Context("denied-bucket"),
            )
        self.assertEqual(self.s3.created, [])

    def test_key_preflight_rejects_disabled_dont_replace_contract(self):
        with self.assertRaises(PermanentOperationError):
            self.handlers["video.output.key.check"](
                self._node({
                    "bucket": "existing-output",
                    "key": "exports/master.m3u8",
                    "dont_replace": False,
                }),
                _Context("replace-enabled"),
            )
        self.assertEqual(self.s3.head_object_calls, [])

    def test_us_east_1_bucket_creation_omits_location_constraint(self):
        handlers = build_output_preflight_handlers(
            self.s3, self.store, region_name="us-east-1"
        )
        handlers["video.output.bucket.check"](
            self._node({
                "bucket": "east-output",
                "create_bucket": True,
            }),
            _Context("east-bucket"),
        )
        self.assertEqual(self.s3.created[-1], {"Bucket": "east-output"})

    def test_existing_output_key_is_rejected_when_replacement_disabled(self):
        self.s3.objects.add(("existing-output", "exports/master.m3u8"))
        with self.assertRaises(PermanentOperationError):
            self.handlers["video.output.key.check"](
                self._node({
                    "bucket": "existing-output",
                    "key": "exports/master.m3u8",
                    "dont_replace": True,
                }),
                _Context("existing-key"),
            )

    def test_missing_output_key_produces_available_receipt(self):
        result = self.handlers["video.output.key.check"](
            self._node({
                "bucket": "existing-output",
                "key": "exports/new/master.m3u8",
                "dont_replace": True,
            }),
            _Context("missing-key"),
        )
        receipt = json.loads(
            self.store.objects[result["artifacts"][0]["uri"]].decode("utf-8")
        )
        self.assertTrue(receipt["available"])
        self.assertEqual(receipt["key"], "exports/new/master.m3u8")

    def test_missing_bucket_is_not_mistaken_for_missing_key(self):
        with self.assertRaises(PermanentOperationError):
            self.handlers["video.output.key.check"](
                self._node({
                    "bucket": "missing-output",
                    "key": "exports/master.m3u8",
                    "dont_replace": True,
                }),
                _Context("missing-key-bucket"),
            )

    def test_lost_lease_stops_before_s3_side_effect(self):
        context = _Context("lost-lease", heartbeat_result=False)
        with self.assertRaises(AttemptLeaseLost):
            self.handlers["video.output.bucket.check"](
                self._node({
                    "bucket": "existing-output",
                    "create_bucket": False,
                }),
                context,
            )
        self.assertEqual(self.s3.head_bucket_calls, [])


if __name__ == "__main__":
    unittest.main()
