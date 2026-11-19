"""Process-local heartbeat support for durable workflow attempt leases.

The raw lease capability must remain inside the worker process. This helper is
intentionally not serializable and never writes lease material to logs/events.
Long-running supervised operations call :meth:`on_usage` or
:meth:`on_progress`; state mutations call :meth:`assert_owned` immediately
before committing their result.
"""

import math
import time

from .domain import AttemptLeaseLost


class AttemptLeaseHeartbeat:
    """Renew one attempt lease from a worker-local progress callback."""

    __slots__ = (
        "_repository",
        "_job_id",
        "_stage_key",
        "_attempt_id",
        "_token",
        "_ttl_seconds",
        "_renew_interval",
        "_monotonic",
        "_last_renewed",
        "_lost",
    )

    def __init__(
            self,
            repository,
            job_id,
            stage_key,
            attempt_id,
            lease_token,
            lease_ttl_seconds=60,
            renew_interval_seconds=None,
            monotonic=time.monotonic):
        if not lease_token or not isinstance(lease_token, str):
            raise ValueError("A process-local lease token is required")
        ttl = repository._validated_lease_ttl(lease_ttl_seconds)
        interval = (
            float(renew_interval_seconds)
            if renew_interval_seconds is not None else ttl / 3.0
        )
        if not math.isfinite(interval) or interval <= 0 or interval >= ttl:
            raise ValueError("Heartbeat interval must be positive and shorter than the lease TTL")

        self._repository = repository
        self._job_id = job_id
        self._stage_key = stage_key
        self._attempt_id = attempt_id
        self._token = lease_token
        self._ttl_seconds = ttl
        self._renew_interval = interval
        self._monotonic = monotonic
        self._last_renewed = monotonic()
        self._lost = False

    def __repr__(self):
        return (
            "AttemptLeaseHeartbeat(job_id={!r}, stage_key={!r}, "
            "attempt_id={!r}, token=<redacted>, lost={!r})"
        ).format(
            self._job_id, self._stage_key, self._attempt_id, self._lost
        )

    @property
    def lost(self):
        return self._lost

    def _renew(self):
        if self._lost:
            raise AttemptLeaseLost("Attempt lease is no longer owned")
        try:
            self._repository.renew_attempt_lease(
                self._job_id,
                self._stage_key,
                self._attempt_id,
                self._token,
                lease_ttl_seconds=self._ttl_seconds,
            )
        except Exception:
            # A failed renewal is fail-closed: the caller must stop the
            # supervised operation rather than continue with uncertain ownership.
            self._lost = True
            raise
        self._last_renewed = self._monotonic()
        return True

    def on_progress(self, *_args, **_kwargs):
        """Callback adapter for SDK/transfer progress callbacks."""
        if self._lost:
            raise AttemptLeaseLost("Attempt lease is no longer owned")
        if self._monotonic() - self._last_renewed >= self._renew_interval:
            return self._renew()
        return False

    def on_usage(self, _metrics):
        """Callback adapter for process-tree usage samples."""
        return self.on_progress()

    def assert_owned(self):
        """Force a DB renewal before a validation or state mutation boundary."""
        return self._renew()
