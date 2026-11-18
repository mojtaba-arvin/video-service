"""Versioned, transport-neutral execution-plan contract.

This module intentionally has no Celery imports. It validates a durable
description of work before a repository or execution adapter stores/consumes it.
"""

import hashlib
import json
import math
from copy import deepcopy


EXECUTION_PLAN_SCHEMA_VERSION = 1
MAX_PLAN_NODES = 500
MAX_DEPENDENCIES_PER_NODE = 100

PLAN_OPERATIONS = frozenset({
    "video.input.check",
    "video.input.download",
    "video.input.analyze",
    "video.inputs.funnel",
    "video.output.bucket.check",
    "video.output.key.check",
    "video.hls.package",
    "video.dash.package",
    "video.thumbnail.generate",
    "video.watermark.apply",
    "video.output.upload_file",
    "video.output.upload_directory",
    "video.webhook.deliver",
})

RESOURCE_PROFILES = frozenset({
    "control",
    "probe",
    "thumbnail",
    "package",
    "transcode",
    "upload",
})

_PLAN_FIELDS = frozenset({
    "schema_version",
    "workflow_name",
    "workflow_version",
    "job_id",
    "nodes",
})

_NODE_FIELDS = frozenset({
    "node_id",
    "operation",
    "stage_key",
    "depends_on",
    "parameters",
    "retry_policy",
    "resource_profile",
})

_RETRY_FIELDS = frozenset({
    "max_attempts",
    "backoff",
    "initial_delay_seconds",
    "max_delay_seconds",
})

_SENSITIVE_PARAMETER_KEYS = frozenset({
    "webhook_url",
    "callback_url",
    "signed_url",
    "authorization",
    "bearer_token",
    "access_key",
    "access_key_id",
    "secret",
    "secret_key",
    "secret_access_key",
    "password",
    "credential",
    "credentials",
    "token",
    "api_key",
    "private_key",
    "client_secret",
    "access_token",
    "refresh_token",
})

_SENSITIVE_PARAMETER_SUFFIXES = (
    "_webhook_url",
    "_callback_url",
    "_signed_url",
    "_authorization",
    "_bearer_token",
    "_access_key",
    "_access_key_id",
    "_secret",
    "_secret_key",
    "_secret_access_key",
    "_password",
    "_credential",
    "_credentials",
    "_token",
    "_api_key",
    "_private_key",
    "_client_secret",
)


class ExecutionPlanValidationError(ValueError):
    """Raised when an execution plan is unsafe or not schema-compatible."""


def _require_text(value, field_name, max_length=255):
    if not isinstance(value, str) or not value.strip():
        raise ExecutionPlanValidationError(
            "{} must be a non-empty string".format(field_name)
        )
    if len(value) > max_length:
        raise ExecutionPlanValidationError(
            "{} exceeds {} characters".format(field_name, max_length)
        )
    return value


def _validate_json_value(value, path):
    """Reject non-JSON values, non-finite numbers and embedded credentials."""
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExecutionPlanValidationError(
                "{} contains a non-finite number".format(path)
            )
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, "{}[{}]".format(path, index))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ExecutionPlanValidationError(
                    "{} contains a non-string object key".format(path)
                )
            normalized_key = key.strip().lower()
            # A secret reference is an opaque lookup handle, never secret data.
            if (
                not normalized_key.endswith("_secret_ref")
                and (
                    normalized_key in _SENSITIVE_PARAMETER_KEYS
                    or normalized_key.endswith(
                        _SENSITIVE_PARAMETER_SUFFIXES
                    )
                )
            ):
                raise ExecutionPlanValidationError(
                    "{}.{} is sensitive and must not be stored in a plan"
                    .format(path, key)
                )
            if normalized_key.endswith("_secret_ref"):
                _require_text(item, "{}.{}".format(path, key), max_length=512)
                continue
            _validate_json_value(item, "{}.{}".format(path, key))
        return
    raise ExecutionPlanValidationError(
        "{} contains unsupported value type {}".format(
            path, type(value).__name__
        )
    )


def validate_execution_plan(plan):
    """Validate and return a detached JSON-safe plan dictionary.

    Dependencies are required to refer to existing node IDs, and the graph
    must be acyclic. The validator does not assume Celery or task-name identity.
    """
    if not isinstance(plan, dict):
        raise ExecutionPlanValidationError("plan must be an object")

    unknown_plan_fields = set(plan) - _PLAN_FIELDS
    missing_plan_fields = _PLAN_FIELDS - set(plan)
    if unknown_plan_fields:
        raise ExecutionPlanValidationError(
            "plan contains unknown fields: {}".format(
                ", ".join(sorted(unknown_plan_fields))
            )
        )
    if missing_plan_fields:
        raise ExecutionPlanValidationError(
            "plan is missing fields: {}".format(
                ", ".join(sorted(missing_plan_fields))
            )
        )

    if (
        isinstance(plan["schema_version"], bool)
        or not isinstance(plan["schema_version"], int)
        or plan["schema_version"] != EXECUTION_PLAN_SCHEMA_VERSION
    ):
        raise ExecutionPlanValidationError(
            "unsupported execution-plan schema version: {}".format(
                plan["schema_version"]
            )
        )
    _require_text(plan["workflow_name"], "workflow_name", max_length=128)
    _require_text(plan["workflow_version"], "workflow_version", max_length=64)
    _require_text(plan["job_id"], "job_id", max_length=36)

    nodes = plan["nodes"]
    if not isinstance(nodes, list) or not nodes:
        raise ExecutionPlanValidationError("nodes must be a non-empty list")
    if len(nodes) > MAX_PLAN_NODES:
        raise ExecutionPlanValidationError(
            "plan exceeds the {} node limit".format(MAX_PLAN_NODES)
        )

    detached = deepcopy(plan)
    node_ids = set()
    normalized_nodes = []

    for index, node in enumerate(detached["nodes"]):
        node_path = "nodes[{}]".format(index)
        if not isinstance(node, dict):
            raise ExecutionPlanValidationError(
                "{} must be an object".format(node_path)
            )
        unknown = set(node) - _NODE_FIELDS
        missing = _NODE_FIELDS - set(node)
        if unknown:
            raise ExecutionPlanValidationError(
                "{} contains unknown fields: {}".format(
                    node_path, ", ".join(sorted(unknown))
                )
            )
        if missing:
            raise ExecutionPlanValidationError(
                "{} is missing fields: {}".format(
                    node_path, ", ".join(sorted(missing))
                )
            )

        node_id = _require_text(
            node["node_id"], "{}.node_id".format(node_path), max_length=128
        )
        if node_id in node_ids:
            raise ExecutionPlanValidationError(
                "duplicate node_id: {}".format(node_id)
            )
        node_ids.add(node_id)

        operation = _require_text(
            node["operation"], "{}.operation".format(node_path), max_length=128
        )
        if operation not in PLAN_OPERATIONS:
            raise ExecutionPlanValidationError(
                "unsupported operation for {}: {}".format(node_id, operation)
            )
        _require_text(
            node["stage_key"], "{}.stage_key".format(node_path), max_length=128
        )

        dependencies = node["depends_on"]
        if not isinstance(dependencies, list):
            raise ExecutionPlanValidationError(
                "{}.depends_on must be a list".format(node_path)
            )
        if len(dependencies) > MAX_DEPENDENCIES_PER_NODE:
            raise ExecutionPlanValidationError(
                "{} exceeds the dependency limit".format(node_path)
            )
        for dependency in dependencies:
            _require_text(
                dependency,
                "{}.depends_on[]".format(node_path),
                max_length=128,
            )
        if len(dependencies) != len(set(dependencies)):
            raise ExecutionPlanValidationError(
                "{} contains duplicate dependencies".format(node_id)
            )
        for dependency in dependencies:
            if dependency == node_id:
                raise ExecutionPlanValidationError(
                    "{} cannot depend on itself".format(node_id)
                )

        parameters = node["parameters"]
        if not isinstance(parameters, dict):
            raise ExecutionPlanValidationError(
                "{}.parameters must be an object".format(node_path)
            )
        _validate_json_value(parameters, "{}.parameters".format(node_path))

        retry_policy = node["retry_policy"]
        if not isinstance(retry_policy, dict):
            raise ExecutionPlanValidationError(
                "{}.retry_policy must be an object".format(node_path)
            )
        if set(retry_policy) != _RETRY_FIELDS:
            raise ExecutionPlanValidationError(
                "{}.retry_policy must contain exactly {}".format(
                    node_path, ", ".join(sorted(_RETRY_FIELDS))
                )
            )
        attempts = retry_policy["max_attempts"]
        if isinstance(attempts, bool) or not isinstance(attempts, int):
            raise ExecutionPlanValidationError(
                "{}.retry_policy.max_attempts must be an integer".format(
                    node_path
                )
            )
        if not 1 <= attempts <= 20:
            raise ExecutionPlanValidationError(
                "{}.retry_policy.max_attempts must be between 1 and 20".format(
                    node_path
                )
            )
        if retry_policy["backoff"] not in ("fixed", "exponential"):
            raise ExecutionPlanValidationError(
                "{}.retry_policy.backoff must be fixed or exponential".format(
                    node_path
                )
            )
        for key in ("initial_delay_seconds", "max_delay_seconds"):
            value = retry_policy[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ExecutionPlanValidationError(
                    "{}.retry_policy.{} must be a finite non-negative number"
                    .format(node_path, key)
                )
        if retry_policy["max_delay_seconds"] < retry_policy[
            "initial_delay_seconds"
        ]:
            raise ExecutionPlanValidationError(
                "{}.retry_policy.max_delay_seconds cannot be smaller than "
                "initial_delay_seconds".format(node_path)
            )

        resource_profile = _require_text(
            node["resource_profile"],
            "{}.resource_profile".format(node_path),
            max_length=32,
        )
        if resource_profile not in RESOURCE_PROFILES:
            raise ExecutionPlanValidationError(
                "unsupported resource profile: {}".format(resource_profile)
            )

        normalized_nodes.append(node)

    for node in normalized_nodes:
        missing_dependencies = set(node["depends_on"]) - node_ids
        if missing_dependencies:
            raise ExecutionPlanValidationError(
                "{} references missing dependencies: {}".format(
                    node["node_id"],
                    ", ".join(sorted(missing_dependencies)),
                )
            )

    # Kahn's algorithm validates the whole DAG, including forward references.
    remaining_dependencies = {
        node["node_id"]: set(node["depends_on"])
        for node in normalized_nodes
    }
    dependents = {node_id: set() for node_id in node_ids}
    for node_id, dependencies in remaining_dependencies.items():
        for dependency in dependencies:
            dependents[dependency].add(node_id)

    ready = [
        node_id for node_id, dependencies in remaining_dependencies.items()
        if not dependencies
    ]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for dependent in dependents[current]:
            remaining_dependencies[dependent].discard(current)
            if not remaining_dependencies[dependent]:
                ready.append(dependent)
    if visited != len(node_ids):
        raise ExecutionPlanValidationError(
            "execution plan contains a dependency cycle"
        )

    # Verify the detached object can be encoded without custom serializers.
    try:
        json.dumps(detached, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ExecutionPlanValidationError(
            "execution plan is not JSON serializable"
        ) from exc
    return detached


def serialize_execution_plan(plan):
    """Return canonical JSON suitable for immutable persistence and hashing."""
    validated = validate_execution_plan(plan)
    return json.dumps(
        validated,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def execution_plan_sha256(plan):
    """Return the stable SHA-256 fingerprint of a validated plan."""
    serialized = serialize_execution_plan(plan)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
