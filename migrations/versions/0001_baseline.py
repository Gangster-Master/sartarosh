"""baseline

Revision ID: 0001
Revises:
Create Date: 2026-10-24 13:35:23.196725
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fsm_states",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=128), nullable=True),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_fsm_states")),
    )
    op.create_table(
        "services",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("duration_min", sa.Integer(), nullable=False),
        sa.Column("price_uzs", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "duration_min > 0 AND duration_min % 15 = 0 AND duration_min <= 240",
            name=op.f("ck_services_duration"),
        ),
        sa.CheckConstraint("price_uzs >= 0", name=op.f("ck_services_price")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_services")),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tg_id", sa.BigInteger(), nullable=False),
        sa.Column("tg_username", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=128), nullable=False),
        sa.Column("full_name", sa.String(length=64), nullable=True),
        sa.Column("phone", sa.String(length=16), nullable=True),
        sa.Column("phone_verified", sa.Boolean(), nullable=False),
        sa.Column("lang", sa.String(length=5), nullable=False),
        sa.Column("lang_chosen", sa.Boolean(), nullable=False),
        sa.Column("consent_version", sa.Integer(), nullable=True),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("is_blocked", sa.Boolean(), nullable=False),
        sa.Column("no_show_count", sa.Integer(), nullable=False),
        sa.Column("late_cancel_count", sa.Integer(), nullable=False),
        sa.Column("no_show_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bonus_balance", sa.Integer(), nullable=False),
        sa.Column("ref_code", sa.String(length=12), nullable=False),
        sa.Column("referrer_id", sa.Integer(), nullable=True),
        sa.Column("acq_source", sa.String(length=32), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["referrer_id"],
            ["users.id"],
            name=op.f("fk_users_referrer_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("ref_code", name=op.f("uq_users_ref_code")),
        sa.UniqueConstraint("tg_id", name=op.f("uq_users_tg_id")),
    )
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_users_phone"), ["phone"], unique=False)

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_audit_log_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.create_index("ix_audit_log_created_at", ["created_at"], unique=False)

    op.create_table(
        "barbers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("photo_file_id", sa.String(length=256), nullable=True),
        sa.Column("bio", sa.String(length=300), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("slot_step_min", sa.Integer(), nullable=False),
        sa.Column("buffer_min", sa.Integer(), nullable=False),
        sa.Column("auto_accept", sa.Boolean(), nullable=False),
        sa.Column("invite_token", sa.String(length=32), nullable=True),
        sa.Column("invite_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending_link_user_id", sa.Integer(), nullable=True),
        sa.Column("is_reachable", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("buffer_min IN (0, 15, 30)", name=op.f("ck_barbers_buffer")),
        sa.CheckConstraint("slot_step_min IN (15, 30, 45, 60)", name=op.f("ck_barbers_slot_step")),
        sa.ForeignKeyConstraint(
            ["pending_link_user_id"],
            ["users.id"],
            name=op.f("fk_barbers_pending_link_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_barbers_user_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_barbers")),
        sa.UniqueConstraint("invite_token", name=op.f("uq_barbers_invite_token")),
        sa.UniqueConstraint("user_id", name=op.f("uq_barbers_user_id")),
    )
    op.create_table(
        "barber_services",
        sa.Column("barber_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("price_uzs", sa.Integer(), nullable=True),
        sa.Column("duration_min", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "duration_min IS NULL OR (duration_min > 0 AND duration_min % 15 = 0 AND duration_min <= 240)",
            name=op.f("ck_barber_services_duration"),
        ),
        sa.CheckConstraint(
            "price_uzs IS NULL OR price_uzs >= 0", name=op.f("ck_barber_services_price")
        ),
        sa.ForeignKeyConstraint(
            ["barber_id"],
            ["barbers.id"],
            name=op.f("fk_barber_services_barber_id_barbers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_barber_services_service_id_services"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("barber_id", "service_id", name=op.f("pk_barber_services")),
    )
    op.create_table(
        "bookings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=True),
        sa.Column("barber_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("service_name_snapshot", sa.String(length=64), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("block_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_min", sa.Integer(), nullable=False),
        sa.Column("price_snapshot", sa.Integer(), nullable=False),
        sa.Column("bonus_cost", sa.Integer(), nullable=False),
        sa.Column("final_price_uzs", sa.Integer(), nullable=False),
        sa.Column("bonus_redeemed", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("awaiting", sa.String(length=8), nullable=True),
        sa.Column("proposal_count", sa.Integer(), nullable=False),
        sa.Column("original_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("client_name", sa.String(length=64), nullable=False),
        sa.Column("client_phone", sa.String(length=16), nullable=True),
        sa.Column("client_phone_verified", sa.Boolean(), nullable=False),
        sa.Column("comment", sa.String(length=300), nullable=True),
        sa.Column("expire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("late_cancel", sa.Boolean(), nullable=False),
        sa.Column("client_ack_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_completed", sa.Boolean(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("visit_confirmed", sa.Boolean(), nullable=True),
        sa.Column("cancelled_by", sa.String(length=8), nullable=True),
        sa.Column("cancel_reason", sa.String(length=200), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("end_at > start_at", name=op.f("ck_bookings_end_after_start")),
        sa.CheckConstraint("proposal_count <= 2", name=op.f("ck_bookings_proposal_count")),
        sa.ForeignKeyConstraint(
            ["barber_id"],
            ["barbers.id"],
            name=op.f("fk_bookings_barber_id_barbers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["users.id"],
            name=op.f("fk_bookings_client_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_bookings_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_bookings_service_id_services"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookings")),
    )
    with op.batch_alter_table("bookings", schema=None) as batch_op:
        batch_op.create_index(
            "ix_bookings_barber_id_start_at", ["barber_id", "start_at"], unique=False
        )
        batch_op.create_index(
            "ix_bookings_client_id_start_at", ["client_id", "start_at"], unique=False
        )
        batch_op.create_index("ix_bookings_client_phone", ["client_phone"], unique=False)
        batch_op.create_index(batch_op.f("ix_bookings_status"), ["status"], unique=False)
        batch_op.create_index("ix_bookings_status_expire_at", ["status", "expire_at"], unique=False)

    op.create_table(
        "time_off",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=False),
        sa.Column("barber_id", sa.Integer(), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("end_at > start_at", name=op.f("ck_time_off_end_after_start")),
        sa.ForeignKeyConstraint(
            ["barber_id"],
            ["barbers.id"],
            name=op.f("fk_time_off_barber_id_barbers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_time_off_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_time_off")),
    )
    with op.batch_alter_table("time_off", schema=None) as batch_op:
        batch_op.create_index(
            "ix_time_off_barber_id_start_at", ["barber_id", "start_at"], unique=False
        )

    op.create_table(
        "working_hours",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("barber_id", sa.Integer(), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("break_start", sa.Time(), nullable=True),
        sa.Column("break_end", sa.Time(), nullable=True),
        sa.CheckConstraint("start_time < end_time", name=op.f("ck_working_hours_start_end")),
        sa.CheckConstraint("weekday >= 0 AND weekday <= 6", name=op.f("ck_working_hours_weekday")),
        sa.ForeignKeyConstraint(
            ["barber_id"],
            ["barbers.id"],
            name=op.f("fk_working_hours_barber_id_barbers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_working_hours")),
        sa.UniqueConstraint(
            "barber_id", "weekday", name=op.f("uq_working_hours_barber_id_weekday")
        ),
    )
    op.create_table(
        "bonus_transactions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=True),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("unit", sa.String(length=8), nullable=False),
        sa.Column("reason", sa.String(length=10), nullable=False),
        sa.Column("earn_day", sa.Date(), nullable=True),
        sa.Column("comment", sa.String(length=200), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("delta <> 0", name=op.f("ck_bonus_transactions_delta_nonzero")),
        sa.ForeignKeyConstraint(
            ["booking_id"],
            ["bookings.id"],
            name=op.f("fk_bonus_transactions_booking_id_bookings"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_bonus_transactions_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_bonus_transactions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bonus_transactions")),
        sa.UniqueConstraint(
            "user_id",
            "booking_id",
            "reason",
            name=op.f("uq_bonus_transactions_user_id_booking_id_reason"),
        ),
        sa.UniqueConstraint(
            "user_id", "earn_day", name=op.f("uq_bonus_transactions_user_id_earn_day")
        ),
    )
    with op.batch_alter_table("bonus_transactions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_bonus_transactions_user_id"), ["user_id"], unique=False
        )

    op.create_table(
        "booking_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["booking_id"],
            ["bookings.id"],
            name=op.f("fk_booking_messages_booking_id_bookings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_booking_messages")),
        sa.UniqueConstraint(
            "chat_id", "message_id", name=op.f("uq_booking_messages_chat_id_message_id")
        ),
    )
    with op.batch_alter_table("booking_messages", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_booking_messages_booking_id"), ["booking_id"], unique=False
        )

    op.create_table(
        "booking_slots",
        sa.Column("barber_id", sa.Integer(), nullable=False),
        sa.Column("slot_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["barber_id"],
            ["barbers.id"],
            name=op.f("fk_booking_slots_barber_id_barbers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["booking_id"],
            ["bookings.id"],
            name=op.f("fk_booking_slots_booking_id_bookings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("barber_id", "slot_start", name=op.f("pk_booking_slots")),
    )
    with op.batch_alter_table("booking_slots", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_booking_slots_booking_id"), ["booking_id"], unique=False
        )

    op.create_table(
        "reviews",
        sa.Column("booking_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("barber_id", sa.Integer(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rating >= 1 AND rating <= 5", name=op.f("ck_reviews_rating")),
        sa.ForeignKeyConstraint(
            ["barber_id"],
            ["barbers.id"],
            name=op.f("fk_reviews_barber_id_barbers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["booking_id"],
            ["bookings.id"],
            name=op.f("fk_reviews_booking_id_bookings"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_reviews_user_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("booking_id", name=op.f("pk_reviews")),
    )
    op.create_table(
        "scheduled_notifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("send_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(
            ["booking_id"],
            ["bookings.id"],
            name=op.f("fk_scheduled_notifications_booking_id_bookings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduled_notifications")),
        sa.UniqueConstraint(
            "booking_id", "kind", name=op.f("uq_scheduled_notifications_booking_id_kind")
        ),
    )
    with op.batch_alter_table("scheduled_notifications", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_scheduled_notifications_send_at"), ["send_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("scheduled_notifications", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_scheduled_notifications_send_at"))

    op.drop_table("scheduled_notifications")
    op.drop_table("reviews")
    with op.batch_alter_table("booking_slots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_booking_slots_booking_id"))

    op.drop_table("booking_slots")
    with op.batch_alter_table("booking_messages", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_booking_messages_booking_id"))

    op.drop_table("booking_messages")
    with op.batch_alter_table("bonus_transactions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_bonus_transactions_user_id"))

    op.drop_table("bonus_transactions")
    op.drop_table("working_hours")
    with op.batch_alter_table("time_off", schema=None) as batch_op:
        batch_op.drop_index("ix_time_off_barber_id_start_at")

    op.drop_table("time_off")
    with op.batch_alter_table("bookings", schema=None) as batch_op:
        batch_op.drop_index("ix_bookings_status_expire_at")
        batch_op.drop_index(batch_op.f("ix_bookings_status"))
        batch_op.drop_index("ix_bookings_client_phone")
        batch_op.drop_index("ix_bookings_client_id_start_at")
        batch_op.drop_index("ix_bookings_barber_id_start_at")

    op.drop_table("bookings")
    op.drop_table("barber_services")
    op.drop_table("barbers")
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.drop_index("ix_audit_log_created_at")

    op.drop_table("audit_log")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_users_phone"))

    op.drop_table("users")
    op.drop_table("settings")
    op.drop_table("services")
    op.drop_table("fsm_states")
