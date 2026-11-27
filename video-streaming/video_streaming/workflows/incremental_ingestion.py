"""Durable, resumable chunk ingestion for source media.

Chunks are private provisional objects. Only a complete, ordered, checksum-verified
source is promoted to an immutable final Artifact URI. This module deliberately
does not dispatch Celery work: callers must satisfy a declared InputContract and
the existing durable workflow dispatcher remains separately feature-gated.
"""
import hashlib
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey,
    Index, Integer, String, UniqueConstraint,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from .artifact_store import parse_artifact_uri
from .repository import Base, JobRecord, new_id, utcnow


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NAME = re.compile(r"^[^/\\\\\\x00-\\x1f]{1,255}$")


class IngestionError(RuntimeError):
    """Base class for incremental-ingestion failures."""


class IngestionConflict(IngestionError):
    """A replay reused a chunk identity with different content or metadata."""


class IngestionBackpressure(IngestionError):
    """The bounded out-of-order window or local upload capacity is exhausted."""


class IncompleteIngestion(IngestionError):
    """Finalization was requested while one or more chunks are missing."""


class IngestionCancelled(IngestionError):
    """The ingestion no longer accepts new chunks."""


class IngestionSessionRecord(Base):
    __tablename__ = "workflow_ingestions"
    __table_args__ = (
        CheckConstraint("chunk_size_bytes > 0", name="ck_workflow_ingestion_chunk_size"),
        CheckConstraint("max_pending_chunks > 0", name="ck_workflow_ingestion_pending_limit"),
        CheckConstraint(
            "expected_size_bytes IS NULL OR expected_size_bytes > 0",
            name="ck_workflow_ingestion_expected_size",
        ),
        CheckConstraint(
            "state IN ('OPEN', 'FINALIZING', 'COMPLETE', 'CANCELLED', 'FAILED')",
            name="ck_workflow_ingestion_state",
        ),
        UniqueConstraint("id", "job_id", name="uq_workflow_ingestion_job_identity"),
        Index("ix_workflow_ingestions_job_state", "job_id", "state"),
    )

    id = Column(String(36), primary_key=True)
    job_id = Column(String(36), ForeignKey("workflow_jobs.id", ondelete="CASCADE"), nullable=False)
    source_name = Column(String(255), nullable=False)
    chunk_size_bytes = Column(Integer, nullable=False)
    max_pending_chunks = Column(Integer, nullable=False)
    expected_size_bytes = Column(BigInteger, nullable=True)
    expected_sha256 = Column(String(64), nullable=True)
    state = Column(String(16), nullable=False, default="OPEN")
    final_artifact_uri = Column(String(2048), nullable=True)
    final_size_bytes = Column(BigInteger, nullable=True)
    final_sha256 = Column(String(64), nullable=True)
    cleanup_pending = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class IngestionChunkRecord(Base):
    __tablename__ = "workflow_ingestion_chunks"
    __table_args__ = (
        CheckConstraint("chunk_index >= 0", name="ck_workflow_ingestion_chunk_index"),
        CheckConstraint("byte_offset >= 0", name="ck_workflow_ingestion_chunk_offset"),
        CheckConstraint("size_bytes > 0", name="ck_workflow_ingestion_chunk_size"),
        CheckConstraint("length(checksum_sha256) = 64", name="ck_workflow_ingestion_chunk_hash"),
        CheckConstraint(
            "(start_seconds IS NULL AND end_seconds IS NULL) OR "
            "(start_seconds IS NOT NULL AND end_seconds IS NOT NULL AND "
            "start_seconds >= 0 AND end_seconds > start_seconds)",
            name="ck_workflow_ingestion_chunk_time_range",
        ),
        UniqueConstraint("ingestion_id", "chunk_index", name="uq_workflow_ingestion_chunk_index"),
        UniqueConstraint("ingestion_id", "chunk_id", name="uq_workflow_ingestion_chunk_identity"),
        Index("ix_workflow_ingestion_chunks_order", "ingestion_id", "chunk_index"),
    )

    id = Column(String(36), primary_key=True)
    ingestion_id = Column(String(36), ForeignKey("workflow_ingestions.id", ondelete="CASCADE"), nullable=False)
    chunk_id = Column(String(36), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    byte_offset = Column(BigInteger, nullable=False)
    size_bytes = Column(Integer, nullable=False)
    checksum_sha256 = Column(String(64), nullable=False)
    object_uri = Column(String(2048), nullable=False)
    start_seconds = Column(Integer, nullable=True)
    end_seconds = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


@dataclass(frozen=True)
class InputContract:
    """Minimum verified contiguous input required by a downstream operation."""

    min_bytes: int = 1
    min_duration_seconds: int = 0
    require_complete_source: bool = False

    def __post_init__(self):
        if self.min_bytes < 1 or self.min_duration_seconds < 0:
            raise ValueError("input contract bounds are invalid")


class IncrementalIngestionService:
    """PostgreSQL-backed chunk ledger with immutable S3 provisional/final objects."""

    def __init__(
        self, database_url=None, artifact_store=None, engine=None,
        max_chunk_size_bytes=8 * 1024 * 1024,
        max_concurrent_uploads=4, upload_wait_seconds=0.05,
    ):
        if artifact_store is None:
            raise ValueError("artifact_store is required")
        if engine is None and not database_url:
            raise ValueError("database_url or engine is required")
        from sqlalchemy import create_engine
        self._engine = engine or create_engine(database_url, future=True, pool_pre_ping=True)
        self._sessions = sessionmaker(bind=self._engine, expire_on_commit=False, future=True)
        self._store = artifact_store
        self._max_chunk_size = int(max_chunk_size_bytes)
        self._upload_slots = threading.BoundedSemaphore(int(max_concurrent_uploads))
        self._upload_wait = float(upload_wait_seconds)
        if self._max_chunk_size < 1 or max_concurrent_uploads < 1 or self._upload_wait < 0:
            raise ValueError("ingestion resource bounds must be positive")

    def create(self, job_id, source_name, chunk_size_bytes, expected_size_bytes=None,
               expected_sha256=None, max_pending_chunks=4, ingestion_id=None):
        if not isinstance(source_name, str) or not _NAME.fullmatch(source_name.strip()):
            raise ValueError("source_name must be a plain bounded filename")
        if not isinstance(chunk_size_bytes, int) or not 1 <= chunk_size_bytes <= self._max_chunk_size:
            raise ValueError("chunk_size_bytes exceeds the configured bound")
        if expected_size_bytes is not None and (
            not isinstance(expected_size_bytes, int) or expected_size_bytes < 1
        ):
            raise ValueError("expected_size_bytes must be positive")
        if expected_sha256 is not None and (
            not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256)
        ):
            raise ValueError("expected_sha256 must be lowercase SHA-256")
        if not isinstance(max_pending_chunks, int) or not 1 <= max_pending_chunks <= 1024:
            raise ValueError("max_pending_chunks is outside the supported bound")
        identifier = ingestion_id or new_id()
        with self._sessions.begin() as session:
            if session.get(JobRecord, job_id) is None:
                raise ValueError("job_id does not identify a durable Job")
            row = IngestionSessionRecord(
                id=identifier, job_id=job_id, source_name=source_name.strip(),
                chunk_size_bytes=chunk_size_bytes, max_pending_chunks=max_pending_chunks,
                expected_size_bytes=expected_size_bytes, expected_sha256=expected_sha256,
                state="OPEN", created_at=utcnow(), updated_at=utcnow(),
            )
            session.add(row)
        return self.get(identifier)

    def get(self, ingestion_id):
        with self._sessions() as session:
            row = session.get(IngestionSessionRecord, ingestion_id)
            if row is None:
                raise KeyError("ingestion not found")
            chunks = session.query(IngestionChunkRecord).filter_by(
                ingestion_id=ingestion_id
            ).order_by(IngestionChunkRecord.chunk_index).all()
            return self._snapshot(row, chunks)

    @staticmethod
    def _snapshot(row, chunks):
        return {
            "id": row.id, "job_id": row.job_id, "source_name": row.source_name,
            "state": row.state, "chunk_size_bytes": row.chunk_size_bytes,
            "expected_size_bytes": row.expected_size_bytes,
            "expected_sha256": row.expected_sha256,
            "chunks": [{
                "chunk_id": c.chunk_id, "index": c.chunk_index,
                "offset": c.byte_offset, "size_bytes": c.size_bytes,
                "checksum_sha256": c.checksum_sha256,
                "start_seconds": c.start_seconds, "end_seconds": c.end_seconds,
                "status": "PROVISIONAL",
            } for c in chunks],
            "contiguous_bytes": IncrementalIngestionService._contiguous_bytes(chunks),
            "final_artifact_uri": row.final_artifact_uri,
            "final_size_bytes": row.final_size_bytes,
            "final_sha256": row.final_sha256,
            "cleanup_pending": row.cleanup_pending,
        }

    @staticmethod
    def _contiguous_bytes(chunks):
        total = 0
        for expected, chunk in enumerate(chunks):
            if chunk.chunk_index != expected or chunk.byte_offset != total:
                break
            total += chunk.size_bytes
        return total

    def accept_chunk(self, ingestion_id, chunk_id, chunk_index, byte_offset,
                     payload, checksum_sha256, start_seconds=None, end_seconds=None):
        try:
            chunk_id = str(uuid.UUID(str(chunk_id)))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("chunk_id must be a UUID")
        if not isinstance(chunk_index, int) or chunk_index < 0:
            raise ValueError("chunk_index must be a non-negative integer")
        if not isinstance(byte_offset, int) or byte_offset < 0:
            raise ValueError("byte_offset must be non-negative")
        if not isinstance(payload, bytes) or not payload or len(payload) > self._max_chunk_size:
            raise ValueError("payload must be non-empty bytes within the configured chunk limit")
        actual = hashlib.sha256(payload).hexdigest()
        if not isinstance(checksum_sha256, str) or not _SHA256.fullmatch(checksum_sha256):
            raise ValueError("checksum_sha256 must be lowercase SHA-256")
        if actual != checksum_sha256:
            raise IngestionConflict("chunk checksum mismatch")
        if (start_seconds is None) != (end_seconds is None):
            raise ValueError("both time-range endpoints are required")
        if start_seconds is not None and (
            not isinstance(start_seconds, int) or not isinstance(end_seconds, int)
            or start_seconds < 0 or end_seconds <= start_seconds
        ):
            raise ValueError("chunk time range is invalid")

        if not self._upload_slots.acquire(timeout=self._upload_wait):
            raise IngestionBackpressure("concurrent chunk upload capacity is exhausted")
        try:
            with self._sessions() as session:
                row = session.get(IngestionSessionRecord, ingestion_id)
                if row is None:
                    raise KeyError("ingestion not found")
                if row.state not in ("OPEN", "FINALIZING"):
                    raise IngestionCancelled("ingestion does not accept chunks")
                if byte_offset != chunk_index * row.chunk_size_bytes:
                    raise IngestionConflict("chunk offset does not match its stable index")
                if len(payload) > row.chunk_size_bytes:
                    raise IngestionConflict("chunk exceeds the session chunk size")
                if row.expected_size_bytes is not None:
                    if byte_offset + len(payload) > row.expected_size_bytes:
                        raise IngestionConflict("chunk extends beyond declared source size")
                    if byte_offset + len(payload) < row.expected_size_bytes and len(payload) != row.chunk_size_bytes:
                        raise IngestionConflict("non-final chunks must have the declared chunk size")
                existing = session.query(IngestionChunkRecord).filter_by(
                    ingestion_id=ingestion_id, chunk_index=chunk_index
                ).one_or_none()
                if existing:
                    same = (
                        existing.chunk_id == chunk_id and existing.byte_offset == byte_offset
                        and existing.size_bytes == len(payload)
                        and existing.checksum_sha256 == actual
                        and existing.start_seconds == start_seconds
                        and existing.end_seconds == end_seconds
                    )
                    if same:
                        return {"accepted": True, "replayed": True, "chunk_id": chunk_id}
                    raise IngestionConflict("chunk index was already committed with different identity or content")
                ordered = session.query(IngestionChunkRecord).filter_by(
                    ingestion_id=ingestion_id
                ).order_by(IngestionChunkRecord.chunk_index).all()
                contiguous = 0
                for item in ordered:
                    if item.chunk_index != contiguous:
                        break
                    contiguous += 1
                if chunk_index >= contiguous + row.max_pending_chunks:
                    raise IngestionBackpressure("chunk is outside the bounded reorder window")
                if row.state == "FINALIZING":
                    raise IngestionConflict("finalization has already started")
                chunk_size = row.chunk_size_bytes

            logical_key = "ingestions/{}/chunks/{:08d}".format(ingestion_id, chunk_index)
            stored = self._store.put_bytes(
                payload, logical_key, chunk_id, media_type="application/octet-stream"
            )
            with self._sessions.begin() as session:
                row = session.query(IngestionSessionRecord).filter_by(
                    id=ingestion_id
                ).with_for_update().one()
                if row.state != "OPEN":
                    raise IngestionCancelled("ingestion stopped while chunk was uploading")
                existing = session.query(IngestionChunkRecord).filter_by(
                    ingestion_id=ingestion_id, chunk_index=chunk_index
                ).one_or_none()
                if existing:
                    same = (
                        existing.chunk_id == chunk_id and existing.checksum_sha256 == actual
                        and existing.byte_offset == byte_offset and existing.size_bytes == len(payload)
                    )
                    if same:
                        return {"accepted": True, "replayed": True, "chunk_id": chunk_id}
                    raise IngestionConflict("concurrent chunk writer committed a conflicting chunk")
                ordered = session.query(IngestionChunkRecord).filter_by(
                    ingestion_id=ingestion_id
                ).order_by(IngestionChunkRecord.chunk_index).all()
                contiguous = 0
                for item in ordered:
                    if item.chunk_index != contiguous:
                        break
                    contiguous += 1
                if chunk_index >= contiguous + row.max_pending_chunks:
                    raise IngestionBackpressure("chunk is outside the bounded reorder window")
                session.add(IngestionChunkRecord(
                    id=new_id(), ingestion_id=ingestion_id, chunk_id=chunk_id,
                    chunk_index=chunk_index, byte_offset=byte_offset,
                    size_bytes=len(payload), checksum_sha256=actual,
                    object_uri=stored["uri"], start_seconds=start_seconds,
                    end_seconds=end_seconds, created_at=utcnow(),
                ))
                row.updated_at = utcnow()
            return {"accepted": True, "replayed": False, "chunk_id": chunk_id}
        finally:
            self._upload_slots.release()

    def progressive_view(self, ingestion_id):
        snapshot = self.get(ingestion_id)
        chunks = snapshot["chunks"]
        contiguous = []
        expected_index = 0
        expected_offset = 0
        end_seconds = None
        for chunk in chunks:
            if chunk["index"] != expected_index or chunk["offset"] != expected_offset:
                break
            if chunk["start_seconds"] is not None:
                if end_seconds is not None and chunk["start_seconds"] != end_seconds:
                    break
                end_seconds = chunk["end_seconds"]
            elif end_seconds is not None:
                break
            contiguous.append(chunk)
            expected_index += 1
            expected_offset += chunk["size_bytes"]
        return {
            "status": "PROVISIONAL" if contiguous else "UNAVAILABLE",
            "chunks": contiguous,
            "contiguous_bytes": expected_offset,
            "duration_seconds": end_seconds,
            "final_artifact_uri": None,
        }

    def input_contract_satisfied(self, ingestion_id, contract):
        if not isinstance(contract, InputContract):
            raise TypeError("contract must be an InputContract")
        view = self.progressive_view(ingestion_id)
        snapshot = self.get(ingestion_id)
        if contract.require_complete_source and snapshot["state"] != "COMPLETE":
            return False
        if view["contiguous_bytes"] < contract.min_bytes:
            return False
        if contract.min_duration_seconds:
            if view["duration_seconds"] is None or view["duration_seconds"] < contract.min_duration_seconds:
                return False
        return True

    def finalize(self, ingestion_id):
        with self._sessions.begin() as session:
            row = session.query(IngestionSessionRecord).filter_by(
                id=ingestion_id
            ).with_for_update().one()
            if row.state == "COMPLETE":
                return self.get(ingestion_id)
            if row.state not in ("OPEN", "FINALIZING"):
                raise IngestionCancelled("ingestion cannot be finalized")
            if row.expected_size_bytes is None:
                raise ValueError("expected_size_bytes is required before finalization")
            chunks = session.query(IngestionChunkRecord).filter_by(
                ingestion_id=ingestion_id
            ).order_by(IngestionChunkRecord.chunk_index).all()
            expected_count = (row.expected_size_bytes + row.chunk_size_bytes - 1) // row.chunk_size_bytes
            if len(chunks) != expected_count:
                raise IncompleteIngestion("one or more source chunks are missing")
            offset = 0
            for index, chunk in enumerate(chunks):
                if chunk.chunk_index != index or chunk.byte_offset != offset:
                    raise IncompleteIngestion("source chunks contain an ordering gap")
                expected_chunk_size = min(row.chunk_size_bytes, row.expected_size_bytes - offset)
                if chunk.size_bytes != expected_chunk_size:
                    raise IncompleteIngestion("source chunk size does not match its declared range")
                offset += chunk.size_bytes
            if offset != row.expected_size_bytes:
                raise IncompleteIngestion("source byte ranges do not cover the declared source")
            row.state = "FINALIZING"
            row.updated_at = utcnow()
            source_name = row.source_name
            expected_size = row.expected_size_bytes
            expected_hash = row.expected_sha256
            chunk_rows = [{
                "uri": c.object_uri, "sha256": c.checksum_sha256,
                "size": c.size_bytes,
            } for c in chunks]

        with tempfile.TemporaryDirectory(prefix="video-ingestion-") as directory:
            assembled = os.path.join(directory, "source")
            digest = hashlib.sha256()
            total = 0
            for index, chunk in enumerate(chunk_rows):
                path = os.path.join(directory, "chunk-{:08d}".format(index))
                self._store.download_file(chunk["uri"], path, expected_sha256=chunk["sha256"])
                with open(path, "rb") as source, open(assembled, "ab") as target:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        target.write(block)
                        digest.update(block)
                        total += len(block)
                os.unlink(path)
            final_hash = digest.hexdigest()
            if total != expected_size:
                raise IngestionConflict("assembled source size does not match the declared size")
            if expected_hash is not None and final_hash != expected_hash:
                raise IngestionConflict("assembled source checksum mismatch")
            published = self._store.put_file(
                assembled, "ingestions/{}/source/{}".format(ingestion_id, source_name),
                ingestion_id, media_type="application/octet-stream",
            )
            readback = self._store.head(published["uri"])
            if readback["size_bytes"] != total or readback["checksum_sha256"] != final_hash:
                raise IngestionConflict("final source readback validation failed")

        with self._sessions.begin() as session:
            row = session.query(IngestionSessionRecord).filter_by(
                id=ingestion_id
            ).with_for_update().one()
            if row.state != "FINALIZING":
                raise IngestionCancelled("ingestion state changed during finalization")
            row.state = "COMPLETE"
            row.final_artifact_uri = published["uri"]
            row.final_size_bytes = total
            row.final_sha256 = final_hash
            row.updated_at = utcnow()
        return self.get(ingestion_id)

    def cancel(self, ingestion_id):
        with self._sessions.begin() as session:
            row = session.query(IngestionSessionRecord).filter_by(
                id=ingestion_id
            ).with_for_update().one()
            if row.state == "COMPLETE":
                raise IngestionConflict("a completed source cannot be cancelled")
            if row.state == "CANCELLED":
                pass
            else:
                row.state = "CANCELLED"
                row.cleanup_pending = True
                row.updated_at = utcnow()
        self.cleanup_cancelled(ingestion_id)
        return self.get(ingestion_id)

    def cleanup_cancelled(self, ingestion_id):
        with self._sessions() as session:
            row = session.get(IngestionSessionRecord, ingestion_id)
            if row is None:
                raise KeyError("ingestion not found")
            if row.state != "CANCELLED":
                raise IngestionConflict("only cancelled ingestion chunks may be cleaned")
            uris = [c.object_uri for c in session.query(IngestionChunkRecord).filter_by(
                ingestion_id=ingestion_id
            ).all()]
        failed = False
        for uri in uris:
            bucket, key = parse_artifact_uri(uri)
            try:
                self._store.client.delete_object(Bucket=bucket, Key=key)
            except Exception:
                failed = True
        with self._sessions.begin() as session:
            row = session.query(IngestionSessionRecord).filter_by(
                id=ingestion_id
            ).with_for_update().one()
            row.cleanup_pending = failed
            row.updated_at = utcnow()
        return not failed

    def close(self):
        self._engine.dispose()
