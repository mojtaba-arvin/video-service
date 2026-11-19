"""Unit tests for process-local durable Attempt lease heartbeats."""

import unittest

from video_streaming.workflows.domain import AttemptLeaseLost
from video_streaming.workflows.lease import AttemptLeaseHeartbeat


class FakeRepository:
    @staticmethod
    def _validated_lease_ttl(value):
        if isinstance(value, bool) or float(value) <= 0:
            raise ValueError("invalid ttl")
        return float(value)

    def __init__(self, error=None):
        self.renewals = []
        self.error = error

    def renew_attempt_lease(
            self, job_id, stage_key, attempt_id, token,
            lease_ttl_seconds=60):
        self.renewals.append((
            job_id, stage_key, attempt_id, token, lease_ttl_seconds
        ))
        if self.error:
            raise self.error


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class AttemptLeaseHeartbeatTests(unittest.TestCase):

    def test_progress_callback_renews_only_after_interval(self):
        repository = FakeRepository()
        clock = FakeClock()
        heartbeat = AttemptLeaseHeartbeat(
            repository, "job-1", "stage-1", "attempt-1", "secret-token",
            lease_ttl_seconds=60,
            renew_interval_seconds=10,
            monotonic=clock,
        )

        self.assertFalse(heartbeat.on_progress(1024))
        self.assertEqual(repository.renewals, [])
        clock.advance(9.9)
        self.assertFalse(heartbeat.on_usage({"cpu_seconds": 0.2}))
        self.assertEqual(repository.renewals, [])
        clock.advance(0.1)
        self.assertTrue(heartbeat.on_usage({"cpu_seconds": 0.3}))
        self.assertEqual(len(repository.renewals), 1)
        self.assertEqual(repository.renewals[0][3], "secret-token")
        self.assertFalse(heartbeat.lost)

    def test_assert_owned_forces_renewal_even_before_interval(self):
        repository = FakeRepository()
        heartbeat = AttemptLeaseHeartbeat(
            repository, "job-1", "stage-1", "attempt-1", "secret-token",
            lease_ttl_seconds=30,
            monotonic=FakeClock(),
        )

        self.assertTrue(heartbeat.assert_owned())
        self.assertEqual(len(repository.renewals), 1)

    def test_renewal_failure_fences_future_callbacks(self):
        repository = FakeRepository(error=AttemptLeaseLost("expired"))
        heartbeat = AttemptLeaseHeartbeat(
            repository, "job-1", "stage-1", "attempt-1", "secret-token",
            lease_ttl_seconds=30,
            monotonic=FakeClock(),
        )

        with self.assertRaises(AttemptLeaseLost):
            heartbeat.assert_owned()
        self.assertTrue(heartbeat.lost)
        with self.assertRaises(AttemptLeaseLost):
            heartbeat.on_progress()
        self.assertEqual(len(repository.renewals), 1)

    def test_repr_never_discloses_raw_token(self):
        heartbeat = AttemptLeaseHeartbeat(
            FakeRepository(), "job-1", "stage-1", "attempt-1",
            "very-sensitive-lease-token",
            lease_ttl_seconds=30,
            monotonic=FakeClock(),
        )

        self.assertNotIn("very-sensitive-lease-token", repr(heartbeat))
        self.assertIn("<redacted>", repr(heartbeat))

    def test_invalid_heartbeat_intervals_are_rejected(self):
        for ttl, interval in (
                (30, 0), (30, -1), (30, 30), (30, 31),
                (30, float("nan")), (30, float("inf"))):
            with self.subTest(ttl=ttl, interval=interval):
                with self.assertRaises(ValueError):
                    AttemptLeaseHeartbeat(
                        FakeRepository(), "job-1", "stage-1", "attempt-1",
                        "secret-token",
                        lease_ttl_seconds=ttl,
                        renew_interval_seconds=interval,
                        monotonic=FakeClock(),
                    )

    def test_missing_lease_token_is_rejected(self):
        with self.assertRaises(ValueError):
            AttemptLeaseHeartbeat(
                FakeRepository(), "job-1", "stage-1", "attempt-1", None,
                monotonic=FakeClock(),
            )


if __name__ == "__main__":
    unittest.main()
