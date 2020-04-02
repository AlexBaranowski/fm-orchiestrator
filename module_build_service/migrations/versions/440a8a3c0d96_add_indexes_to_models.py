"""Add indexes to models

Revision ID: 440a8a3c0d96
Revises: 4d1e2e13e514
Create Date: 2020-02-04 17:15:37.150756

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "440a8a3c0d96"
down_revision = "4d1e2e13e514"


def upgrade():
    op.create_index(
        "idx_component_builds_build_id_nvr",
        "component_builds",
        ["module_id", "nvr"],
        unique=True,
    )
    op.create_index(
        "idx_component_builds_build_id_task_id",
        "component_builds",
        ["module_id", "task_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_component_builds_batch"), "component_builds", ["batch"], unique=False
    )
    op.create_index(
        "idx_module_builds_name_stream_version_context",
        "module_builds",
        ["name", "stream", "version", "context"],
        unique=True,
    )
    op.create_index(
        op.f("ix_module_builds_name"), "module_builds", ["name"], unique=False
    )
    op.create_index(
        op.f("ix_module_builds_state"), "module_builds", ["state"], unique=False
    )
    op.create_index(
        op.f("ix_module_builds_koji_tag"), "module_builds", ["koji_tag"], unique=False
    )


def downgrade():
    op.drop_index(op.f("ix_module_builds_koji_tag"), table_name="module_builds")
    op.drop_index(op.f("ix_module_builds_state"), table_name="module_builds")
    op.drop_index(op.f("ix_module_builds_name"), table_name="module_builds")
    op.drop_index(
        "idx_module_builds_name_stream_version_context", table_name="module_builds"
    )
    op.drop_index(op.f("ix_component_builds_batch"), table_name="component_builds")
    op.drop_index(
        "idx_component_builds_build_id_task_id", table_name="component_builds"
    )
    op.drop_index("idx_component_builds_build_id_nvr", table_name="component_builds")
