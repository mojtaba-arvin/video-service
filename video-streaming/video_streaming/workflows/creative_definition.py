"""Versioned, provider-neutral creative workflow definition contracts.

A definition is a reusable DAG template. It contains no Job identity, provider
credentials, LLM SDK objects, or runtime handles. Instantiation binds a Job ID
and reuses the canonical execution-plan validator and fingerprint algorithm.
"""

import hashlib
import json

from video_streaming.workflows.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    validate_execution_plan,
)

CREATIVE_DEFINITION_SCHEMA_VERSION = 1
_DEFINITION_FIELDS = frozenset({
    "schema_version",
    "definition_id",
    "version",
    "workflow_name",
    "nodes",
})


class CreativeDefinitionError(ValueError):
    """Raised when a creative workflow definition is invalid or unsupported."""


def _required_text(value, field, maximum):
    if not isinstance(value, str) or not value.strip():
        raise CreativeDefinitionError(
            "{} must be a non-empty string".format(field)
        )
    if len(value) > maximum:
        raise CreativeDefinitionError(
            "{} exceeds {} characters".format(field, maximum)
        )
    return value


def validate_creative_workflow_definition(definition):
    """Validate and detach a reusable, versioned workflow DAG template."""
    if not isinstance(definition, dict):
        raise CreativeDefinitionError("definition must be an object")

    unknown = set(definition) - _DEFINITION_FIELDS
    missing = _DEFINITION_FIELDS - set(definition)
    if unknown:
        raise CreativeDefinitionError(
            "definition contains unknown fields: {}".format(
                ", ".join(sorted(unknown))
            )
        )
    if missing:
        raise CreativeDefinitionError(
            "definition is missing fields: {}".format(
                ", ".join(sorted(missing))
            )
        )

    schema_version = definition["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != CREATIVE_DEFINITION_SCHEMA_VERSION
    ):
        raise CreativeDefinitionError(
            "unsupported creative definition schema version: {}".format(
                schema_version
            )
        )

    definition_id = _required_text(
        definition["definition_id"], "definition_id", 128
    )
    version = _required_text(definition["version"], "version", 64)
    workflow_name = _required_text(
        definition["workflow_name"], "workflow_name", 128
    )

    # Delegate DAG, operation, retry, resource-profile and secret validation
    # to the same contract used by persisted Job execution plans.
    try:
        plan = validate_execution_plan({
            "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
            "workflow_name": workflow_name,
            "workflow_version": version,
            "job_id": "definition-validation",
            "nodes": definition["nodes"],
        })
    except (TypeError, ValueError) as exc:
        raise CreativeDefinitionError(
            "creative definition contains an invalid execution graph: {}".format(
                exc
            )
        ) from exc

    return {
        "schema_version": CREATIVE_DEFINITION_SCHEMA_VERSION,
        "definition_id": definition_id,
        "version": version,
        "workflow_name": workflow_name,
        "nodes": plan["nodes"],
    }


def creative_definition_sha256(definition):
    """Fingerprint definition identity, version and canonical DAG content."""
    validated = validate_creative_workflow_definition(definition)
    serialized = json.dumps(
        validated,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def instantiate_creative_workflow_definition(definition, job_id):
    """Bind a reusable definition to a Job and return a validated execution plan."""
    validated = validate_creative_workflow_definition(definition)
    return validate_execution_plan({
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "workflow_name": validated["workflow_name"],
        "workflow_version": validated["version"],
        "job_id": job_id,
        "nodes": validated["nodes"],
    })
