"""Persist immutable, attempt-bound creative Asset provenance.

Revision ID: 0015_asset_provenance
Revises: 0014_project_refs
"""
from alembic import op
import sqlalchemy as sa


revision = "0015_asset_provenance"
down_revision = "0014_project_refs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workflow_stages",
        sa.Column(
            "operation_contract_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "workflow_jobs",
        sa.Column("project_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "workflow_jobs",
        sa.Column("project_revision_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_workflow_job_project_tenant",
        "workflow_jobs",
        "workflow_creative_projects",
        ["project_id", "owner_tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_workflow_job_project_revision_tenant",
        "workflow_jobs",
        "workflow_creative_project_revisions",
        ["project_revision_id", "project_id", "owner_tenant_id"],
        ["id", "project_id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_workflow_job_project_revision_pin_complete",
        "workflow_jobs",
        "(project_id IS NULL AND project_revision_id IS NULL) OR "
        "(project_id IS NOT NULL AND project_revision_id IS NOT NULL "
        "AND owner_tenant_id IS NOT NULL)",
    )
    op.create_unique_constraint(
        "uq_workflow_job_project_revision_pin",
        "workflow_jobs",
        ["id", "project_id", "project_revision_id", "owner_tenant_id"],
    )
    op.create_unique_constraint(
        "uq_workflow_workflow_job_identity",
        "workflow_workflows",
        ["id", "job_id"],
    )
    op.create_unique_constraint(
        "uq_workflow_attempt_stage_identity",
        "workflow_attempts",
        ["id", "stage_id"],
    )
    op.create_unique_constraint(
        "uq_workflow_artifact_execution_identity",
        "workflow_artifacts",
        ["id", "job_id", "stage_id", "attempt_id"],
    )
    op.create_unique_constraint(
        "uq_workflow_creative_asset_source_identity",
        "workflow_creative_assets",
        ["id", "project_id", "tenant_id", "artifact_id", "source_job_id"],
    )
    op.create_unique_constraint(
        "uq_workflow_creative_asset_content_identity",
        "workflow_creative_assets",
        ["id", "project_id", "tenant_id", "content_sha256"],
    )

    op.create_table(
        "workflow_creative_asset_provenance",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("artifact_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("stage_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("project_revision_id", sa.String(length=36), nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("operation_contract_version", sa.Integer(), nullable=False),
        sa.Column("workflow_version", sa.String(length=64), nullable=False),
        sa.Column("execution_plan_schema_version", sa.Integer(), nullable=False),
        sa.Column("execution_plan_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("prompt_config_revision_id", sa.String(length=128), nullable=False),
        sa.Column("validation_evidence_json", sa.JSON(), nullable=False),
        sa.Column("validation_evidence_sha256", sa.String(length=64), nullable=False),
        sa.Column("is_sealed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["asset_id", "project_id", "tenant_id", "artifact_id", "job_id"],
            [
                "workflow_creative_assets.id",
                "workflow_creative_assets.project_id",
                "workflow_creative_assets.tenant_id",
                "workflow_creative_assets.artifact_id",
                "workflow_creative_assets.source_job_id",
            ],
            name="fk_workflow_creative_provenance_asset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_revision_id", "project_id", "tenant_id"],
            [
                "workflow_creative_project_revisions.id",
                "workflow_creative_project_revisions.project_id",
                "workflow_creative_project_revisions.tenant_id",
            ],
            name="fk_workflow_creative_provenance_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "tenant_id"],
            ["workflow_jobs.id", "workflow_jobs.owner_tenant_id"],
            name="fk_workflow_creative_provenance_job_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "project_id", "project_revision_id", "tenant_id"],
            [
                "workflow_jobs.id",
                "workflow_jobs.project_id",
                "workflow_jobs.project_revision_id",
                "workflow_jobs.owner_tenant_id",
            ],
            name="fk_workflow_creative_provenance_job_revision_pin",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id", "job_id"],
            ["workflow_workflows.id", "workflow_workflows.job_id"],
            name="fk_workflow_creative_provenance_workflow_job",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["stage_id", "workflow_id"],
            ["workflow_stages.id", "workflow_stages.workflow_id"],
            name="fk_workflow_creative_provenance_stage_workflow",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id", "stage_id"],
            ["workflow_attempts.id", "workflow_attempts.stage_id"],
            name="fk_workflow_creative_provenance_attempt_stage",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id", "job_id", "stage_id", "attempt_id"],
            [
                "workflow_artifacts.id",
                "workflow_artifacts.job_id",
                "workflow_artifacts.stage_id",
                "workflow_artifacts.attempt_id",
            ],
            name="fk_workflow_creative_provenance_artifact_execution",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "operation_contract_version >= 1",
            name="ck_workflow_creative_provenance_operation_version",
        ),
        sa.CheckConstraint(
            "execution_plan_schema_version >= 1",
            name="ck_workflow_creative_provenance_plan_schema_version",
        ),
        sa.CheckConstraint(
            "length(execution_plan_fingerprint) = 64",
            name="ck_workflow_creative_provenance_plan_fingerprint",
        ),
        sa.CheckConstraint(
            "length(validation_evidence_sha256) = 64",
            name="ck_workflow_creative_provenance_validation_hash",
        ),
        sa.CheckConstraint(
            "(validation_evidence_json->>'status' = 'PASSED') IS TRUE",
            name="ck_workflow_creative_provenance_validation_passed",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "asset_id", "project_id", "tenant_id",
            name="uq_workflow_creative_asset_provenance_asset",
        ),
        sa.UniqueConstraint(
            "id", "project_id", "tenant_id",
            name="uq_workflow_creative_asset_provenance_identity",
        ),
    )
    op.create_index(
        "ix_workflow_creative_asset_provenance_project_created",
        "workflow_creative_asset_provenance",
        ["project_id", "tenant_id", "created_at"],
    )

    op.create_table(
        "workflow_creative_asset_provenance_parents",
        sa.Column("provenance_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("parent_asset_id", sa.String(length=36), nullable=False),
        sa.Column("parent_content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["provenance_id", "project_id", "tenant_id"],
            [
                "workflow_creative_asset_provenance.id",
                "workflow_creative_asset_provenance.project_id",
                "workflow_creative_asset_provenance.tenant_id",
            ],
            name="fk_workflow_creative_provenance_parent_provenance",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "parent_asset_id", "project_id", "tenant_id",
                "parent_content_sha256",
            ],
            [
                "workflow_creative_assets.id",
                "workflow_creative_assets.project_id",
                "workflow_creative_assets.tenant_id",
                "workflow_creative_assets.content_sha256",
            ],
            name="fk_workflow_creative_provenance_parent_asset_hash",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(parent_content_sha256) = 64",
            name="ck_workflow_creative_provenance_parent_hash",
        ),
        sa.PrimaryKeyConstraint("provenance_id", "parent_asset_id"),
    )
    op.create_index(
        "ix_workflow_creative_provenance_parents_project",
        "workflow_creative_asset_provenance_parents",
        ["project_id", "tenant_id", "parent_asset_id"],
    )

    op.execute("""
        CREATE FUNCTION workflow_guard_creative_provenance_header()
        RETURNS trigger AS $
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.is_sealed THEN
                    RAISE EXCEPTION 'creative asset provenance must be created unsealed';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'creative asset provenance is immutable';
            END IF;
            IF OLD.is_sealed IS FALSE
               AND NEW.is_sealed IS TRUE
               AND (to_jsonb(NEW) - 'is_sealed')
                   IS NOT DISTINCT FROM (to_jsonb(OLD) - 'is_sealed') THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'creative asset provenance is immutable';
        END;
        $ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_workflow_creative_asset_provenance_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON workflow_creative_asset_provenance
        FOR EACH ROW EXECUTE FUNCTION workflow_guard_creative_provenance_header()
    """)
    op.execute("""
        CREATE FUNCTION workflow_require_creative_provenance_sealed()
        RETURNS trigger AS $
        DECLARE
            current_sealed boolean;
        BEGIN
            SELECT is_sealed INTO current_sealed
            FROM workflow_creative_asset_provenance
            WHERE id = NEW.id;
            IF current_sealed IS DISTINCT FROM TRUE THEN
                RAISE EXCEPTION 'creative asset provenance must be sealed before commit';
            END IF;
            RETURN NULL;
        END;
        $ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER trg_workflow_creative_provenance_must_be_sealed
        AFTER INSERT OR UPDATE ON workflow_creative_asset_provenance
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION workflow_require_creative_provenance_sealed()
    """)
    op.execute("""
        CREATE FUNCTION workflow_guard_creative_provenance_parent_insert()
        RETURNS trigger AS $
        DECLARE
            child_asset_id varchar(36);
            header_sealed boolean;
            creates_cycle boolean;
        BEGIN
            PERFORM 1
            FROM workflow_creative_projects
            WHERE id = NEW.project_id AND tenant_id = NEW.tenant_id
            FOR UPDATE;
            SELECT asset_id, is_sealed
            INTO child_asset_id, header_sealed
            FROM workflow_creative_asset_provenance
            WHERE id = NEW.provenance_id
              AND project_id = NEW.project_id
              AND tenant_id = NEW.tenant_id
            FOR UPDATE;
            IF NOT FOUND OR header_sealed THEN
                RAISE EXCEPTION 'creative asset provenance parent set is sealed';
            END IF;
            WITH RECURSIVE ancestors(asset_id) AS (
                SELECT NEW.parent_asset_id
                UNION
                SELECT lineage_parent.parent_asset_id
                FROM ancestors
                JOIN workflow_creative_asset_provenance child
                  ON child.asset_id = ancestors.asset_id
                JOIN workflow_creative_asset_provenance_parents lineage_parent
                  ON lineage_parent.provenance_id = child.id
            )
            SELECT EXISTS(
                SELECT 1 FROM ancestors WHERE asset_id = child_asset_id
            ) INTO creates_cycle;
            IF creates_cycle THEN
                RAISE EXCEPTION 'creative asset provenance lineage cycle detected';
            END IF;
            RETURN NEW;
        END;
        $ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_workflow_creative_provenance_parent_insert_guard
        BEFORE INSERT ON workflow_creative_asset_provenance_parents
        FOR EACH ROW EXECUTE FUNCTION workflow_guard_creative_provenance_parent_insert()
    """)
    op.execute("""
        CREATE FUNCTION workflow_reject_creative_provenance_parent_mutation()
        RETURNS trigger AS $
        BEGIN
            RAISE EXCEPTION 'creative asset provenance is immutable';
        END;
        $ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_workflow_creative_asset_provenance_parents_immutable
        BEFORE UPDATE OR DELETE ON workflow_creative_asset_provenance_parents
        FOR EACH ROW EXECUTE FUNCTION workflow_reject_creative_provenance_parent_mutation()
    """)
    op.execute("""
        CREATE FUNCTION workflow_reject_job_project_revision_pin_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.project_id IS DISTINCT FROM OLD.project_id
               OR NEW.project_revision_id IS DISTINCT FROM OLD.project_revision_id THEN
                RAISE EXCEPTION 'Job ProjectRevision pin is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_workflow_job_project_revision_pin_immutable
        BEFORE UPDATE ON workflow_jobs
        FOR EACH ROW EXECUTE FUNCTION workflow_reject_job_project_revision_pin_mutation()
    """)
    op.execute("""
        CREATE FUNCTION workflow_reject_stage_operation_contract_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.operation IS DISTINCT FROM OLD.operation
               OR NEW.operation_contract_version
                  IS DISTINCT FROM OLD.operation_contract_version THEN
                RAISE EXCEPTION 'stage operation contract identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_workflow_stage_operation_contract_immutable
        BEFORE UPDATE ON workflow_stages
        FOR EACH ROW EXECUTE FUNCTION workflow_reject_stage_operation_contract_mutation()
    """)


def downgrade():
    op.execute(
        "LOCK TABLE workflow_jobs, workflow_stages, "
        "workflow_creative_asset_provenance, "
        "workflow_creative_asset_provenance_parents "
        "IN SHARE ROW EXCLUSIVE MODE"
    )
    count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM workflow_creative_asset_provenance"
    )).scalar_one()
    if count:
        # Parent rows have a non-null FK to a provenance row, so checking the
        # owning table is sufficient to protect both levels of the lineage.
        raise RuntimeError(
            "Refusing to drop asset provenance while records exist"
        )
    pinned_jobs = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM workflow_jobs "
        "WHERE project_revision_id IS NOT NULL"
    )).scalar_one()
    if pinned_jobs:
        raise RuntimeError(
            "Refusing to drop Job ProjectRevision pins while pinned Jobs exist"
        )

    versioned_stages = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM workflow_stages "
        "WHERE operation_contract_version <> 1"
    )).scalar_one()
    if versioned_stages:
        raise RuntimeError(
            "Refusing to drop operation contract versions after they have advanced"
        )

    op.execute(
        "DROP TRIGGER trg_workflow_job_project_revision_pin_immutable "
        "ON workflow_jobs"
    )
    op.execute("DROP FUNCTION workflow_reject_job_project_revision_pin_mutation()")
    op.execute(
        "DROP TRIGGER trg_workflow_stage_operation_contract_immutable "
        "ON workflow_stages"
    )
    op.execute(
        "DROP FUNCTION workflow_reject_stage_operation_contract_mutation()"
    )
    op.execute(
        "DROP TRIGGER trg_workflow_creative_asset_provenance_parents_immutable "
        "ON workflow_creative_asset_provenance_parents"
    )
    op.execute(
        "DROP TRIGGER trg_workflow_creative_provenance_parent_insert_guard "
        "ON workflow_creative_asset_provenance_parents"
    )
    op.execute("DROP FUNCTION workflow_reject_creative_provenance_parent_mutation()")
    op.execute("DROP FUNCTION workflow_guard_creative_provenance_parent_insert()")
    op.execute(
        "DROP TRIGGER trg_workflow_creative_provenance_must_be_sealed "
        "ON workflow_creative_asset_provenance"
    )
    op.execute("DROP FUNCTION workflow_require_creative_provenance_sealed()")
    op.execute(
        "DROP TRIGGER trg_workflow_creative_asset_provenance_immutable "
        "ON workflow_creative_asset_provenance"
    )
    op.execute("DROP FUNCTION workflow_guard_creative_provenance_header()")

    op.drop_index(
        "ix_workflow_creative_provenance_parents_project",
        table_name="workflow_creative_asset_provenance_parents",
    )
    op.drop_table("workflow_creative_asset_provenance_parents")
    op.drop_index(
        "ix_workflow_creative_asset_provenance_project_created",
        table_name="workflow_creative_asset_provenance",
    )
    op.drop_table("workflow_creative_asset_provenance")
    op.drop_constraint(
        "uq_workflow_creative_asset_content_identity",
        "workflow_creative_assets",
        type_="unique",
    )
    op.drop_constraint(
        "uq_workflow_creative_asset_source_identity",
        "workflow_creative_assets",
        type_="unique",
    )
    op.drop_constraint(
        "uq_workflow_artifact_execution_identity",
        "workflow_artifacts",
        type_="unique",
    )
    op.drop_constraint(
        "uq_workflow_attempt_stage_identity",
        "workflow_attempts",
        type_="unique",
    )
    op.drop_constraint(
        "uq_workflow_workflow_job_identity",
        "workflow_workflows",
        type_="unique",
    )
    op.drop_constraint(
        "uq_workflow_job_project_revision_pin",
        "workflow_jobs",
        type_="unique",
    )
    op.drop_constraint(
        "ck_workflow_job_project_revision_pin_complete",
        "workflow_jobs",
        type_="check",
    )
    op.drop_constraint(
        "fk_workflow_job_project_revision_tenant",
        "workflow_jobs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_workflow_job_project_tenant",
        "workflow_jobs",
        type_="foreignkey",
    )
    op.drop_column("workflow_jobs", "project_revision_id")
    op.drop_column("workflow_jobs", "project_id")
    op.drop_column("workflow_stages", "operation_contract_version")
