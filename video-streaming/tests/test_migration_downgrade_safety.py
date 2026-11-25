"""Fail closed before destructive schema downgrades discard live state."""

import importlib.util
import os
import unittest
from unittest.mock import patch


MIGRATION_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "migrations", "versions",
)


class _ScalarResult:
    def __init__(self, count):
        self.count = count

    def scalar_one(self):
        return self.count


class _Connection:
    def __init__(self, count):
        self.count = count
        self.statements = []

    def execute(self, statement):
        self.statements.append(str(statement))
        return _ScalarResult(self.count)


class _Operations:
    def __init__(self, count):
        self.connection = _Connection(count)
        self.calls = []

    def get_bind(self):
        return self.connection

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return record


class _SelectiveConnection:
    def __init__(self):
        self.statements = []

    def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if "FROM workflow_jobs" in sql:
            return _ScalarResult(1)
        return _ScalarResult(0)


class _SelectiveOperations(_Operations):
    def __init__(self):
        self.connection = _SelectiveConnection()
        self.calls = []


def _load_migration(filename):
    path = os.path.join(MIGRATION_DIR, filename)
    spec = importlib.util.spec_from_file_location(
        "migration_" + filename.replace(".", "_"), path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DestructiveDowngradeGuardTests(unittest.TestCase):
    CASES = (
        ("0006_job_tenant_ownership.py", "workflow_jobs", "owned Jobs"),
        ("0007_job_secret_store.py", "workflow_job_secrets", "encrypted Job secrets"),
        ("0008_attempt_leases.py", "workflow_attempts", "Attempt lease state"),
        ("0009_transactional_outbox.py", "workflow_outbox", "workflow Outbox"),
        ("0010_workflow_stage_dependencies.py", "workflow_stage_dependencies", "dependency edges"),
        ("0011_tenant_job_quotas.py", "workflow_tenant_job_quota_policies", "tenant quota policies"),
        ("0013_creative_project_revisions.py", "workflow_creative_project_revisions", "immutable creative project revision history"),
        ("0014_creative_project_typed_references.py", "workflow_creative_project_revision_references", "creative project revision references"),
        ("0015_creative_asset_provenance.py", "workflow_creative_asset_provenance", "asset provenance"),
        ("0015_creative_asset_provenance.py", "workflow_creative_asset_provenance", "asset provenance"),
    )

    def test_downgrade_refuses_to_discard_existing_state(self):
        for filename, table, message in self.CASES:
            with self.subTest(migration=filename):
                migration = _load_migration(filename)
                operations = _Operations(count=1)
                with patch.object(migration, "op", operations):
                    with self.assertRaisesRegex(RuntimeError, message):
                        migration.downgrade()
                self.assertTrue(operations.connection.statements)
                self.assertIn(table, operations.connection.statements[0])
                self.assertEqual(operations.calls, [])

    def test_provenance_downgrade_refuses_pinned_jobs(self):
        migration = _load_migration("0015_creative_asset_provenance.py")
        operations = _SelectiveOperations()
        with patch.object(migration, "op", operations):
            with self.assertRaisesRegex(RuntimeError, "ProjectRevision pins"):
                migration.downgrade()
        self.assertEqual(len(operations.connection.statements), 2)
        self.assertIn("workflow_jobs", operations.connection.statements[1])
        self.assertEqual(operations.calls, [])

    def test_empty_state_preserves_normal_downgrade_path(self):
        for filename, table, _ in self.CASES:
            with self.subTest(migration=filename):
                migration = _load_migration(filename)
                operations = _Operations(count=0)
                with patch.object(migration, "op", operations):
                    migration.downgrade()
                self.assertTrue(operations.connection.statements)
                self.assertTrue(operations.calls)


if __name__ == "__main__":
    unittest.main()
