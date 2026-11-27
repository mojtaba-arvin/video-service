import hashlib
import io
import os
import tempfile
import unittest
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from video_streaming.workflows.artifact_store import S3ArtifactStore
from video_streaming.workflows.incremental_ingestion import (
    Base,
    IncompleteIngestion,
    IngestionBackpressure,
    IngestionCancelled,
    IngestionConflict,
    IncrementalIngestionService,
    InputContract,
    IngestionChunkRecord,
    IngestionSessionRecord,
)
from video_streaming.workflows.repository import JobRecord


class _Body(io.BytesIO):
    pass


class _S3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kwargs):
        identity = (Bucket, Key)
        if kwargs.get("IfNoneMatch") == "*" and identity in self.objects:
            error = RuntimeError("precondition failed")
            error.response = {
                "ResponseMetadata": {"HTTPStatusCode": 412},
                "Error": {"Code": "PreconditionFailed"},
            }
            raise error
        self.objects[identity] = {
            "body": Body.read(),
            "Metadata": kwargs.get("Metadata", {}),
            "ContentType": kwargs.get("ContentType"),
        }

    def get_object(self, Bucket, Key):
        item = self.objects[(Bucket, Key)]
        return {"Body": _Body(item["body"]), "Metadata": item["Metadata"]}

    def head_object(self, Bucket, Key):
        item = self.objects[(Bucket, Key)]
        return {
            "ContentLength": len(item["body"]),
            "Metadata": item["Metadata"],
            "ContentType": item["ContentType"],
        }

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


class IncrementalIngestionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:", connect_args={"check_same_thread": False},
            future=True,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, future=True)
        self.job_id = str(uuid4())
        with self.sessions.begin() as session:
            session.add(JobRecord(
                id=self.job_id, idempotency_key="ingest-" + self.job_id,
                request_hash="a" * 64, reference_id="ingest-test",
            ))
        self.s3 = _S3()
        self.store = S3ArtifactStore(self.s3, "media-output")
        self.service = IncrementalIngestionService(
            artifact_store=self.store, engine=self.engine,
            max_chunk_size_bytes=8, max_concurrent_uploads=2,
        )

    def tearDown(self):
        self.service.close()
        self.engine.dispose()

    def create(self, data, chunk_size=4, max_pending=4):
        return self.service.create(
            self.job_id, "source.mp4", chunk_size,
            expected_size_bytes=len(data),
            expected_sha256=hashlib.sha256(data).hexdigest(),
            max_pending_chunks=max_pending,
        )["id"]

    def add(self, ingestion_id, index, payload, start=None, end=None, chunk_id=None):
        return self.service.accept_chunk(
            ingestion_id, chunk_id or str(uuid4()), index, index * 4,
            payload, hashlib.sha256(payload).hexdigest(), start, end,
        )

    def test_interrupted_ingestion_resumes_and_publishes_only_verified_source(self):
        source = b"abcdefghij"
        ingestion_id = self.create(source)
        self.add(ingestion_id, 0, source[:4])
        self.add(ingestion_id, 1, source[4:8])

        # Simulate a process restart: the new service reads the same durable ledger.
        restarted = IncrementalIngestionService(
            artifact_store=self.store, engine=self.engine,
            max_chunk_size_bytes=8, max_concurrent_uploads=2,
        )
        restarted.accept_chunk(
            ingestion_id, str(uuid4()), 2, 8, source[8:],
            hashlib.sha256(source[8:]).hexdigest(),
        )
        result = restarted.finalize(ingestion_id)
        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(result["final_size_bytes"], len(source))
        self.assertEqual(result["final_sha256"], hashlib.sha256(source).hexdigest())
        self.assertTrue(result["final_artifact_uri"].startswith("s3://media-output/"))
        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "restored.mp4")
            self.store.download_file(result["final_artifact_uri"], destination)
            with open(destination, "rb") as stream:
                self.assertEqual(stream.read(), source)
        self.assertEqual(restarted.finalize(ingestion_id)["final_artifact_uri"], result["final_artifact_uri"])
        restarted.close()

    def test_duplicate_chunk_replay_is_idempotent_but_conflict_is_rejected(self):
        ingestion_id = self.create(b"abcdefgh")
        chunk_id = str(uuid4())
        payload = b"abcd"
        first = self.service.accept_chunk(
            ingestion_id, chunk_id, 0, 0, payload, hashlib.sha256(payload).hexdigest()
        )
        replay = self.service.accept_chunk(
            ingestion_id, chunk_id, 0, 0, payload, hashlib.sha256(payload).hexdigest()
        )
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(IngestionConflict):
            self.service.accept_chunk(
                ingestion_id, chunk_id, 0, 0, b"wxyz",
                hashlib.sha256(b"wxyz").hexdigest(),
            )

    def test_checksum_failure_never_commits_a_chunk(self):
        ingestion_id = self.create(b"abcdefgh")
        with self.assertRaises(IngestionConflict):
            self.service.accept_chunk(ingestion_id, str(uuid4()), 0, 0, b"abcd", "0" * 64)
        self.assertEqual(self.service.get(ingestion_id)["chunks"], [])

    def test_out_of_order_chunks_are_provisional_until_contiguous_prefix_exists(self):
        ingestion_id = self.create(b"abcdefgh")
        self.add(ingestion_id, 1, b"efgh", 1, 2)
        self.assertEqual(self.service.progressive_view(ingestion_id)["status"], "UNAVAILABLE")
        self.add(ingestion_id, 0, b"abcd", 0, 1)
        view = self.service.progressive_view(ingestion_id)
        self.assertEqual(view["status"], "PROVISIONAL")
        self.assertEqual(view["contiguous_bytes"], 8)
        self.assertEqual(view["duration_seconds"], 2)
        self.assertIsNone(view["final_artifact_uri"])

    def test_downstream_contract_requires_declared_verified_prefix_or_final_source(self):
        ingestion_id = self.create(b"abcdefgh")
        self.add(ingestion_id, 0, b"abcd", 0, 1)
        self.assertTrue(self.service.input_contract_satisfied(
            ingestion_id, InputContract(min_bytes=4, min_duration_seconds=1)
        ))
        self.assertFalse(self.service.input_contract_satisfied(
            ingestion_id, InputContract(min_bytes=4, require_complete_source=True)
        ))
        self.assertFalse(self.service.input_contract_satisfied(
            ingestion_id, InputContract(min_bytes=5)
        ))

    def test_missing_chunk_prevents_finalization_then_retry_succeeds(self):
        source = b"abcdefghij"
        ingestion_id = self.create(source)
        self.add(ingestion_id, 0, source[:4])
        self.add(ingestion_id, 2, source[8:])
        with self.assertRaises(IncompleteIngestion):
            self.service.finalize(ingestion_id)
        self.add(ingestion_id, 1, source[4:8])
        self.assertEqual(self.service.finalize(ingestion_id)["state"], "COMPLETE")

    def test_offset_order_and_reorder_window_are_enforced(self):
        ingestion_id = self.create(b"abcdefghijklmnop", max_pending=2)
        with self.assertRaises(IngestionConflict):
            self.service.accept_chunk(
                ingestion_id, str(uuid4()), 1, 3, b"efgh",
                hashlib.sha256(b"efgh").hexdigest(),
            )
        with self.assertRaises(IngestionBackpressure):
            self.service.accept_chunk(
                ingestion_id, str(uuid4()), 2, 8, b"ijkl",
                hashlib.sha256(b"ijkl").hexdigest(),
            )

    def test_chunk_time_ranges_must_be_valid_and_contiguous_for_progress(self):
        ingestion_id = self.create(b"abcdefgh")
        with self.assertRaises(ValueError):
            self.add(ingestion_id, 0, b"abcd", 1, 1)
        self.add(ingestion_id, 0, b"abcd", 0, 1)
        self.add(ingestion_id, 1, b"efgh", 2, 3)
        view = self.service.progressive_view(ingestion_id)
        self.assertEqual(view["contiguous_bytes"], 4)
        self.assertEqual(view["duration_seconds"], 1)

    def test_cancel_cleans_only_its_own_provisional_chunks(self):
        first = self.create(b"abcdefgh")
        self.add(first, 0, b"abcd")
        first_uri = self.service.get(first)["chunks"][0]
        second = self.create(b"abcdefgh")
        self.add(second, 0, b"abcd")
        second_uri = self.service.get(second)["chunks"][0]
        self.service.cancel(first)
        self.assertFalse(any(key[1] in first_uri["checksum_sha256"] for key in self.s3.objects))
        self.assertTrue(any("chunks/00000000" in key[1] for key in self.s3.objects))
        self.assertEqual(self.service.get(first)["state"], "CANCELLED")
        with self.assertRaises(IngestionCancelled):
            self.add(first, 1, b"efgh")

    def test_cancel_does_not_remove_another_sessions_objects(self):
        first = self.create(b"abcdefgh")
        second = self.create(b"abcdefgh")
        self.add(first, 0, b"abcd")
        self.add(second, 0, b"abcd")
        second_objects = {
            key: value for key, value in self.s3.objects.items()
            if "chunks/00000000" in key[1]
        }
        self.service.cancel(first)
        self.assertEqual(self.s3.objects, second_objects)

    def test_final_source_checksum_mismatch_never_marks_complete(self):
        ingestion_id = self.service.create(
            self.job_id, "source.mp4", 4, expected_size_bytes=8,
            expected_sha256="0" * 64,
        )["id"]
        self.add(ingestion_id, 0, b"abcd")
        self.add(ingestion_id, 1, b"efgh")
        with self.assertRaises(IngestionConflict):
            self.service.finalize(ingestion_id)
        self.assertEqual(self.service.get(ingestion_id)["state"], "FINALIZING")
        self.assertIsNone(self.service.get(ingestion_id)["final_artifact_uri"])


if __name__ == "__main__":
    unittest.main()
