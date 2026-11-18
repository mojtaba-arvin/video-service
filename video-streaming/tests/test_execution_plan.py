"""Tests for the vendor-neutral versioned execution-plan contract."""

import copy
import unittest

from video_streaming.workflows.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    ExecutionPlanValidationError,
    execution_plan_sha256,
    serialize_execution_plan,
    validate_execution_plan,
)


def valid_plan():
    return {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "workflow_name": "video.vod",
        "workflow_version": "1",
        "job_id": "job-1",
        "nodes": [
            {
                "node_id": "check-input",
                "operation": "video.input.check",
                "stage_key": "preflight.input",
                "depends_on": [],
                "parameters": {"bucket": "source", "key": "source.mp4"},
                "retry_policy": {
                    "max_attempts": 2,
                    "backoff": "exponential",
                    "initial_delay_seconds": 1,
                    "max_delay_seconds": 30,
                },
                "resource_profile": "control",
            },
            {
                "node_id": "download-input",
                "operation": "video.input.download",
                "stage_key": "input.video",
                "depends_on": ["check-input"],
                "parameters": {"bucket": "source", "key": "source.mp4"},
                "retry_policy": {
                    "max_attempts": 3,
                    "backoff": "exponential",
                    "initial_delay_seconds": 1,
                    "max_delay_seconds": 60,
                },
                "resource_profile": "upload",
            },
            {
                "node_id": "analyze-input",
                "operation": "video.input.analyze",
                "stage_key": "input.video",
                "depends_on": ["download-input"],
                "parameters": {"input_number": 0},
                "retry_policy": {
                    "max_attempts": 1,
                    "backoff": "fixed",
                    "initial_delay_seconds": 0,
                    "max_delay_seconds": 0,
                },
                "resource_profile": "probe",
            },
            {
                "node_id": "inputs-funnel",
                "operation": "video.inputs.funnel",
                "stage_key": "inputs",
                "depends_on": ["analyze-input"],
                "parameters": {},
                "retry_policy": {
                    "max_attempts": 1,
                    "backoff": "fixed",
                    "initial_delay_seconds": 0,
                    "max_delay_seconds": 0,
                },
                "resource_profile": "control",
            },
            {
                "node_id": "package",
                "operation": "video.hls.package",
                "stage_key": "playlist-0",
                "depends_on": ["inputs-funnel"],
                "parameters": {"quality_names": ["360p", "720p"]},
                "retry_policy": {
                    "max_attempts": 2,
                    "backoff": "exponential",
                    "initial_delay_seconds": 2,
                    "max_delay_seconds": 60,
                },
                "resource_profile": "package",
            },
            {
                "node_id": "notify",
                "operation": "video.webhook.deliver",
                "stage_key": "notification.playlist-0",
                "depends_on": ["upload"],
                "parameters": {
                    "webhook_secret_ref": "secret-store://callbacks/job-1"
                },
                "retry_policy": {
                    "max_attempts": 5,
                    "backoff": "exponential",
                    "initial_delay_seconds": 2,
                    "max_delay_seconds": 300,
                },
                "resource_profile": "control",
            },
            {
                "node_id": "upload",
                "operation": "video.output.upload_directory",
                "stage_key": "playlist-0",
                "depends_on": ["package"],
                "parameters": {"bucket": "output", "key": "hls/master.m3u8"},
                "retry_policy": {
                    "max_attempts": 3,
                    "backoff": "exponential",
                    "initial_delay_seconds": 2,
                    "max_delay_seconds": 120,
                },
                "resource_profile": "upload",
            },
        ],
    }


class ExecutionPlanContractTests(unittest.TestCase):

    def test_accepts_dag_with_fan_in_and_secret_reference(self):
        plan = valid_plan()

        validated = validate_execution_plan(plan)

        self.assertEqual(validated["schema_version"], 1)
        self.assertEqual(
            validated["nodes"][5]["parameters"]["webhook_secret_ref"],
            "secret-store://callbacks/job-1",
        )

    def test_serialization_and_fingerprint_are_deterministic(self):
        first = valid_plan()
        second = copy.deepcopy(first)
        second["nodes"][0]["parameters"] = {
            "key": "source.mp4",
            "bucket": "source",
        }

        self.assertEqual(
            serialize_execution_plan(first),
            serialize_execution_plan(second),
        )
        self.assertEqual(
            execution_plan_sha256(first),
            execution_plan_sha256(second),
        )

    def test_validation_returns_a_detached_object(self):
        plan = valid_plan()

        validated = validate_execution_plan(plan)
        validated["nodes"][0]["parameters"]["bucket"] = "changed"

        self.assertEqual(
            plan["nodes"][0]["parameters"]["bucket"], "source"
        )

    def test_rejects_unsupported_schema_version(self):
        plan = valid_plan()
        plan["schema_version"] = 2

        with self.assertRaisesRegex(
                ExecutionPlanValidationError, "unsupported"):
            validate_execution_plan(plan)

    def test_rejects_boolean_schema_version(self):
        plan = valid_plan()
        plan["schema_version"] = True

        with self.assertRaisesRegex(
                ExecutionPlanValidationError, "unsupported"):
            validate_execution_plan(plan)

    def test_rejects_unknown_plan_and_node_fields(self):
        plan = valid_plan()
        plan["celery_canvas"] = "chain(...)"

        with self.assertRaisesRegex(
                ExecutionPlanValidationError, "unknown fields"):
            validate_execution_plan(plan)

        plan = valid_plan()
        plan["nodes"][0]["task_name"] = "internal.task"

        with self.assertRaisesRegex(
                ExecutionPlanValidationError, "unknown fields"):
            validate_execution_plan(plan)

    def test_rejects_duplicate_node_ids(self):
        plan = valid_plan()
        plan["nodes"][1]["node_id"] = "check-input"

        with self.assertRaisesRegex(
                ExecutionPlanValidationError, "duplicate node_id"):
            validate_execution_plan(plan)

    def test_rejects_missing_dependency(self):
        plan = valid_plan()
        plan["nodes"][1]["depends_on"] = ["missing-node"]

        with self.assertRaisesRegex(
                ExecutionPlanValidationError, "missing dependencies"):
            validate_execution_plan(plan)


    def test_rejects_non_string_dependency_values(self):
        plan = valid_plan()
        plan["nodes"][1]["depends_on"] = [{"id": "check-input"}]

        with self.assertRaisesRegex(
                ExecutionPlanValidationError, "non-empty string"):
            validate_execution_plan(plan)

    def test_rejects_dependency_cycles(self):
        plan = valid_plan()
        plan["nodes"][0]["depends_on"] = ["notify"]

        with self.assertRaisesRegex(
                ExecutionPlanValidationError, "cycle"):
            validate_execution_plan(plan)

    def test_rejects_self_dependency_and_duplicate_edges(self):
        plan = valid_plan()
        plan["nodes"][0]["depends_on"] = ["check-input"]

        with self.assertRaisesRegex(
                ExecutionPlanValidationError, "itself"):
            validate_execution_plan(plan)

        plan = valid_plan()
        plan["nodes"][1]["depends_on"] = ["check-input", "check-input"]

        with self.assertRaisesRegex(
                ExecutionPlanValidationError, "duplicate dependencies"):
            validate_execution_plan(plan)

    def test_rejects_raw_webhook_urls_and_credential_fields(self):
        for key, value in (
            ("webhook_url", "https://callback.invalid/?token=secret"),
            ("authorization", "Bearer secret"),
            ("s3_secret_access_key", "secret"),
            ("signed_url", "https://storage.invalid/file?signature=secret"),
        ):
            plan = valid_plan()
            plan["nodes"][0]["parameters"][key] = value
            with self.subTest(key=key):
                with self.assertRaisesRegex(
                        ExecutionPlanValidationError, "sensitive"):
                    validate_execution_plan(plan)

    def test_rejects_empty_secret_reference(self):
        plan = valid_plan()
        plan["nodes"][5]["parameters"]["webhook_secret_ref"] = ""

        with self.assertRaisesRegex(
                ExecutionPlanValidationError, "non-empty"):
            validate_execution_plan(plan)

    def test_rejects_non_finite_or_non_json_parameters(self):
        plan = valid_plan()
        plan["nodes"][0]["parameters"]["ratio"] = float("nan")

        with self.assertRaisesRegex(
                ExecutionPlanValidationError, "non-finite"):
            validate_execution_plan(plan)

        plan = valid_plan()
        plan["nodes"][0]["parameters"]["payload"] = object()

        with self.assertRaisesRegex(
                ExecutionPlanValidationError, "unsupported value type"):
            validate_execution_plan(plan)

    def test_rejects_invalid_retry_policy_and_resource_profile(self):
        plan = valid_plan()
        plan["nodes"][0]["retry_policy"]["max_attempts"] = 0

        with self.assertRaisesRegex(
                ExecutionPlanValidationError, "between 1 and 20"):
            validate_execution_plan(plan)

        plan = valid_plan()
        plan["nodes"][0]["resource_profile"] = "unbounded-host"

        with self.assertRaisesRegex(
                ExecutionPlanValidationError, "unsupported resource profile"):
            validate_execution_plan(plan)


if __name__ == "__main__":
    unittest.main()
