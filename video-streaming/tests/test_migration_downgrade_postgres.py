"""Exercise destructive Alembic downgrade guards against real PostgreSQL.

The test uses temporary tables with the production table/constraint/index names,
so the migration's SQL COUNT and Alembic DDL execute on PostgreSQL without
mutating the CI database's durable workflow schema.
"""
import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


MIGRATION_DIR = (
    Path(__file__).resolve().parents[1] / "migrations" / "versions"
)


def _load_migration(filename):
    spec = importlib.util.spec_from_file_location(
        "postgres_migration_" + filename.replace(".", "_"),
        str(MIGRATION_DIR / filename),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PostgresMigrationDowngradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database_url = os.environ.get("WORKFLOW_DATABASE_URL")
        if not database_url:
            raise unittest.SkipTest(
                "WORKFLOW_DATABASE_URL is required for PostgreSQL migration tests"
            )
        cls.engine = create_engine(database_url, pool_pre_ping=True, future=True)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "engine"):
            cls.engine.dispose()

    def _exercise(self, filename, setup, insert, error, cleanup, postcondition):
        migration = _load_migration(filename)
        with self.engine.connect() as connection:
            for statement in setup:
                connection.execute(text(statement))
            connection.execute(text(insert))
            operations = Operations(MigrationContext.configure(connection))
            with patch.object(migration, "op", operations):
                with self.assertRaisesRegex(RuntimeError, error):
                    migration.downgrade()

                # The guard must fail before any destructive DDL. Removing the
                # sentinel then exercises the real PostgreSQL downgrade path.
                connection.execute(text(cleanup))
                migration.downgrade()

            self.assertTrue(connection.execute(text(postcondition)).scalar())
            connection.rollback()

    def test_all_five_guards_refuse_live_state_then_downgrade_empty_state(self):
        cases = (
            (
                "0006_job_tenant_ownership.py",
                [
                    """CREATE TEMP TABLE workflow_jobs (
                        id varchar(36) PRIMARY KEY,
                        owner_tenant_id varchar(255),
                        owner_issuer varchar(512),
                        owner_subject varchar(255),
                        created_at timestamp,
                        CONSTRAINT ck_workflow_job_owner_identity_complete CHECK (
                            (owner_tenant_id IS NULL AND owner_issuer IS NULL
                             AND owner_subject IS NULL) OR
                            (owner_tenant_id IS NOT NULL AND owner_issuer IS NOT NULL
                             AND owner_subject IS NOT NULL)
                        )
                    )""",
                    "CREATE INDEX ix_workflow_jobs_owner_tenant_created "
                    "ON workflow_jobs(owner_tenant_id, created_at)",
                ],
                "INSERT INTO workflow_jobs VALUES "
                "('job', 'tenant', 'issuer', 'subject', CURRENT_TIMESTAMP)",
                "owned Jobs",
                "DELETE FROM workflow_jobs",
                "SELECT COUNT(*) = 0 FROM information_schema.columns "
                "WHERE table_schema = 'pg_temp' AND table_name = 'workflow_jobs' "
                "AND column_name = 'owner_tenant_id'",
            ),
            (
                "0007_job_secret_store.py",
                [
                    """CREATE TEMP TABLE workflow_jobs (
                        id varchar(36),
                        owner_tenant_id varchar(255),
                        owner_issuer varchar(512),
                        owner_subject varchar(255),
                        CONSTRAINT uq_workflow_jobs_secret_owner UNIQUE
                        (id, owner_tenant_id, owner_issuer, owner_subject)
                    )""",
                    "CREATE TEMP TABLE workflow_job_secrets (secret_id varchar(64))",
                    "CREATE INDEX ix_workflow_job_secrets_scope "
                    "ON workflow_job_secrets(secret_id)",
                ],
                "INSERT INTO workflow_job_secrets VALUES ('secret-1')",
                "encrypted Job secrets",
                "DELETE FROM workflow_job_secrets",
                "SELECT to_regclass('pg_temp.workflow_job_secrets') IS NULL",
            ),
            (
                "0008_attempt_leases.py",
                [
                    """CREATE TEMP TABLE workflow_attempts (
                        id varchar(36),
                        state varchar(32),
                        lease_owner varchar(255),
                        lease_token_hash varchar(64),
                        lease_expires_at timestamptz,
                        heartbeat_at timestamptz,
                        CONSTRAINT ck_workflow_attempt_lease_complete CHECK (
                            (lease_owner IS NULL AND lease_token_hash IS NULL
                             AND lease_expires_at IS NULL AND heartbeat_at IS NULL)
                            OR
                            (lease_owner IS NOT NULL AND lease_token_hash IS NOT NULL
                             AND lease_expires_at IS NOT NULL AND heartbeat_at IS NOT NULL)
                        )
                    )""",
                    "CREATE INDEX ix_workflow_attempts_lease_expiry "
                    "ON workflow_attempts(state, lease_expires_at)",
                ],
                "INSERT INTO workflow_attempts VALUES "
                "('attempt', 'RUNNING', 'worker', 'token', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                "leased or",
                "DELETE FROM workflow_attempts",
                "SELECT COUNT(*) = 0 FROM information_schema.columns "
                "WHERE table_schema = 'pg_temp' AND table_name = 'workflow_attempts' "
                "AND column_name = 'lease_owner'",
            ),
            (
                "0009_transactional_outbox.py",
                [
                    "CREATE TEMP TABLE workflow_outbox (id varchar(36))",
                    "CREATE INDEX ix_workflow_outbox_ready "
                    "ON workflow_outbox(id)",
                    "CREATE INDEX ix_workflow_outbox_job_created "
                    "ON workflow_outbox(id)",
                ],
                "INSERT INTO workflow_outbox VALUES ('outbox-1')",
                "Outbox",
                "DELETE FROM workflow_outbox",
                "SELECT to_regclass('pg_temp.workflow_outbox') IS NULL",
            ),
            (
                "0010_workflow_stage_dependencies.py",
                [
                    """CREATE TEMP TABLE workflow_stages (
                        id varchar(36),
                        workflow_id varchar(36),
                        CONSTRAINT uq_workflow_stage_id_workflow
                        UNIQUE (id, workflow_id)
                    )""",
                    """CREATE TEMP TABLE workflow_stage_dependencies (
                        stage_id varchar(36),
                        depends_on_stage_id varchar(36),
                        workflow_id varchar(36),
                        CONSTRAINT ck_workflow_stage_dependency_not_self
                        CHECK (stage_id <> depends_on_stage_id)
                    )""",
                    "CREATE INDEX ix_workflow_stage_dependencies_workflow "
                    "ON workflow_stage_dependencies(workflow_id, stage_id)",
                ],
                "INSERT INTO workflow_stage_dependencies "
                "VALUES ('stage-a', 'stage-b', 'workflow')",
                "dependency edges",
                "DELETE FROM workflow_stage_dependencies",
                "SELECT to_regclass('pg_temp.workflow_stage_dependencies') IS NULL",
            ),
        )
        for case in cases:
            with self.subTest(migration=case[0]):
                self._exercise(*case)


if __name__ == "__main__":
    unittest.main()
