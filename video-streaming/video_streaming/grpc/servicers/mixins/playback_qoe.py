"""gRPC adapter for durable playback QoE session telemetry."""

import grpc

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
