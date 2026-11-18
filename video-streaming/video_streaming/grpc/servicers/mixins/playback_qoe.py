"""gRPC adapter for durable playback QoE session telemetry."""

import grpc

from video_streaming.workflows.service import get_workflow_repository


class PlaybackQoEMixin:

    def _playback_repository(self, context):
        repository = get_workflow_repository(required=False)
        if repository is None:
            context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "Durable playback telemetry storage is not configured",
            )
        return repository

    def start_playback_session(self, request, context):
        repository = self._playback_repository(context)
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
        repository = self._playback_repository(context)
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
        repository = self._playback_repository(context)
        try:
            report = repository.get_playback_qoe_report(
                job_id=request.tracking_id,
                stage_key=request.output_id,
            )
        except KeyError as exc:
            context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
        return self.pb2.PlaybackQoEReportResponse(
            tracking_id=report["job_id"],
            output_id=report["stage_key"],
            stage_status=report["stage_status"],
            session_count=report["session_count"],
            playback_started_count=report["playback_started_count"],
            average_startup_ms=int(round(report["average_startup_ms"] or 0)),
            rebuffer_count=report["rebuffer_count"],
            total_buffering_ms=report["total_buffering_ms"],
            decoder_error_count=report["decoder_error_count"],
            buffering_ratio_percent=(
                report["buffering_ratio_percent"] or 0.0
            ),
            total_cpu_seconds=report["total_cpu_seconds"],
            total_estimated_cost=(
                report["total_estimated_cost"] or 0.0
            ),
            cost_currency=report["cost_currency"] or "",
            pricing_model_id=report["pricing_model_id"] or "",
            cost_estimate_complete=report["cost_estimate_complete"],
            priced_attempt_count=report["priced_attempt_count"],
            total_attempt_count=report["total_attempt_count"],
        )
