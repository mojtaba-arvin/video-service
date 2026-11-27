"""gRPC adapters for playback QoE and correlated CDN delivery telemetry."""

from datetime import datetime

import grpc

from video_streaming.workflows.delivery_repository import DeliveryTelemetryRepository

from video_streaming.workflows.qoe import playback_qoe_report_payload
from video_streaming.grpc.servicers.mixins.job_authorization import (
    require_job_access,
    require_playback_session_access,
)


class PlaybackQoEMixin:

    def start_playback_session(self, request, context):
        repository = require_job_access(
            request.tracking_id, "video.playback:write", context
        )
        try:
            result = repository.start_playback_session(
                job_id=request.tracking_id,
                stage_key=request.output_id,
                client_version=request.client_version,
            )
        except KeyError as exc:
            context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
        except ValueError as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))

        return self.pb2.StartPlaybackSessionResponse(
            session_id=result["session_id"],
            session_token=result["session_token"],
            manifest_artifact_id=result["manifest_artifact_id"],
        )

    def record_playback_event(self, request, context):
        repository = require_playback_session_access(
            request.session_id, "video.playback:write", context
        )
        try:
            event_type = self.pb2.PlaybackEventType.Name(
                request.event_type
            )
        except ValueError:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "Unsupported playback event type",
            )

        if event_type == "PLAYBACK_EVENT_UNSPECIFIED":
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "A playback event type is required",
            )

        try:
            result = repository.record_playback_event(
                session_id=request.session_id,
                session_token=request.session_token,
                event_id=request.event_id,
                event_type=event_type,
                startup_ms=(
                    request.startup_ms
                    if event_type == "PLAYBACK_STARTED" else None
                ),
                error_code=request.error_code,
                decoder=request.decoder,
                client_version=request.client_version,
            )
        except KeyError as exc:
            context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
        except PermissionError as exc:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, str(exc))
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))

        return self.pb2.RecordPlaybackEventResponse(
            accepted=True,
            duplicate=result["duplicate"],
            rebuffer_count=result["rebuffer_count"],
            total_buffering_ms=result["total_buffering_ms"],
            decoder_error_count=result["decoder_error_count"],
            ended=result["status"] == "ENDED",
        )

    def get_playback_qoe_report(self, request, context):
        repository = require_job_access(
            request.tracking_id, "video.qoe:read", context
        )
        try:
            report = repository.get_playback_qoe_report(
                job_id=request.tracking_id,
                stage_key=request.output_id,
            )
        except KeyError as exc:
            context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
        return self.pb2.PlaybackQoEReportResponse(
            **playback_qoe_report_payload(report)
        )

    @staticmethod
    def _delivery_query_time(value, field_name, context):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "{} must be an RFC3339 timestamp".format(field_name),
            )
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "{} must include a timezone".format(field_name),
            )
        return parsed

    def get_delivery_telemetry_report(self, request, context):
        repository = require_job_access(
            request.tracking_id, "video.qoe:read", context
        )
        window = request.aggregation_window_seconds or 300
        limit = request.limit or 500
        try:
            report = DeliveryTelemetryRepository(repository).query(
                job_id=request.tracking_id,
                stage_key=request.output_id or None,
                session_id=request.session_id or None,
                artifact_id=request.artifact_id or None,
                source=request.source or None,
                edge_region=request.edge_region or None,
                cache_status=request.cache_status or None,
                asset_version=request.asset_version or None,
                delivery_config_id=request.delivery_config_id or None,
                start_time=self._delivery_query_time(
                    request.start_time, "start_time", context
                ),
                end_time=self._delivery_query_time(
                    request.end_time, "end_time", context
                ),
                aggregation_window_seconds=window,
                limit=limit,
            )
        except KeyError as exc:
            context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))

        observed = [
            self.pb2.ObservedDeliveryEvent(
                source=item["source"],
                source_event_id=item["source_event_id"],
                source_timestamp=item["source_timestamp"],
                ingested_at=item["ingested_at"],
                artifact_id=item["artifact_id"],
                manifest_version=item["manifest_version"],
                asset_version=item["asset_version"] or "",
                delivery_config_id=item["delivery_config_id"] or "",
                session_id=item["session_id"] or "",
                job_id=item["job_id"],
                workflow_id=item["workflow_id"],
                stage_id=item["stage_id"],
                attempt_id=item["attempt_id"] or "",
                edge_region=item["edge_region"] or "",
                cache_status=item["cache_status"],
                http_status=item["http_status"] or 0,
                http_status_available=item["http_status"] is not None,
                response_time_ms=item["response_time_ms"] or 0,
                response_time_available=item["response_time_ms"] is not None,
                bytes_sent=item["bytes_sent"] or 0,
                bytes_sent_available=item["bytes_sent"] is not None,
            )
            for item in report["observed_events"]
        ]
        aggregates = [
            self.pb2.DerivedDeliveryAggregate(
                window_start=item["window_start"],
                window_end=item["window_end"],
                source=item["source"],
                artifact_id=item["artifact_id"],
                manifest_version=item["manifest_version"],
                asset_version=item["asset_version"] or "",
                delivery_config_id=item["delivery_config_id"] or "",
                edge_region=item["edge_region"] or "",
                cache_status=item["cache_status"],
                event_count=item["event_count"],
                average_response_time_ms=(
                    item["average_response_time_ms"] or 0.0
                ),
                response_time_observation_count=(
                    item["response_time_observation_count"]
                ),
                bytes_sent=item["bytes_sent"] or 0,
                bytes_observation_count=item["bytes_observation_count"],
                http_error_count=item["http_error_count"],
                bytes_sent_available=item["bytes_sent"] is not None,
                average_response_time_available=(
                    item["average_response_time_ms"] is not None
                ),
            )
            for item in report["derived_aggregates"]
        ]
        return self.pb2.DeliveryTelemetryReportResponse(
            tracking_id=report["job_id"],
            output_id=report["stage_key"] or "",
            aggregation_window_seconds=report["aggregation_window_seconds"],
            observed_events=observed,
            derived_aggregates=aggregates,
            coverage=self.pb2.DeliveryTelemetryCoverage(
                **report["coverage"]
            ),
        )
