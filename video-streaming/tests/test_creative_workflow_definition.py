"""Tests for versioned creative workflow definition contracts."""

import copy
import unittest

from video_streaming.workflows.creative_definition import (
    CreativeDefinitionError,
    creative_definition_sha256,
    instantiate_creative_workflow_definition,
    validate_creative_workflow_definition,
)


class CreativeWorkflowDefinitionTests(unittest.TestCase):

    def make_definition(self):
        return {
            "schema_version": 1,
            "definition_id": "creative.video.basic",
            "version": "1.0.0",
            "workflow_name": "creative.video.basic",
            "nodes": [{
                "node_id": "probe",
                "operation": "video.input.analyze",
                "stage_key": "probe",
                "depends_on": [],
                "parameters": {"input_name": "source"},
                "retry_policy": {
                    "max_attempts": 2,
                    "backoff": "exponential",
                    "initial_delay_seconds": 1,
                    "max_delay_seconds": 15,
                },
                "resource_profile": "probe",
            }],
        }

    def test_definition_is_reusable_and_instantiates_for_a_job(self):
        definition = self.make_definition()
        validated = validate_creative_workflow_definition(definition)
        plan = instantiate_creative_workflow_definition(
            definition, "job-123"
        )

        self.assertEqual(validated["definition_id"], "creative.video.basic")
        self.assertEqual(validated["version"], "1.0.0")
        self.assertNotIn("job_id", validated)
        self.assertEqual(plan["job_id"], "job-123")
        self.assertEqual(plan["workflow_version"], "1.0.0")
        self.assertEqual(plan["nodes"], validated["nodes"])

    def test_fingerprint_is_canonical_and_version_sensitive(self):
        definition = self.make_definition()
        reordered = {
            key: definition[key]
            for key in reversed(list(definition))
        }
        self.assertEqual(
            creative_definition_sha256(definition),
            creative_definition_sha256(reordered),
        )

        changed_version = copy.deepcopy(definition)
        changed_version["version"] = "1.0.1"
        self.assertNotEqual(
            creative_definition_sha256(definition),
            creative_definition_sha256(changed_version),
        )

    def test_rejects_unknown_fields_and_unsupported_schema(self):
        definition = self.make_definition()
        definition["provider"] = "vendor-specific"
        with self.assertRaisesRegex(CreativeDefinitionError, "unknown fields"):
            validate_creative_workflow_definition(definition)

        definition = self.make_definition()
        definition["schema_version"] = 2
        with self.assertRaisesRegex(CreativeDefinitionError, "unsupported"):
            validate_creative_workflow_definition(definition)

    def test_reuses_execution_plan_secret_and_dag_validation(self):
        definition = self.make_definition()
        definition["nodes"][0]["parameters"]["callback_url"] = (
            "https://example.invalid/callback"
        )
        with self.assertRaisesRegex(
            CreativeDefinitionError, "invalid execution graph"
        ):
            validate_creative_workflow_definition(definition)

        definition = self.make_definition()
        definition["nodes"][0]["depends_on"] = ["probe"]
        with self.assertRaisesRegex(
            CreativeDefinitionError, "invalid execution graph"
        ):
            validate_creative_workflow_definition(definition)


if __name__ == "__main__":
    unittest.main()
