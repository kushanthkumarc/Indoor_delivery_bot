"""initial_schema

Revision ID: 6aba335e
Revises:
Create Date: 2026-09-28

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6aba335e"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# CHAR(36) is used for UUIDs — MySQL has no native UUID type.
# JSON is used instead of PostgreSQL's JSONB.


def upgrade() -> None:
    # --- users ---
    op.create_table(
        "users",
        sa.Column("id", sa.CHAR(36), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column(
            "role",
            sa.Enum("ADMIN", "USER", name="userrole"),
            nullable=False,
            server_default="USER",
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_email", "users", ["email"])

    # --- robots ---
    op.create_table(
        "robots",
        sa.Column("id", sa.CHAR(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("api_key_hash", sa.String(255), nullable=False),
        sa.Column("status", sa.Enum("ONLINE", "OFFLINE", name="robotstatus"), nullable=False, server_default="OFFLINE"),
        sa.Column("control_mode", sa.Enum("MANUAL", "AUTONOMOUS", "STOPPED", name="controlmode"), nullable=False, server_default="STOPPED"),
        sa.Column("safety_state", sa.Enum("SAFE", "SLOW", "REPLAN", "EMERGENCY_STOP", name="safetystate"), nullable=False, server_default="SAFE"),
        sa.Column("slam_state", sa.Enum("TRACKING", "LOST", "RELOCATING", "IDLE", name="slamstate"), nullable=False, server_default="IDLE"),
        sa.Column("battery_level", sa.Float(), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("firmware_version", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # --- mapping_sessions ---
    op.create_table(
        "mapping_sessions",
        sa.Column("id", sa.CHAR(36), primary_key=True),
        sa.Column("robot_id", sa.CHAR(36), sa.ForeignKey("robots.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.Enum("IDLE", "MAPPING", "PAUSED", "FINISHED", name="mappingsessionstatus"), nullable=False, server_default="IDLE"),
        sa.Column("elapsed_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("pose_update_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_pose_json", sa.JSON(), nullable=True),
        sa.Column("slam_status", sa.Enum("TRACKING", "LOST", "RELOCATING", name="slamstatusenum"), nullable=True),
        sa.Column("map_data_ref", sa.String(500), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_mapping_sessions_robot_id", "mapping_sessions", ["robot_id"])

    # --- destinations ---
    op.create_table(
        "destinations",
        sa.Column("id", sa.CHAR(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("map_id", sa.CHAR(36), sa.ForeignKey("mapping_sessions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("x", sa.Float(), nullable=False),
        sa.Column("y", sa.Float(), nullable=False),
        sa.Column("theta", sa.Float(), nullable=False),
        sa.Column("is_home", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_destinations_map_id", "destinations", ["map_id"])

    # --- deliveries ---
    op.create_table(
        "deliveries",
        sa.Column("id", sa.CHAR(36), primary_key=True),
        sa.Column("robot_id", sa.CHAR(36), sa.ForeignKey("robots.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("destination_id", sa.CHAR(36), sa.ForeignKey("destinations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("requester_id", sa.CHAR(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column(
            "status",
            sa.Enum("IDLE", "GO_TO_DESTINATION", "ARRIVED", "DELIVERED", "RETURN_HOME", "HOME", "EMERGENCY_STOP", name="deliverystatus"),
            nullable=False,
            server_default="IDLE",
        ),
        sa.Column("state_history_json", sa.JSON(), nullable=False),
        sa.Column("nav_goal_id", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_deliveries_robot_id", "deliveries", ["robot_id"])
    op.create_index("ix_deliveries_requester_id", "deliveries", ["requester_id"])
    op.create_index("ix_deliveries_status", "deliveries", ["status"])

    # --- events (append-only audit log) ---
    op.create_table(
        "events",
        sa.Column("id", sa.CHAR(36), primary_key=True),
        sa.Column("robot_id", sa.CHAR(36), sa.ForeignKey("robots.id", ondelete="SET NULL"), nullable=True),
        sa.Column("user_id", sa.CHAR(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("type", sa.String(100), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("severity", sa.Enum("INFO", "WARN", "ERROR", "CRITICAL", name="eventseverity"), nullable=False, server_default="INFO"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_events_robot_id", "events", ["robot_id"])
    op.create_index("ix_events_user_id", "events", ["user_id"])
    op.create_index("ix_events_severity", "events", ["severity"])
    op.create_index("ix_events_type", "events", ["type"])
    op.create_index("ix_events_created_at", "events", ["created_at"])

    # --- Append-only protection for events table (Requirement 9.6) ---
    # MySQL triggers raise an error on UPDATE/DELETE, unlike PostgreSQL rules
    # which silently swallow the operation — this is stricter and preferable.
    op.execute("""
        CREATE TRIGGER events_no_update
        BEFORE UPDATE ON events
        FOR EACH ROW
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'events table is append-only: UPDATE not allowed';
    """)
    op.execute("""
        CREATE TRIGGER events_no_delete
        BEFORE DELETE ON events
        FOR EACH ROW
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'events table is append-only: DELETE not allowed';
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS events_no_delete;")
    op.execute("DROP TRIGGER IF EXISTS events_no_update;")

    op.drop_table("events")
    op.drop_table("deliveries")
    op.drop_table("destinations")
    op.drop_table("mapping_sessions")
    op.drop_table("robots")
    op.drop_table("users")
