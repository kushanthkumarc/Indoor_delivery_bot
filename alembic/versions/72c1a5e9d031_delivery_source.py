"""Persist mapped pickup locations and pickup navigation states."""
from alembic import op
import sqlalchemy as sa

revision = "72c1a5e9d031"
down_revision = "340c2e71c85c"
branch_labels = None
depends_on = None

OLD_STATES = ("IDLE", "GO_TO_DESTINATION", "ARRIVED", "DELIVERED", "RETURN_HOME", "HOME", "EMERGENCY_STOP")
NEW_STATES = OLD_STATES + ("GO_TO_SOURCE", "AT_SOURCE")


def upgrade():
    op.add_column("deliveries", sa.Column("source_id", sa.CHAR(36), nullable=True))
    op.create_foreign_key("fk_deliveries_source_id", "deliveries", "destinations", ["source_id"], ["id"], ondelete="RESTRICT")
    op.alter_column("deliveries", "status", existing_type=sa.Enum(*OLD_STATES, name="deliverystatus"),
                    type_=sa.Enum(*NEW_STATES, name="deliverystatus"), existing_nullable=False)


def downgrade():
    # Refuse to silently rewrite in-progress pickup deliveries during downgrade.
    count = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM deliveries WHERE status IN ('GO_TO_SOURCE','AT_SOURCE')")).scalar()
    if count:
        raise RuntimeError("Resolve pickup deliveries before downgrading")
    op.alter_column("deliveries", "status", existing_type=sa.Enum(*NEW_STATES, name="deliverystatus"),
                    type_=sa.Enum(*OLD_STATES, name="deliverystatus"), existing_nullable=False)
    op.drop_constraint("fk_deliveries_source_id", "deliveries", type_="foreignkey")
    op.drop_column("deliveries", "source_id")
