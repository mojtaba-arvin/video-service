"""Lease-fenced executor for versioned workflow DAG nodes.

Operation implementations are injected adapters. The registry is an explicit
allow-list and must cover every operation in the persisted plan before the
executor can be constructed. Each adapter receives a deterministic idempotency
key and is responsible for applying it to every external side effect.
"""

import re
from contextlib import nullcontext
from dataclasses import dataclass

from video_streaming.execution.attempt_scope import attempt_cgroup_scope

from video_streaming.workflows.domain import (
    AttemptLeaseLost,
    InvalidStateTransition,
    StageAlreadyRunning,
)
from video_streaming.workflows.execution_plan import PLAN_OPERATIONS
from video_streaming.workflows.operation_contracts import OPERATION_CONTRACTS


@dataclass(frozen=True)
class NodeExecutionContext:
    job_id: str
    workflow_id: str
    node_id: str
    stage_key: str
    attempt_id: str
    attempt_number: int
    plan_fingerprint: str
    idempotency_key: str
    heartbeat: object
    cancel: object
    is_cancelled: object
    is_terminal: object
    input_artifacts: dict = None


class WorkflowNodeExecutor:
    """Execute one persisted node after its dependencies and lease are checked."""

    def __init__(self, repository, handlers, worker_id, lease_ttl_seconds=60):
        if repository is None:
            raise ValueError("repository is required")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id is required")
        if not isinstance(handlers, dict) or set(handlers) != set(PLAN_OPERATIONS):
            missing = set(PLAN_OPERATIONS) - set(handlers or {})
            extra = set(handlers or {}) - set(PLAN_OPERATIONS)
            raise ValueError(
                "handler registry must exactly cover PLAN_OPERATIONS; "
                "missing={}, extra={}".format(
                    sorted(missing), sorted(extra)
                )
            )
        if any(not callable(handler) for handler in handlers.values()):
            raise TypeError("every workflow operation handler must be callable")
        self.repository = repository
        self.handlers = dict(handlers)
        self.worker_id = worker_id.strip()
        self.lease_ttl_seconds = lease_ttl_seconds

    @staticmethod
    def _uses_cgroup(operation):
        contract = OPERATION_CONTRACTS[operation]
        return contract.cancellation == "process_group_termination"

    @staticmethod
    def _capture_cgroup_usage(cgroup):
        if cgroup is None:
            return None, None
        try:
            return cgroup.usage(), None
        except Exception as exc:
            # Resource accounting must never replace the handler's success or
            # failure classification. Retain only a safe exception type code.
            code = re.sub(r"[^A-Za-z0-9_.-]", "_", type(exc).__name__)[:128]
            return None, code

    @staticmethod
    def _merge_cgroup_metrics(metrics, usage, usage_error_code=None):
        merged = dict(metrics or {})
        if usage_error_code:
            merged["cgroup_metrics_status"] = "unavailable"
            merged["cgroup_metrics_error_code"] = usage_error_code
        if not isinstance(usage, dict):
            return merged
        cpu = usage.get("cpu") or {}
        cpu_usec = cpu.get("usage_usec")
        if cpu_usec is not None:
            measured = float(cpu_usec) / 1000000.0
            previous = merged.get("cpu_seconds")
            merged["cpu_seconds"] = max(
                measured, float(previous) if previous is not None else 0.0
            )
        peak = usage.get("memory_peak_bytes")
        if peak is not None:
            previous = merged.get("peak_memory_bytes")
            merged["peak_memory_bytes"] = max(
                int(peak), int(previous) if previous is not None else 0
            )
        return merged

    @staticmethod
    def _error_code(exc):
        return re.sub(r"[^A-Za-z0-9_.-]", "_", type(exc).__name__)[:128]

    @staticmethod
    def _retry_delay(retry_policy, attempt_number):
        """Calculate the persisted node policy's bounded retry delay."""
        initial = float(retry_policy["initial_delay_seconds"])
        maximum = float(retry_policy["max_delay_seconds"])
        if retry_policy["backoff"] == "fixed":
            return min(initial, maximum)
        return min(maximum, initial * (2 ** max(0, attempt_number - 1)))

    def execute(self, job_id, node_id, execution_ref=None):
        snapshot = self.repository.get_execution_plan(job_id)
        if snapshot is None:
            raise KeyError("workflow execution plan not found")
        plan = snapshot["plan"]
        node = next(
            (item for item in plan["nodes"] if item["node_id"] == node_id),
            None,
        )
        if node is None:
            raise KeyError("workflow node not found")
        operation = node["operation"]
        if operation not in PLAN_OPERATIONS or operation not in self.handlers:
            raise ValueError("workflow operation is not allow-listed")
        stage_key = node["stage_key"]
        input_artifacts = self.repository.get_dependency_artifacts(
            job_id, node_id
        )
        try:
            attempt_id, attempt_number, token, _ = (
                self.repository.claim_node_attempt(
                    job_id,
                    stage_key,
                    lease_owner=self.worker_id,
                    lease_ttl_seconds=self.lease_ttl_seconds,
                    execution_ref=execution_ref,
                )
            )
        except StageAlreadyRunning:
            return {"status": "already_running", "node_id": node_id}
        except InvalidStateTransition:
            stage = self.repository.get_stage_snapshot(job_id, stage_key)
            job = self.repository.get_job_snapshot(job_id)
            if job and job["status"] == "CANCELLED":
                return {"status": "cancelled", "node_id": node_id}
            if stage and stage["status"] == "FINISHED":
                return {"status": "already_completed", "node_id": node_id}
            return {"status": "dependencies_pending", "node_id": node_id}

        idempotency_key = "{}:{}:{}".format(
            job_id, snapshot["fingerprint"], node_id
        )

        def heartbeat():
            return self.repository.renew_attempt_lease(
                job_id,
                stage_key,
                attempt_id,
                token,
                lease_ttl_seconds=self.lease_ttl_seconds,
            )

        def is_cancelled():
            current = self.repository.get_job_snapshot(job_id)
            return current is not None and current["status"] == "CANCELLED"

        def is_terminal():
            current = self.repository.get_job_snapshot(job_id)
            return current is None or current["status"] in (
                "CANCELLED", "FAILED", "FINISHED",
            )

        def cancel(reason="WORKER_CANCELLED"):
            return self.repository.cancel_attempt(
                job_id,
                stage_key,
                attempt_id,
                reason=reason,
                lease_token=token,
            )

        context = NodeExecutionContext(
            job_id=job_id,
            workflow_id=snapshot["workflow_id"],
            node_id=node_id,
            stage_key=stage_key,
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            plan_fingerprint=snapshot["fingerprint"],
            idempotency_key=idempotency_key,
            heartbeat=heartbeat,
            cancel=cancel,
            is_cancelled=is_cancelled,
            is_terminal=is_terminal,
            input_artifacts=input_artifacts,
        )
        cgroup_usage = None
        cgroup_usage_error_code = None
        try:
            if is_terminal():
                self.repository.cancel_attempt(
                    job_id, stage_key, attempt_id,
                    reason="JOB_CANCELLED_BEFORE_NODE_START",
                    lease_token=token,
                )
                return {"status": "cancelled", "node_id": node_id}
            scope = (
                attempt_cgroup_scope(attempt_id)
                if self._uses_cgroup(operation) else nullcontext(None)
            )
            with scope as cgroup:
                try:
                    result = self.handlers[operation](node, context)
                finally:
                    # Read kernel counters before the scope removes its cgroup.
                    # Capture on failure too, so failed/retried Attempts retain
                    # the resource cost observed before the exception.
                    if cgroup is not None:
                        (
                            cgroup_usage,
                            cgroup_usage_error_code,
                        ) = self._capture_cgroup_usage(cgroup)
            if not isinstance(result, dict):
                raise ValueError("node handler must return a result dictionary")
            result["metrics"] = self._merge_cgroup_metrics(
                result.get("metrics"),
                cgroup_usage,
                cgroup_usage_error_code,
            )
            validation_report = result.get("validation_report")
            if not isinstance(validation_report, dict):
                raise ValueError("node handler omitted validation_report")
            completed = self.repository.complete_node_attempt(
                job_id,
                stage_key,
                attempt_id,
                validation_report,
                lease_token=token,
                metrics=result.get("metrics"),
                artifacts=result.get("artifacts"),
            )
            if not completed:
                return {"status": "cancelled", "node_id": node_id}
            return {
                "status": "completed",
                "node_id": node_id,
                "attempt_id": attempt_id,
                "attempt_number": attempt_number,
                "idempotency_key": idempotency_key,
            }
        except AttemptLeaseLost:
            current = self.repository.get_job_snapshot(job_id)
            if current and current["status"] == "CANCELLED":
                return {"status": "cancelled", "node_id": node_id}
            if current and current["status"] in ("FAILED", "FINISHED"):
                return {
                    "status": "terminal",
                    "node_id": node_id,
                    "job_status": current["status"],
                }
            return {"status": "lease_lost", "node_id": node_id}
        except Exception as exc:
            contract = OPERATION_CONTRACTS[operation]
            explicitly_retryable = getattr(exc, "retryable", None)
            retryable = (
                contract.retry_class != "never"
                and attempt_number < node["retry_policy"]["max_attempts"]
                and explicitly_retryable is not False
            )
            retry_after_seconds = (
                self._retry_delay(node["retry_policy"], attempt_number)
                if retryable else None
            )
            try:
                failure_recorded = self.repository.fail_attempt(
                    job_id,
                    stage_key,
                    attempt_id,
                    retryable=retryable,
                    error_code=self._error_code(exc),
                    # Exception messages may include customer values or URLs.
                    error_message=type(exc).__name__,
                    metrics=self._merge_cgroup_metrics(
                        {}, cgroup_usage, cgroup_usage_error_code
                    ),
                    lease_token=token,
                    retry_after_seconds=retry_after_seconds,
                )
                if not failure_recorded:
                    current = self.repository.get_job_snapshot(job_id)
                    return {
                        "status": "terminal",
                        "node_id": node_id,
                        "job_status": (
                            current["status"] if current else "UNKNOWN"
                        ),
                    }
            except AttemptLeaseLost:
                current = self.repository.get_job_snapshot(job_id)
                if current and current["status"] == "CANCELLED":
                    return {"status": "cancelled", "node_id": node_id}
                return {"status": "lease_lost", "node_id": node_id}
            outcome = {
                "status": "retryable_failure" if retryable else "failed",
                "node_id": node_id,
                "attempt_id": attempt_id,
                "error_code": self._error_code(exc),
            }
            if retryable:
                outcome["retry_after_seconds"] = retry_after_seconds
            return outcome
