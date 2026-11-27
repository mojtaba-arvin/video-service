"""gRPC revoke remains a thin compatibility adapter over durable cancellation."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from video_streaming.grpc.protos import streaming_pb2
from video_streaming.grpc.servicers.mixins.revoke_jobs import RevokeJobsMixin


class RevokeJobsCompatibilityTests(unittest.TestCase):
    def test_batch_is_authorized_before_durable_cancel_and_redis_fallback(self):
        order = []

        class FakeCancellationService:
            def __init__(self, _repository):
                pass

            def execute(self, job_id, _principal, reason):
                order.append(("cancel", job_id))
                self.assert_reason = reason
                return SimpleNamespace(
                    status="CANCELLED",
                    requested=True,
                )

        class Servicer(RevokeJobsMixin):
            pb2 = streaming_pb2

            def _revoke_job(self, _job_id):
                # The durable cancellation must still be reported when Redis
                # has no legacy JOB_DETAILS projection.
                return None

        request = streaming_pb2.RevokeJobsRequest(
            tracking_ids=["job-a", "job-b"]
        )
        with patch(
            "video_streaming.grpc.servicers.mixins.revoke_jobs.require_job_access",
            side_effect=lambda job_id, _scope, _context: order.append(
                ("authorize", job_id)
            ),
        ), patch(
            "video_streaming.grpc.servicers.mixins.revoke_jobs.get_workflow_repository",
            return_value=object(),
        ), patch(
            "video_streaming.grpc.servicers.mixins.revoke_jobs.get_request_principal",
            return_value=object(),
        ), patch(
            "video_streaming.grpc.servicers.mixins.revoke_jobs.CancelJobService",
            FakeCancellationService,
        ):
            response = Servicer()._revoke_jobs(request, SimpleNamespace())

        self.assertEqual(order, [
            ("authorize", "job-a"),
            ("authorize", "job-b"),
            ("cancel", "job-a"),
            ("cancel", "job-b"),
        ])
        self.assertEqual(len(response.results), 2)
        self.assertEqual(
            [item.has_been_sent for item in response.results],
            [
                streaming_pb2.RevokeSignalStatus.REVOKE_SIGNAL_SENT,
                streaming_pb2.RevokeSignalStatus.REVOKE_SIGNAL_SENT,
            ],
        )

    def test_terminal_success_is_not_rewritten_as_a_revoke(self):
        class FakeCancellationService:
            def __init__(self, _repository):
                pass

            def execute(self, _job_id, _principal, _reason):
                return SimpleNamespace(status="FINISHED", requested=False)

        class Servicer(RevokeJobsMixin):
            pb2 = streaming_pb2

            def _revoke_job(self, _job_id):
                raise AssertionError("terminal Job must not receive Redis revoke")

        request = streaming_pb2.RevokeJobsRequest(tracking_ids=["finished-job"])
        with patch(
            "video_streaming.grpc.servicers.mixins.revoke_jobs.require_job_access"
        ), patch(
            "video_streaming.grpc.servicers.mixins.revoke_jobs.get_workflow_repository",
            return_value=object(),
        ), patch(
            "video_streaming.grpc.servicers.mixins.revoke_jobs.get_request_principal",
            return_value=object(),
        ), patch(
            "video_streaming.grpc.servicers.mixins.revoke_jobs.CancelJobService",
            FakeCancellationService,
        ):
            response = Servicer()._revoke_jobs(request, SimpleNamespace())

        self.assertEqual(len(response.results), 1)
        self.assertEqual(
            response.results[0].has_been_sent,
            streaming_pb2.RevokeSignalStatus.REQUEST_NOT_FOUND,
        )


if __name__ == "__main__":
    unittest.main()
