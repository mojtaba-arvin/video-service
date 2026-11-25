"""Add typed project entities and immutable revision membership references.

Revision ID: 0014_creative_project_typed_references
Revises: 0013_creative_project_revisions
"""
from alembic import op
import sqlalchemy as sa


revision = "0014_project_refs"
down_revision = "0013_creative_project_revisions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        "uq_workflow_jobs_tenant_identity",
        "workflow_jobs",
        ["id", "owner_tenant_id"],
    )
    op.create_unique_constraint(
        "uq_workflow_artifact_job_identity",
        "workflow_artifacts",
        ["id", "job_id"],
    )

    def project_fk(name):
        return sa.ForeignKeyConstraint(
            ["project_id", "tenant_id"],
            [
                "workflow_creative_projects.id",
                "workflow_creative_projects.tenant_id",
            ],
            name=name,
            ondelete="RESTRICT",
        )

    def entity_columns():
        return [
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("project_id", sa.String(length=36), nullable=False),
            sa.Column("tenant_id", sa.String(length=255), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_by_issuer", sa.String(length=512), nullable=False),
            sa.Column("created_by_subject", sa.String(length=255), nullable=False),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.func.now(),
            ),
        ]

    op.create_table(
        "workflow_creative_scenes",
        *entity_columns(),
        project_fk("fk_workflow_creative_scene_project"),
        sa.CheckConstraint(
            "sort_order >= 0", name="ck_workflow_creative_scene_order"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "project_id", "tenant_id",
            name="uq_workflow_creative_scene_project_identity",
        ),
    )
    op.create_index(
        "ix_workflow_creative_scenes_project_order",
        "workflow_creative_scenes",
        ["project_id", "tenant_id", "sort_order"],
    )

    shot_columns = [
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("scene_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by_issuer", sa.String(length=512), nullable=False),
        sa.Column("created_by_subject", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    ]
    op.create_table(
        "workflow_creative_shots",
        *shot_columns,
        project_fk("fk_workflow_creative_shot_project"),
        sa.ForeignKeyConstraint(
            ["scene_id", "project_id", "tenant_id"],
            [
                "workflow_creative_scenes.id",
                "workflow_creative_scenes.project_id",
                "workflow_creative_scenes.tenant_id",
            ],
            name="fk_workflow_creative_shot_scene_project",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "sort_order >= 0", name="ck_workflow_creative_shot_order"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "project_id", "tenant_id",
            name="uq_workflow_creative_shot_project_identity",
        ),
    )
    op.create_index(
        "ix_workflow_creative_shots_scene_order",
        "workflow_creative_shots",
        ["scene_id", "project_id", "tenant_id", "sort_order"],
    )

    op.create_table(
        "workflow_creative_characters",
        *entity_columns(),
        project_fk("fk_workflow_creative_character_project"),
        sa.CheckConstraint(
            "sort_order >= 0", name="ck_workflow_creative_character_order"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "project_id", "tenant_id",
            name="uq_workflow_creative_character_project_identity",
        ),
    )
    op.create_index(
        "ix_workflow_creative_characters_project_order",
        "workflow_creative_characters",
        ["project_id", "tenant_id", "sort_order"],
    )

    op.create_table(
        "workflow_creative_assets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("artifact_id", sa.String(length=36), nullable=False),
        sa.Column("source_job_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("asset_type", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by_issuer", sa.String(length=512), nullable=False),
        sa.Column("created_by_subject", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        project_fk("fk_workflow_creative_asset_project"),
        sa.ForeignKeyConstraint(
            ["source_job_id", "tenant_id"],
            ["workflow_jobs.id", "workflow_jobs.owner_tenant_id"],
            name="fk_workflow_creative_asset_source_job_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id", "source_job_id"],
            ["workflow_artifacts.id", "workflow_artifacts.job_id"],
            name="fk_workflow_creative_asset_artifact_job",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "sort_order >= 0", name="ck_workflow_creative_asset_order"
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_workflow_creative_asset_hash_length",
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0",
            name="ck_workflow_creative_asset_size",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "project_id", "tenant_id",
            name="uq_workflow_creative_asset_project_identity",
        ),
        sa.UniqueConstraint(
            "artifact_id", "project_id", "tenant_id",
            name="uq_workflow_creative_asset_artifact_project",
        ),
    )
    op.create_index(
        "ix_workflow_creative_assets_project_order",
        "workflow_creative_assets",
        ["project_id", "tenant_id", "sort_order"],
    )

    op.create_table(
        "workflow_creative_project_revision_references",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("scene_id", sa.String(length=36), nullable=True),
        sa.Column("shot_id", sa.String(length=36), nullable=True),
        sa.Column("character_id", sa.String(length=36), nullable=True),
        sa.Column("asset_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["revision_id", "project_id", "tenant_id"],
            [
                "workflow_creative_project_revisions.id",
                "workflow_creative_project_revisions.project_id",
                "workflow_creative_project_revisions.tenant_id",
            ],
            name="fk_workflow_project_revision_reference_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scene_id", "project_id", "tenant_id"],
            [
                "workflow_creative_scenes.id",
                "workflow_creative_scenes.project_id",
                "workflow_creative_scenes.tenant_id",
            ],
            name="fk_workflow_project_revision_reference_scene",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["shot_id", "project_id", "tenant_id"],
            [
                "workflow_creative_shots.id",
                "workflow_creative_shots.project_id",
                "workflow_creative_shots.tenant_id",
            ],
            name="fk_workflow_project_revision_reference_shot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["character_id", "project_id", "tenant_id"],
            [
                "workflow_creative_characters.id",
                "workflow_creative_characters.project_id",
                "workflow_creative_characters.tenant_id",
            ],
            name="fk_workflow_project_revision_reference_character",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id", "project_id", "tenant_id"],
            [
                "workflow_creative_assets.id",
                "workflow_creative_assets.project_id",
                "workflow_creative_assets.tenant_id",
            ],
            name="fk_workflow_project_revision_reference_asset",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "(CASE WHEN scene_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN shot_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN character_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN asset_id IS NULL THEN 0 ELSE 1 END) = 1",
            name="ck_workflow_project_revision_reference_one_type",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "revision_id", "scene_id",
            name="uq_workflow_project_revision_reference_scene",
        ),
        sa.UniqueConstraint(
            "revision_id", "shot_id",
            name="uq_workflow_project_revision_reference_shot",
        ),
        sa.UniqueConstraint(
            "revision_id", "character_id",
            name="uq_workflow_project_revision_reference_character",
        ),
        sa.UniqueConstraint(
            "revision_id", "asset_id",
            name="uq_workflow_project_revision_reference_asset",
        ),
    )
    op.create_index(
        "ix_workflow_project_revision_references_revision",
        "workflow_creative_project_revision_references",
        ["revision_id", "project_id", "tenant_id"],
    )

    op.execute("""
        CREATE FUNCTION workflow_reject_creative_entity_identity_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.project_id IS DISTINCT FROM OLD.project_id
               OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
               OR NEW.created_by_issuer IS DISTINCT FROM OLD.created_by_issuer
               OR NEW.created_by_subject IS DISTINCT FROM OLD.created_by_subject
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'creative entity identity is immutable';
            END IF;
            IF TG_TABLE_NAME = 'workflow_creative_shots'
               AND NEW.scene_id IS DISTINCT FROM OLD.scene_id THEN
                RAISE EXCEPTION 'creative shot scene identity is immutable';
            END IF;
            IF TG_TABLE_NAME = 'workflow_creative_assets'
               AND (
                   NEW.artifact_id IS DISTINCT FROM OLD.artifact_id
                   OR NEW.source_job_id IS DISTINCT FROM OLD.source_job_id
                   OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256
                   OR NEW.asset_type IS DISTINCT FROM OLD.asset_type
                   OR NEW.media_type IS DISTINCT FROM OLD.media_type
                   OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
               ) THEN
                RAISE EXCEPTION 'creative asset source identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    for table, trigger in (
        ("workflow_creative_scenes", "trg_workflow_creative_scene_identity"),
        ("workflow_creative_shots", "trg_workflow_creative_shot_identity"),
        ("workflow_creative_characters", "trg_workflow_creative_character_identity"),
        ("workflow_creative_assets", "trg_workflow_creative_asset_identity"),
    ):
        op.execute(
            "CREATE TRIGGER {} BEFORE UPDATE ON {} "
            "FOR EACH ROW EXECUTE FUNCTION "
            "workflow_reject_creative_entity_identity_mutation()".format(
                trigger, table
            )
        )

    op.execute("""
        CREATE FUNCTION workflow_reject_project_revision_reference_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'creative project revision references are immutable';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_workflow_project_revision_references_immutable
        BEFORE UPDATE OR DELETE ON workflow_creative_project_revision_references
        FOR EACH ROW EXECUTE FUNCTION workflow_reject_project_revision_reference_mutation()
    """)


def downgrade():
    checks = (
        (
            "workflow_creative_project_revision_references",
            "creative project revision references",
        ),
        ("workflow_creative_assets", "creative project assets"),
        ("workflow_creative_characters", "creative project characters"),
        ("workflow_creative_shots", "creative project shots"),
        ("workflow_creative_scenes", "creative project scenes"),
    )
    for table, description in checks:
        count = op.get_bind().execute(sa.text(
            "SELECT COUNT(*) FROM {}".format(table)
        )).scalar_one()
        if count:
            raise RuntimeError(
                "Refusing to drop {} while records exist".format(description)
            )

    op.execute(
        "DROP TRIGGER trg_workflow_project_revision_references_immutable "
        "ON workflow_creative_project_revision_references"
    )
    op.execute(
        "DROP FUNCTION workflow_reject_project_revision_reference_mutation()"
    )
    for table, trigger in (
        ("workflow_creative_scenes", "trg_workflow_creative_scene_identity"),
        ("workflow_creative_shots", "trg_workflow_creative_shot_identity"),
        ("workflow_creative_characters", "trg_workflow_creative_character_identity"),
        ("workflow_creative_assets", "trg_workflow_creative_asset_identity"),
    ):
        op.execute("DROP TRIGGER {} ON {}".format(trigger, table))
    op.execute("DROP FUNCTION workflow_reject_creative_entity_identity_mutation()")

    op.drop_index(
        "ix_workflow_project_revision_references_revision",
        table_name="workflow_creative_project_revision_references",
    )
    op.drop_table("workflow_creative_project_revision_references")
    op.drop_index(
        "ix_workflow_creative_assets_project_order",
        table_name="workflow_creative_assets",
    )
    op.drop_table("workflow_creative_assets")
    op.drop_index(
        "ix_workflow_creative_characters_project_order",
        table_name="workflow_creative_characters",
    )
    op.drop_table("workflow_creative_characters")
    op.drop_index(
        "ix_workflow_creative_shots_scene_order",
        table_name="workflow_creative_shots",
    )
    op.drop_table("workflow_creative_shots")
    op.drop_index(
        "ix_workflow_creative_scenes_project_order",
        table_name="workflow_creative_scenes",
    )
    op.drop_table("workflow_creative_scenes")
    op.drop_constraint(
        "uq_workflow_artifact_job_identity",
        "workflow_artifacts",
        type_="unique",
    )
    op.drop_constraint(
        "uq_workflow_jobs_tenant_identity",
        "workflow_jobs",
        type_="unique",
    )
