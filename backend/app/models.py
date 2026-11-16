"""PostgreSQL persistence model for the approved ticketing domain.

Cross-row transitions belong to transactional services. These constraints protect
identity, ownership links and per-row shape; they do not replace quota locking.
"""
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey,
    ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint,
    func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UUIDKey:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)


class Created:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class Timestamps(Created):
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(), onupdate=func.clock_timestamp())


def json_column():
    return mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))


class User(UUIDKey, Timestamps, Base):
    __tablename__ = 'users'
    email: Mapped[str] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default='USER', server_default='USER')
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text('true'))
    auth_version: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    __table_args__ = (
        Index('uq_users_email_lower', func.lower(email), unique=True),
        CheckConstraint("role IN ('ADMIN','USER','VIEWER')", name='ck_users_role'),
        CheckConstraint('auth_version >= 0', name='ck_users_auth_version'),
    )


class Session(UUIDKey, Created, Base):
    __tablename__ = 'sessions'
    user_id: Mapped[UUID] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    auth_version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint('auth_version >= 0', name='ck_sessions_auth_version'),)


class Event(UUIDKey, Timestamps, Base):
    __tablename__ = 'events'
    owner_id: Mapped[UUID] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'), index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default='', server_default='')
    category: Mapped[str] = mapped_column(String(100), default='', server_default='')
    venue: Mapped[str] = mapped_column(String(200), default='', server_default='')
    status: Mapped[str] = mapped_column(String(16), default='DRAFT', server_default='DRAFT')
    sales_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sales_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    performance_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    performance_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("status IN ('DRAFT','PUBLISHED')", name='ck_events_status'),
        CheckConstraint('sales_end > sales_start', name='ck_events_sales_window'),
        CheckConstraint('(performance_start IS NULL AND performance_end IS NULL) OR (performance_start IS NOT NULL AND performance_end IS NOT NULL AND performance_end > performance_start)', name='ck_events_performance_window'),
        CheckConstraint('version >= 0', name='ck_events_version'),
        Index('ix_events_status_sales', 'status', 'sales_start', 'id'),
        Index('ix_events_created', 'created_at', 'id'),
    )


class Zone(UUIDKey, Created, Base):
    __tablename__ = 'zones'
    event_id: Mapped[UUID] = mapped_column(ForeignKey('events.id', ondelete='RESTRICT'))
    code: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(100))
    price_twd: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        UniqueConstraint('event_id', 'code', name='uq_zones_event_code'),
        UniqueConstraint('id', 'event_id', name='uq_zones_id_event'),
        CheckConstraint('price_twd >= 0', name='ck_zones_price'),
    )


class Order(UUIDKey, Timestamps, Base):
    __tablename__ = 'orders'
    event_id: Mapped[UUID] = mapped_column(ForeignKey('events.id', ondelete='RESTRICT'))
    user_id: Mapped[UUID] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    status: Mapped[str] = mapped_column(String(24), default='PENDING_PAYMENT', server_default='PENDING_PAYMENT')
    ticket_count: Mapped[int] = mapped_column(Integer)
    subtotal_twd: Mapped[int] = mapped_column(BigInteger)
    fee_twd: Mapped[int] = mapped_column(BigInteger)
    total_twd: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default='TWD', server_default='TWD')
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_payment_id: Mapped[UUID | None]
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint('id', 'event_id', name='uq_orders_id_event'),
        UniqueConstraint('id', 'event_id', 'user_id', name='uq_orders_id_event_user'),
        ForeignKeyConstraint(['accepted_payment_id', 'id'], ['payments.id', 'payments.order_id'], name='fk_orders_accepted_payment', use_alter=True),
        CheckConstraint("status IN ('PENDING_PAYMENT','PAID','CANCELED','EXPIRED')", name='ck_orders_status'),
        CheckConstraint('ticket_count BETWEEN 1 AND 4', name='ck_orders_ticket_count'),
        CheckConstraint('subtotal_twd >= 0 AND fee_twd >= 0 AND total_twd >= 0', name='ck_orders_amounts'),
        CheckConstraint('fee_twd = (subtotal_twd * 5 + 50) / 100', name='ck_orders_service_fee'),
        CheckConstraint('total_twd = subtotal_twd + fee_twd', name='ck_orders_total'),
        CheckConstraint("currency = 'TWD'", name='ck_orders_currency'),
        CheckConstraint("status <> 'PAID' OR (accepted_payment_id IS NOT NULL AND paid_at IS NOT NULL)", name='ck_orders_paid_fields'),
        Index('ix_orders_user_event_status_expiry', 'user_id', 'event_id', 'status', 'expires_at'),
        Index('ix_orders_pending_expiry', 'expires_at', postgresql_where=text("status = 'PENDING_PAYMENT'")),
    )


class UserEventQuota(Created, Base):
    __tablename__ = 'user_event_quotas'
    user_id: Mapped[UUID] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(ForeignKey('events.id', ondelete='RESTRICT'), primary_key=True)
    max_tickets: Mapped[int] = mapped_column(Integer, default=4, server_default='4')
    __table_args__ = (CheckConstraint('max_tickets = 4', name='ck_quotas_limit'),)


class Seat(UUIDKey, Timestamps, Base):
    __tablename__ = 'seats'
    event_id: Mapped[UUID] = mapped_column(ForeignKey('events.id', ondelete='RESTRICT'))
    zone_id: Mapped[UUID]
    row_label: Mapped[str] = mapped_column(String(100))
    seat_number: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), default='AVAILABLE', server_default='AVAILABLE')
    current_order_id: Mapped[UUID | None]
    held_by: Mapped[UUID | None]
    held_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    version: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    __table_args__ = (
        UniqueConstraint('event_id', 'zone_id', 'row_label', 'seat_number', name='uq_seats_coordinates'),
        UniqueConstraint('id', 'event_id', name='uq_seats_id_event'),
        ForeignKeyConstraint(['zone_id', 'event_id'], ['zones.id', 'zones.event_id'], name='fk_seats_zone_event'),
        ForeignKeyConstraint(['current_order_id', 'event_id'], ['orders.id', 'orders.event_id'], name='fk_seats_order_event'),
        ForeignKeyConstraint(['current_order_id', 'event_id', 'held_by'], ['orders.id', 'orders.event_id', 'orders.user_id'], name='fk_seats_hold_owner'),
        CheckConstraint("(status = 'AVAILABLE' AND current_order_id IS NULL AND held_by IS NULL AND held_until IS NULL) OR (status = 'HELD' AND current_order_id IS NOT NULL AND held_by IS NOT NULL AND held_until IS NOT NULL) OR (status = 'SOLD' AND current_order_id IS NOT NULL AND held_by IS NULL AND held_until IS NULL)", name='ck_seats_state_fields'),
        CheckConstraint('version >= 0', name='ck_seats_version'),
        Index('ix_seats_allocation', 'event_id', 'zone_id', 'status', 'id'),
    )


class OrderItem(UUIDKey, Base):
    __tablename__ = 'order_items'
    order_id: Mapped[UUID]
    event_id: Mapped[UUID]
    seat_id: Mapped[UUID]
    zone_code_snapshot: Mapped[str] = mapped_column(String(50))
    row_label_snapshot: Mapped[str] = mapped_column(String(100))
    seat_number_snapshot: Mapped[str] = mapped_column(String(100))
    unit_price_twd: Mapped[int] = mapped_column(Integer)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(['order_id', 'event_id'], ['orders.id', 'orders.event_id'], name='fk_items_order_event'),
        ForeignKeyConstraint(['seat_id', 'event_id'], ['seats.id', 'seats.event_id'], name='fk_items_seat_event'),
        UniqueConstraint('order_id', 'seat_id', name='uq_items_order_seat'),
        CheckConstraint('unit_price_twd >= 0', name='ck_items_price'),
        Index('uq_items_sold_seat', 'seat_id', unique=True, postgresql_where=text('sold_at IS NOT NULL')),
        Index('ix_items_order', 'order_id'),
    )


class Payment(UUIDKey, Timestamps, Base):
    __tablename__ = 'payments'
    order_id: Mapped[UUID] = mapped_column(ForeignKey('orders.id', ondelete='RESTRICT'))
    status: Mapped[str] = mapped_column(String(24), default='PENDING', server_default='PENDING')
    amount_twd: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default='TWD', server_default='TWD')
    provider_charge_id: Mapped[str | None] = mapped_column(Text, unique=True)
    request_id: Mapped[str] = mapped_column(String(128))
    failure_code: Mapped[str | None] = mapped_column(Text)
    reversal_key: Mapped[str | None] = mapped_column(Text, unique=True)
    reversal_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint('id', 'order_id', name='uq_payments_id_order'),
        CheckConstraint("status IN ('PENDING','FAILED','SUCCEEDED','REVERSAL_PENDING','REVERSED')", name='ck_payments_status'),
        CheckConstraint("amount_twd >= 0 AND currency = 'TWD' AND reversal_attempts >= 0", name='ck_payments_fields'),
        Index('uq_payments_pending_order', 'order_id', unique=True, postgresql_where=text("status = 'PENDING'")),
        Index('ix_payments_retry', 'status', 'next_retry_at'),
    )


class PaymentCallback(Base):
    __tablename__ = 'payment_callbacks'
    provider_event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    payment_id: Mapped[UUID] = mapped_column(ForeignKey('payments.id', ondelete='RESTRICT'))
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = json_column()
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_code: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str] = mapped_column(String(128))


class IdempotencyKey(UUIDKey, Created, Base):
    __tablename__ = 'idempotency_keys'
    user_id: Mapped[UUID] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    operation: Mapped[str] = mapped_column(String(200))
    key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default='IN_PROGRESS', server_default='IN_PROGRESS')
    generation: Mapped[int] = mapped_column(BigInteger, default=0, server_default='0')
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    order_id: Mapped[UUID | None] = mapped_column(ForeignKey('orders.id', ondelete='RESTRICT'))
    payment_id: Mapped[UUID | None] = mapped_column(ForeignKey('payments.id', ondelete='RESTRICT'))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    __table_args__ = (
        UniqueConstraint('user_id', 'operation', 'key', name='uq_idempotency_scope'),
        CheckConstraint("state IN ('IN_PROGRESS','COMPLETED') AND generation >= 0", name='ck_idempotency_state'),
        Index('ix_idempotency_lease', 'state', 'lease_until'),
    )


class ImportJob(UUIDKey, Created, Base):
    __tablename__ = 'import_jobs'
    event_id: Mapped[UUID] = mapped_column(ForeignKey('events.id', ondelete='RESTRICT'))
    submitted_by: Mapped[UUID] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    authorization_snapshot: Mapped[dict[str, Any]] = json_column()
    file_hash: Mapped[str] = mapped_column(String(64))
    file_path: Mapped[str] = mapped_column(Text)
    original_name: Mapped[str] = mapped_column(Text)
    format: Mapped[str] = mapped_column(String(8))
    mode: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default='QUEUED', server_default='QUEUED')
    phase: Mapped[str] = mapped_column(String(24), default='QUEUED', server_default='QUEUED')
    total_rows: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    processed_rows: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    inserted_rows: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    error_rows: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    checkpoint: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    generation: Mapped[int] = mapped_column(BigInteger, default=0, server_default='0')
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str] = mapped_column(String(128))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint('event_id', 'file_hash', name='uq_import_jobs_content'),
        CheckConstraint("format IN ('CSV','JSON')", name='ck_import_jobs_format'),
        CheckConstraint("mode IN ('ALL_OR_NOTHING','SKIP_INVALID')", name='ck_import_jobs_mode'),
        CheckConstraint("status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED_VALIDATION','FAILED_SYSTEM')", name='ck_import_jobs_status'),
        CheckConstraint('total_rows >= 0 AND processed_rows >= 0 AND inserted_rows >= 0 AND error_rows >= 0 AND checkpoint >= 0 AND generation >= 0', name='ck_import_jobs_counts'),
        Index('ix_import_jobs_lease', 'status', 'lease_until'),
    )


class ImportRow(Base):
    __tablename__ = 'import_rows'
    job_id: Mapped[UUID] = mapped_column(ForeignKey('import_jobs.id', ondelete='CASCADE'), primary_key=True)
    row_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_line: Mapped[int | None] = mapped_column(Integer)
    zone_code: Mapped[str | None] = mapped_column(Text)
    row_label: Mapped[str | None] = mapped_column(Text)
    seat_number: Mapped[str | None] = mapped_column(Text)
    validation_state: Mapped[str] = mapped_column(String(16), default='PENDING', server_default='PENDING')
    resolved_zone_id: Mapped[UUID | None] = mapped_column(ForeignKey('zones.id', ondelete='RESTRICT'))
    applied_seat_id: Mapped[UUID | None] = mapped_column(ForeignKey('seats.id', ondelete='RESTRICT'))
    __table_args__ = (
        CheckConstraint('row_number > 0', name='ck_import_rows_number'),
        Index('ix_import_rows_coordinates', 'job_id', 'zone_code', 'row_label', 'seat_number'),
    )


class ImportError(UUIDKey, Base):
    __tablename__ = 'import_errors'
    job_id: Mapped[UUID] = mapped_column(ForeignKey('import_jobs.id', ondelete='CASCADE'))
    row_number: Mapped[int | None] = mapped_column(Integer)
    source_line: Mapped[int | None] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)
    __table_args__ = (Index('ix_import_errors_row', 'job_id', 'row_number'),)


class EventStream(Base):
    __tablename__ = 'event_streams'
    event_id: Mapped[UUID] = mapped_column(ForeignKey('events.id', ondelete='RESTRICT'), primary_key=True)
    current_version: Mapped[int] = mapped_column(BigInteger, default=0, server_default='0')
    published_version: Mapped[int] = mapped_column(BigInteger, default=0, server_default='0')
    publisher_generation: Mapped[int] = mapped_column(BigInteger, default=0, server_default='0')
    publisher_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(), onupdate=func.clock_timestamp())
    __table_args__ = (CheckConstraint('published_version >= 0 AND current_version >= published_version AND publisher_generation >= 0', name='ck_event_stream_versions'),)


class SeatEvent(Base):
    __tablename__ = 'seat_events'
    event_id: Mapped[UUID] = mapped_column(ForeignKey('events.id', ondelete='RESTRICT'), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    changes: Mapped[dict[str, Any]] = json_column()
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    request_id: Mapped[str] = mapped_column(String(128))
    __table_args__ = (CheckConstraint('version > 0', name='ck_seat_events_version'),)


class OutboxEvent(UUIDKey, Created, Base):
    __tablename__ = 'outbox_events'
    kind: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[UUID]
    aggregate_version: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[dict[str, Any]] = json_column()
    request_id: Mapped[str] = mapped_column(String(128))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    generation: Mapped[int] = mapped_column(BigInteger, default=0, server_default='0')
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        Index('uq_outbox_seat_version', 'aggregate_id', 'aggregate_version', unique=True, postgresql_where=text("kind = 'seat_changed'")),
        Index('ix_outbox_pending', 'available_at', postgresql_where=text('published_at IS NULL')),
        CheckConstraint('attempts >= 0 AND generation >= 0', name='ck_outbox_counts'),
    )


class ListSnapshot(UUIDKey, Created, Base):
    __tablename__ = 'list_snapshots'
    session_id: Mapped[UUID] = mapped_column(ForeignKey('sessions.id', ondelete='CASCADE'))
    user_id: Mapped[UUID] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    auth_version: Mapped[int] = mapped_column(Integer)
    filter_spec: Mapped[dict[str, Any]] = json_column()
    sort_spec: Mapped[dict[str, Any]] = json_column()
    page_size: Mapped[int] = mapped_column(Integer)
    total_count: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    __table_args__ = (CheckConstraint('auth_version >= 0 AND page_size > 0 AND total_count >= 0', name='ck_snapshots_fields'),)


class ListSnapshotItem(Base):
    __tablename__ = 'list_snapshot_items'
    snapshot_id: Mapped[UUID] = mapped_column(ForeignKey('list_snapshots.id', ondelete='CASCADE'), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[UUID]
    projection: Mapped[dict[str, Any]] = json_column()
    __table_args__ = (CheckConstraint('position >= 0', name='ck_snapshot_items_position'),)


class AccessLog(Base):
    __tablename__ = 'access_logs'
    event_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    request_id: Mapped[str] = mapped_column(String(128), index=True)
    instance_id: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(String(16))
    route_template: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int] = mapped_column(Integer)
    error_kind: Mapped[str | None] = mapped_column(Text)
    business_outcome: Mapped[str | None] = mapped_column(Text)
    duration_us: Mapped[int] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    clock_error_us: Mapped[int] = mapped_column(BigInteger, default=0, server_default='0')
    __table_args__ = (CheckConstraint('duration_us >= 0 AND clock_error_us >= 0', name='ck_access_logs_timing'),)


class ServiceLog(Base):
    __tablename__ = 'service_logs'
    event_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    request_id: Mapped[str] = mapped_column(String(128), index=True)
    span_id: Mapped[str | None] = mapped_column(Text)
    parent_span_id: Mapped[str | None] = mapped_column(Text)
    service: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(16))
    code: Mapped[str] = mapped_column(String(100))
    context: Mapped[dict[str, Any]] = json_column()
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class SystemMetric(Base):
    __tablename__ = 'system_metrics'
    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    bucket_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    window_seconds: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_count: Mapped[int] = mapped_column(BigInteger, default=0, server_default='0')
    error_count: Mapped[int] = mapped_column(BigInteger, default=0, server_default='0')
    business_counts: Mapped[dict[str, Any]] = json_column()
    latency_summary: Mapped[dict[str, Any]] = json_column()
    connections: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data_complete: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text('true'))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp(), onupdate=func.clock_timestamp())
    __table_args__ = (
        CheckConstraint('window_seconds > 0 AND request_count >= 0 AND error_count >= 0 AND error_count <= request_count AND connections >= 0', name='ck_system_metrics_counts'),
        Index('ix_system_metrics_bucket', 'bucket_at'),
    )


class Alert(UUIDKey, Base):
    __tablename__ = 'alerts'
    rule_id: Mapped[str] = mapped_column(String(100))
    scope: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = json_column()
    __table_args__ = (
        Index('uq_alerts_open_rule', 'rule_id', 'scope', unique=True, postgresql_where=text('resolved_at IS NULL')),
        Index('ix_alerts_status_triggered', 'status', 'triggered_at'),
    )


class GatewayCharge(UUIDKey, Created, Base):
    __tablename__ = 'charges'
    payment_key: Mapped[str] = mapped_column(Text, unique=True)
    order_reference: Mapped[str] = mapped_column(Text)
    amount_twd: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default='TWD', server_default='TWD')
    outcome: Mapped[str] = mapped_column(String(16))
    provider_event_id: Mapped[str] = mapped_column(Text, unique=True)
    callback_payload: Mapped[dict[str, Any]] = json_column()
    callback_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    next_callback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    callback_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_generation: Mapped[int] = mapped_column(BigInteger, default=0, server_default='0')
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversal_key: Mapped[str | None] = mapped_column(Text, unique=True)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_id: Mapped[str] = mapped_column(String(128))
    __table_args__ = (
        CheckConstraint("amount_twd >= 0 AND currency = 'TWD' AND callback_attempts >= 0 AND lease_generation >= 0", name='ck_gateway_charge_fields'),
        CheckConstraint("outcome IN ('PENDING','SUCCESS','FAILURE')", name='ck_gateway_charge_outcome'),
        {'schema': 'gateway'},
    )
