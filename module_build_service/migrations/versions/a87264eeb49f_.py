"""Add build_context_no_bms

Revision ID: a97269751cb4
Revises: 1817e62719f9
Create Date: 2019-10-01 13:59:48.947851

"""

# revision identifiers, used by Alembic.
revision = "a87264eeb49f"
down_revision = "1817e62719f9"

from alembic import op
import sqlalchemy as sa
from module_build_service.common.models import ModuleBuild
from module_build_service.common.modulemd import Modulemd


modulebuild = sa.Table(
    "module_builds",
    sa.MetaData(),
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("modulemd", sa.String()),
    sa.Column("build_context_no_bms", sa.String()),
)


def upgrade():
    connection = op.get_bind()
    op.add_column("module_builds", sa.Column("build_context_no_bms", sa.String(), nullable=True))

    # Determine what the contexts should be based on the modulemd
    for build in connection.execute(modulebuild.select()):
        if not build.modulemd:
            continue
        try:
            mmd = Modulemd.ModuleStream.read_string(build.modulemd, True)
            mmd = mmd.upgrade(Modulemd.ModuleStreamVersionEnum.TWO)
        except Exception:
            # If the modulemd isn"t parseable then skip this build
            continue

        # It's possible this module build was built before MBS filled out xmd or before MBS
        # filled out the requires in xmd
        mbs_xmd_buildrequires = mmd.get_xmd().get("mbs", {}).get("buildrequires")
        if not mbs_xmd_buildrequires:
            continue
        build_context_no_bms = ModuleBuild.calculate_build_context(mbs_xmd_buildrequires, True)
        connection.execute(
            modulebuild.update().where(modulebuild.c.id == build.id).values(
                build_context_no_bms=build_context_no_bms))


def downgrade():
    with op.batch_alter_table("module_builds", schema=None) as batch_op:
        batch_op.drop_column("build_context_no_bms")
