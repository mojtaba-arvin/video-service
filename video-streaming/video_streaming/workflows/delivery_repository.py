"""Durable, provider-neutral CDN delivery telemetry persistence and queries.

Adapters normalize provider records into DeliveryEvent before this boundary.
Raw logs, request URLs, headers, IP addresses and viewer identifiers are never
accepted or persisted here.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    BigInteger, CheckConstraint, Column, DateTime, ForeignKey, Index,
    Integer, String, UniqueConstraint, delete, select,
)
from sqlalchemy.exc import IntegrityError

from .delivery import DeliveryEvent
from .repository import (
    ArtifactRecord, AttemptRecord, Base, PlaybackEventRecord,
    PlaybackSessionRecord, StageRecord, WorkflowRecord, utcnow,
)


DEFAULT_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 90
MAX_QUERY_LIMIT = 1000
MIN_WINDOW_SECONDS = 60
MAX_WINDOW_SECONDS = 86400


class DeliveryEventConflictError(ValueError):
    """A source event identity was replayed with different normalized data."""


class DeliveryEventRecord(Base):
    """Allow-listed delivery evidence tied to durable workflow identities."""

    __tablename__ = "cdn_delivery_events"
    __table_args__ = (
        UniqueConstraint(
            "source", "source_event_id",
            name="uq_cdn_delivery_source_event",
        ),
        CheckConstraint(
            "schema_version = 1",
            name="ck_cdn_delivery_schema_version",
        ),
        CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="ck_cdn_delivery_http_status",
        ),
        CheckConstraint(
            "response_time_ms IS NULL OR response_time_ms BETWEEN 0 AND 86400000",
            name="ck_cdn_delivery_response_time",
        ),
        CheckConstraint(
            "bytes_sent IS NULL OR bytes_sent >= 0",
            name="ck_cdn_delivery_bytes_sent",
        ),
        CheckConstraint(
            "expires_at > ingested_at",
            name="ck_cdn_delivery_expiry",
        ),
        Index(
            "ix_cdn_delivery_job_source_time",
            "job_id", "source", "source_timestamp",
        ),
        Index(
            "ix_cdn_delivery_stage_time",
            "stage_id", "source_timestamp",
        ),
        Index(
            "ix_cdn_delivery_session_time",
            "session_id", "source_timestamp",
        ),
        Index("ix_cdn_delivery_expiry", "expires_at"),
    )

    id = Column(String(36), primary_key=True)
    source = Column(String(64), nullable=False)
    source_event_id = Column(String(128), nullable=False)
    schema_version = Column(Integer, nullable=False, default=1)
    payload_sha256 = Column(String(64), nullable=False)
    source_timestamp = Column(DateTime(timezone=True), nullable=False)
    ingested_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)

    job_id = Column(
        String(36), ForeignKey("workflow_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_id = Column(
        String(36), ForeignKey("workflow_workflows.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage_id = Column(
        String(36), ForeignKey("workflow_stages.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_id = Column(
        String(36), ForeignKey("workflow_attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    artifact_id = Column(
        String(36), ForeignKey("workflow_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    session_id = Column(
        String(36), ForeignKey("playback_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )

    asset_version = Column(String(128), nullable=True)
    delivery_config_id = Column(String(128), nullable=True)
    edge_region = Column(String(64), nullable=True)
    cache_status = Column(String(16), nullable=False)
    http_status = Column(Integer, nullable=True)
    response_time_ms = Column(BigInteger, nullable=True)
    bytes_sent = Column(BigInteger, nullable=True)


def _aware_utc(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("{} must be timezone-aware".format(name))
    return value.astimezone(timezone.utc)


def _optional_filter(value, name, maximum):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > maximum or value.strip() != value:
        raise ValueError("{} filter is invalid".format(name))
    return value


def _iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class DeliveryTelemetryRepository:
    """Persistence adapter over the workflow repository's authoritative DB."""

    def __init__(self, workflow_repository, retention_days=DEFAULT_RETENTION_DAYS):
        if (
            isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or not 1 <= retention_days <= MAX_RETENTION_DAYS
        ):
            raise ValueError("retention_days must be between 1 and 90")
        self.workflow_repository = workflow_repository
        self._session = workflow_repository._session
        self.retention_days = retention_days

    @staticmethod
    def _event_payload(record):
        return {
            "source": record.source,
            "source_event_id": record.source_event_id,
            "schema_version": record.schema_version,
            "source_timestamp": _iso(record.source_timestamp),
            "ingested_at": _iso(record.ingested_at),
            "job_id": record.job_id,
            "workflow_id": record.workflow_id,
            "stage_id": record.stage_id,
            "attempt_id": record.attempt_id,
            "artifact_id": record.artifact_id,
            "manifest_version": record.artifact_id,
            "session_id": record.session_id,
            "asset_version": record.asset_version,
            "delivery_config_id": record.delivery_config_id,
            "edge_region": record.edge_region,
            "cache_status": record.cache_status,
            "http_status": record.http_status,
            "response_time_ms": record.response_time_ms,
            "bytes_sent": record.bytes_sent,
        }

    def ingest(self, event):
        """Persist one normalized event; exact replays are no-ops.

        The Artifact is authoritative for Job/Workflow/Stage/Attempt linkage.
        A supplied session must point to that same immutable manifest.
        """
        if not isinstance(event, DeliveryEvent):
            raise TypeError("event must be a normalized DeliveryEvent")
        now = utcnow()
        try:
            with self._session.begin() as session:
                artifact = session.get(ArtifactRecord, event.artifact_id)
                if (
                        artifact is None
                        or artifact.artifact_type != "streaming_manifest"
                        or artifact.status != "AVAILABLE"
                        or not artifact.uri
                        or artifact.stage_id is None):
                    raise KeyError("Published manifest Artifact was not found")

                stage = session.get(StageRecord, artifact.stage_id)
                if stage is None or stage.status != "FINISHED":
                    raise ValueError(
                        "Delivery telemetry requires a finished published Stage"
                    )
                workflow = session.get(WorkflowRecord, stage.workflow_id)
                if workflow is None:
                    raise KeyError("Manifest Workflow was not found")

                if event.session_id is not None:
                    playback = session.get(PlaybackSessionRecord, event.session_id)
                    if (
                            playback is None
                            or playback.manifest_artifact_id != artifact.id
                            or playback.stage_id != stage.id
                            or playback.job_id != workflow.job_id):
                        raise ValueError(
                            "Playback session does not reference the same published manifest"
                        )

                existing = session.execute(
                    select(DeliveryEventRecord).where(
                        DeliveryEventRecord.source == event.source,
                        DeliveryEventRecord.source_event_id == event.source_event_id,
                    ).with_for_update()
                ).scalar_one_or_none()
                if existing is not None:
                    if existing.payload_sha256 != event.fingerprint():
                        raise DeliveryEventConflictError(
                            "source event identity was reused with a different payload"
                        )
                    return {
                        "accepted": True, "duplicate": True,
                        "event_id": existing.id,
                    }

                record = DeliveryEventRecord(
                    id=self._new_id(),
                    source=event.source,
                    source_event_id=event.source_event_id,
                    schema_version=event.schema_version,
                    payload_sha256=event.fingerprint(),
                    source_timestamp=event.source_timestamp,
                    ingested_at=now,
                    expires_at=now + timedelta(days=self.retention_days),
                    job_id=workflow.job_id,
                    workflow_id=workflow.id,
                    stage_id=stage.id,
                    attempt_id=artifact.attempt_id,
                    artifact_id=artifact.id,
                    session_id=event.session_id,
                    asset_version=event.asset_version,
                    delivery_config_id=event.delivery_config_id,
                    edge_region=event.edge_region,
                    cache_status=event.cache_status,
                    http_status=event.http_status,
                    response_time_ms=event.response_time_ms,
                    bytes_sent=event.bytes_sent,
                )
                session.add(record)
                session.flush()
                return {
                    "accepted": True, "duplicate": False,
                    "event_id": record.id,
                }
        except IntegrityError:
            # Concurrent at-least-once deliveries race on the durable unique key.
            with self._session() as retry_session:
                winner = retry_session.execute(
                    select(DeliveryEventRecord).where(
                        DeliveryEventRecord.source == event.source,
                        DeliveryEventRecord.source_event_id == event.source_event_id,
                    )
                ).scalar_one_or_none()
                if winner is None:
                    raise
                if winner.payload_sha256 != event.fingerprint():
                    raise DeliveryEventConflictError(
                        "source event identity was reused with a different payload"
                    )
                return {
                    "accepted": True, "duplicate": True,
                    "event_id": winner.id,
                }

    @staticmethod
    def _new_id():
        import uuid
        return str(uuid.uuid4())

    def query(
        self,
        job_id,
        stage_key=None,
        session_id=None,
        artifact_id=None,
        source=None,
        edge_region=None,
        cache_status=None,
        asset_version=None,
        delivery_config_id=None,
        start_time=None,
        end_time=None,
        aggregation_window_seconds=300,
        limit=500,
    ):
        """Return observed events, derived time buckets and explicit coverage."""
        if not isinstance(job_id, str) or not job_id or len(job_id) > 36:
            raise ValueError("job_id is invalid")
        stage_key = _optional_filter(stage_key, "stage_key", 128)
        session_id = _optional_filter(session_id, "session_id", 36)
        artifact_id = _optional_filter(artifact_id, "artifact_id", 36)
        source = _optional_filter(source, "source", 64)
        edge_region = _optional_filter(edge_region, "edge_region", 64)
        cache_status = _optional_filter(cache_status, "cache_status", 16)
        asset_version = _optional_filter(asset_version, "asset_version", 128)
        delivery_config_id = _optional_filter(delivery_config_id, "delivery_config_id", 128)
        if cache_status is not None and cache_status not in {
            "HIT", "MISS", "BYPASS", "REVALIDATED", "STALE", "UNKNOWN",
        }:
            raise ValueError("cache_status filter is invalid")
        if (
            isinstance(limit, bool) or not isinstance(limit, int)
            or not 1 <= limit <= MAX_QUERY_LIMIT
        ):
            raise ValueError("limit must be between 1 and 1000")
        if (
            isinstance(aggregation_window_seconds, bool)
            or not isinstance(aggregation_window_seconds, int)
            or not MIN_WINDOW_SECONDS <= aggregation_window_seconds <= MAX_WINDOW_SECONDS
        ):
            raise ValueError("aggregation window must be between 60 and 86400 seconds")
        start_time = _aware_utc(start_time, "start_time") if start_time else None
        end_time = _aware_utc(end_time, "end_time") if end_time else None
        if start_time and end_time and start_time >= end_time:
            raise ValueError("start_time must be earlier than end_time")

        with self._session() as session:
            query = select(DeliveryEventRecord).where(
                DeliveryEventRecord.job_id == job_id
            )
            if stage_key:
                query = query.join(
                    StageRecord, DeliveryEventRecord.stage_id == StageRecord.id
                ).where(StageRecord.stage_key == stage_key)
            if session_id:
                query = query.where(DeliveryEventRecord.session_id == session_id)
            if artifact_id:
                query = query.where(DeliveryEventRecord.artifact_id == artifact_id)
            if source:
                query = query.where(DeliveryEventRecord.source == source)
            if edge_region:
                query = query.where(DeliveryEventRecord.edge_region == edge_region)
            if cache_status:
                query = query.where(DeliveryEventRecord.cache_status == cache_status)
            if asset_version:
                query = query.where(DeliveryEventRecord.asset_version == asset_version)
            if delivery_config_id:
                query = query.where(DeliveryEventRecord.delivery_config_id == delivery_config_id)
            if start_time:
                query = query.where(DeliveryEventRecord.source_timestamp >= start_time)
            if end_time:
                query = query.where(DeliveryEventRecord.source_timestamp < end_time)
            records = session.execute(
                query.order_by(DeliveryEventRecord.source_timestamp)
                .limit(limit + 1)
            ).scalars().all()
            truncated = len(records) > limit
            records = records[:limit]

            # Coverage is scoped to the requested Job/Stage and time range,
            # independent of whether a CDN event happened to arrive.
            sessions_query = select(PlaybackSessionRecord).where(
                PlaybackSessionRecord.job_id == job_id
            )
            if session_id:
                sessions_query = sessions_query.where(PlaybackSessionRecord.id == session_id)
            if stage_key:
                sessions_query = sessions_query.join(
                    StageRecord, PlaybackSessionRecord.stage_id == StageRecord.id
                ).where(StageRecord.stage_key == stage_key)
            if start_time:
                sessions_query = sessions_query.where(
                    PlaybackSessionRecord.started_at >= start_time
                )
            if end_time:
                sessions_query = sessions_query.where(
                    PlaybackSessionRecord.started_at < end_time
                )
            playback_sessions = session.execute(sessions_query).scalars().all()
            session_ids = [item.id for item in playback_sessions]
            player_signal_ids = set()
            if session_ids:
                player_signal_ids = set(session.execute(
                    select(PlaybackEventRecord.session_id).where(
                        PlaybackEventRecord.session_id.in_(session_ids)
                    ).distinct()
                ).scalars().all())

            event_session_ids = {
                item.session_id for item in records if item.session_id is not None
            }
            buckets = {}
            window = aggregation_window_seconds
            for item in records:
                timestamp = item.source_timestamp
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                epoch = int(timestamp.timestamp())
                bucket_epoch = (epoch // window) * window
                key = (bucket_epoch, item.edge_region or "", item.cache_status)
                bucket = buckets.setdefault(key, {
                    "window_start": datetime.fromtimestamp(
                        bucket_epoch, timezone.utc
                    ),
                    "window_end": datetime.fromtimestamp(
                        bucket_epoch + window, timezone.utc
                    ),
                    "edge_region": item.edge_region,
                    "cache_status": item.cache_status,
                    "event_count": 0,
                    "response_time_sum_ms": 0,
                    "response_time_count": 0,
                    "bytes_sum": 0,
                    "bytes_count": 0,
                    "http_error_count": 0,
                })
                bucket["event_count"] += 1
                if item.response_time_ms is not None:
                    bucket["response_time_sum_ms"] += item.response_time_ms
                    bucket["response_time_count"] += 1
                if item.bytes_sent is not None:
                    bucket["bytes_sum"] += item.bytes_sent
                    bucket["bytes_count"] += 1
                if item.http_status is not None and item.http_status >= 400:
                    bucket["http_error_count"] += 1

            derived = []
            for item in sorted(buckets.values(), key=lambda value: value["window_start"]):
                derived.append({
                    "window_start": _iso(item["window_start"]),
                    "window_end": _iso(item["window_end"]),
                    "edge_region": item["edge_region"],
                    "cache_status": item["cache_status"],
                    "event_count": item["event_count"],
                    "average_response_time_ms": (
                        item["response_time_sum_ms"] / item["response_time_count"]
                        if item["response_time_count"] else None
                    ),
                    "response_time_observation_count": item["response_time_count"],
                    "bytes_sent": item["bytes_sum"] if item["bytes_count"] else None,
                    "bytes_observation_count": item["bytes_count"],
                    "http_error_count": item["http_error_count"],
                })

            return {
                "job_id": job_id,
                "stage_key": stage_key,
                "aggregation_window_seconds": window,
                "observed_events": [self._event_payload(item) for item in records],
                "derived_aggregates": derived,
                "coverage": {
                    "playback_session_count": len(playback_sessions),
                    "sessions_with_player_signals": sum(
                        1 for item in session_ids if item in player_signal_ids
                    ),
                    "sessions_without_player_signals": sum(
                        1 for item in session_ids if item not in player_signal_ids
                    ),
                    "sessions_with_delivery_signals": sum(
                        1 for item in session_ids if item in event_session_ids
                    ),
                    "sessions_without_delivery_signals": sum(
                        1 for item in session_ids if item not in event_session_ids
                    ),
                    "delivery_event_count": len(records),
                    "delivery_events_with_session": sum(
                        1 for item in records if item.session_id is not None
                    ),
                    "delivery_events_without_session": sum(
                        1 for item in records if item.session_id is None
                    ),
                    "missing_asset_version_count": sum(
                        1 for item in records if item.asset_version is None
                    ),
                    "missing_response_time_count": sum(
                        1 for item in records if item.response_time_ms is None
                    ),
                    "missing_bytes_count": sum(
                        1 for item in records if item.bytes_sent is None
                    ),
                    "unknown_cache_outcome_count": sum(
                        1 for item in records if item.cache_status == "UNKNOWN"
                    ),
                    "truncated": truncated,
                },
            }

    def purge_expired(self, now=None, limit=500):
        """Delete at most limit expired rows; repeat calls safely drain backlog."""
        now = _aware_utc(now, "now") if now is not None else utcnow()
        if (
            isinstance(limit, bool) or not isinstance(limit, int)
            or not 1 <= limit <= 5000
        ):
            raise ValueError("limit must be between 1 and 5000")
        with self._session.begin() as session:
            ids = session.execute(
                select(DeliveryEventRecord.id)
                .where(DeliveryEventRecord.expires_at <= now)
                .order_by(DeliveryEventRecord.expires_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).scalars().all()
            if not ids:
                return 0
            result = session.execute(
                delete(DeliveryEventRecord).where(DeliveryEventRecord.id.in_(ids))
            )
            return result.rowcount or 0
