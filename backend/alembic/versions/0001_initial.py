"""Initial approved PostgreSQL schema. Frozen migration: no app-model imports."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE SCHEMA IF NOT EXISTS gateway")
    op.create_table('users',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column('email', sa.String(320), nullable=False),
        sa.Column('password_hash', sa.Text(), nullable=False),
        sa.Column('role', sa.String(16), server_default='USER', nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('auth_version', sa.Integer(), server_default='0', nullable=False),
        sa.CheckConstraint("role IN ('ADMIN','USER','VIEWER')", name='ck_users_role'),
        sa.CheckConstraint('auth_version >= 0', name='ck_users_auth_version'),
    )
    op.create_table('sessions',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('token_hash', sa.String(64), unique=True, nullable=False),
        sa.Column('auth_version', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint('auth_version >= 0', name='ck_sessions_auth_version'),
    )
    op.create_table('events',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column('owner_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('description', sa.Text(), server_default='', nullable=False),
        sa.Column('category', sa.String(100), server_default='', nullable=False),
        sa.Column('venue', sa.String(200), server_default='', nullable=False),
        sa.Column('status', sa.String(16), server_default='DRAFT', nullable=False),
        sa.Column('sales_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('sales_end', sa.DateTime(timezone=True), nullable=False),
        sa.Column('performance_start', sa.DateTime(timezone=True), nullable=True),
        sa.Column('performance_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('DRAFT','PUBLISHED')", name='ck_events_status'),
        sa.CheckConstraint('sales_end > sales_start', name='ck_events_sales_window'),
        sa.CheckConstraint('(performance_start IS NULL AND performance_end IS NULL) OR (performance_start IS NOT NULL AND performance_end IS NOT NULL AND performance_end > performance_start)', name='ck_events_performance_window'),
        sa.CheckConstraint('version >= 0', name='ck_events_version'),
    )
    op.create_table('zones',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column('event_id', sa.Uuid(), sa.ForeignKey('events.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('code', sa.String(50), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('price_twd', sa.Integer(), nullable=False),
        sa.UniqueConstraint('event_id', 'code', name='uq_zones_event_code'),
        sa.UniqueConstraint('id', 'event_id', name='uq_zones_id_event'),
        sa.CheckConstraint('price_twd >= 0', name='ck_zones_price'),
    )
    op.create_table('orders',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column('event_id', sa.Uuid(), sa.ForeignKey('events.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('status', sa.String(24), server_default='PENDING_PAYMENT', nullable=False),
        sa.Column('ticket_count', sa.Integer(), nullable=False),
        sa.Column('subtotal_twd', sa.BigInteger(), nullable=False),
        sa.Column('fee_twd', sa.BigInteger(), nullable=False),
        sa.Column('total_twd', sa.BigInteger(), nullable=False),
        sa.Column('currency', sa.String(3), server_default='TWD', nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('accepted_payment_id', sa.Uuid(), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('canceled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expired_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('id', 'event_id', name='uq_orders_id_event'),
        sa.UniqueConstraint('id', 'event_id', 'user_id', name='uq_orders_id_event_user'),
        sa.CheckConstraint("status IN ('PENDING_PAYMENT','PAID','CANCELED','EXPIRED')", name='ck_orders_status'),
        sa.CheckConstraint('ticket_count BETWEEN 1 AND 4', name='ck_orders_ticket_count'),
        sa.CheckConstraint('subtotal_twd >= 0 AND fee_twd >= 0 AND total_twd >= 0', name='ck_orders_amounts'),
        sa.CheckConstraint('fee_twd = (subtotal_twd * 5 + 50) / 100', name='ck_orders_service_fee'),
        sa.CheckConstraint('total_twd = subtotal_twd + fee_twd', name='ck_orders_total'),
        sa.CheckConstraint("currency = 'TWD'", name='ck_orders_currency'),
        sa.CheckConstraint("status <> 'PAID' OR (accepted_payment_id IS NOT NULL AND paid_at IS NOT NULL)", name='ck_orders_paid_fields'),
    )
    op.create_table('user_event_quotas',
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), primary_key=True, nullable=False),
        sa.Column('event_id', sa.Uuid(), sa.ForeignKey('events.id', ondelete='RESTRICT'), primary_key=True, nullable=False),
        sa.Column('max_tickets', sa.Integer(), server_default='4', nullable=False),
        sa.CheckConstraint('max_tickets = 4', name='ck_quotas_limit'),
    )
    op.create_table('seats',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column('event_id', sa.Uuid(), sa.ForeignKey('events.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('zone_id', sa.Uuid(), nullable=False),
        sa.Column('row_label', sa.String(100), nullable=False),
        sa.Column('seat_number', sa.String(100), nullable=False),
        sa.Column('status', sa.String(16), server_default='AVAILABLE', nullable=False),
        sa.Column('current_order_id', sa.Uuid(), nullable=True),
        sa.Column('held_by', sa.Uuid(), nullable=True),
        sa.Column('held_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('version', sa.Integer(), server_default='0', nullable=False),
        sa.UniqueConstraint('event_id', 'zone_id', 'row_label', 'seat_number', name='uq_seats_coordinates'),
        sa.UniqueConstraint('id', 'event_id', name='uq_seats_id_event'),
        sa.ForeignKeyConstraint(['zone_id', 'event_id'], ['zones.id', 'zones.event_id'], name='fk_seats_zone_event'),
        sa.ForeignKeyConstraint(['current_order_id', 'event_id'], ['orders.id', 'orders.event_id'], name='fk_seats_order_event'),
        sa.ForeignKeyConstraint(['current_order_id', 'event_id', 'held_by'], ['orders.id', 'orders.event_id', 'orders.user_id'], name='fk_seats_hold_owner'),
        sa.CheckConstraint("(status = 'AVAILABLE' AND current_order_id IS NULL AND held_by IS NULL AND held_until IS NULL) OR (status = 'HELD' AND current_order_id IS NOT NULL AND held_by IS NOT NULL AND held_until IS NOT NULL) OR (status = 'SOLD' AND current_order_id IS NOT NULL AND held_by IS NULL AND held_until IS NULL)", name='ck_seats_state_fields'),
        sa.CheckConstraint('version >= 0', name='ck_seats_version'),
    )
    op.create_table('order_items',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False),
        sa.Column('order_id', sa.Uuid(), nullable=False),
        sa.Column('event_id', sa.Uuid(), nullable=False),
        sa.Column('seat_id', sa.Uuid(), nullable=False),
        sa.Column('zone_code_snapshot', sa.String(50), nullable=False),
        sa.Column('row_label_snapshot', sa.String(100), nullable=False),
        sa.Column('seat_number_snapshot', sa.String(100), nullable=False),
        sa.Column('unit_price_twd', sa.Integer(), nullable=False),
        sa.Column('sold_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['order_id', 'event_id'], ['orders.id', 'orders.event_id'], name='fk_items_order_event'),
        sa.ForeignKeyConstraint(['seat_id', 'event_id'], ['seats.id', 'seats.event_id'], name='fk_items_seat_event'),
        sa.UniqueConstraint('order_id', 'seat_id', name='uq_items_order_seat'),
        sa.CheckConstraint('unit_price_twd >= 0', name='ck_items_price'),
    )
    op.create_table('payments',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column('order_id', sa.Uuid(), sa.ForeignKey('orders.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('status', sa.String(24), server_default='PENDING', nullable=False),
        sa.Column('amount_twd', sa.BigInteger(), nullable=False),
        sa.Column('currency', sa.String(3), server_default='TWD', nullable=False),
        sa.Column('provider_charge_id', sa.Text(), unique=True, nullable=True),
        sa.Column('request_id', sa.String(128), nullable=False),
        sa.Column('failure_code', sa.Text(), nullable=True),
        sa.Column('reversal_key', sa.Text(), unique=True, nullable=True),
        sa.Column('reversal_attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('next_retry_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('succeeded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reversed_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('id', 'order_id', name='uq_payments_id_order'),
        sa.CheckConstraint("status IN ('PENDING','FAILED','SUCCEEDED','REVERSAL_PENDING','REVERSED')", name='ck_payments_status'),
        sa.CheckConstraint("amount_twd >= 0 AND currency = 'TWD' AND reversal_attempts >= 0", name='ck_payments_fields'),
    )
    op.create_table('payment_callbacks',
        sa.Column('provider_event_id', sa.Text(), primary_key=True, nullable=False),
        sa.Column('payment_id', sa.Uuid(), sa.ForeignKey('payments.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('payload_hash', sa.String(64), nullable=False),
        sa.Column('payload', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.func.clock_timestamp(), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('result_code', sa.Text(), nullable=True),
        sa.Column('request_id', sa.String(128), nullable=False),
    )
    op.create_table('idempotency_keys',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('operation', sa.String(200), nullable=False),
        sa.Column('key', sa.String(200), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('state', sa.String(16), server_default='IN_PROGRESS', nullable=False),
        sa.Column('generation', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=False),
        sa.Column('response_status', sa.Integer(), nullable=True),
        sa.Column('response_body', postgresql.JSONB(), nullable=True),
        sa.Column('order_id', sa.Uuid(), sa.ForeignKey('orders.id', ondelete='RESTRICT'), nullable=True),
        sa.Column('payment_id', sa.Uuid(), sa.ForeignKey('payments.id', ondelete='RESTRICT'), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('user_id', 'operation', 'key', name='uq_idempotency_scope'),
        sa.CheckConstraint("state IN ('IN_PROGRESS','COMPLETED') AND generation >= 0", name='ck_idempotency_state'),
    )
    op.create_table('import_jobs',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column('event_id', sa.Uuid(), sa.ForeignKey('events.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('submitted_by', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('authorization_snapshot', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('file_hash', sa.String(64), nullable=False),
        sa.Column('file_path', sa.Text(), nullable=False),
        sa.Column('original_name', sa.Text(), nullable=False),
        sa.Column('format', sa.String(8), nullable=False),
        sa.Column('mode', sa.String(24), nullable=False),
        sa.Column('status', sa.String(24), server_default='QUEUED', nullable=False),
        sa.Column('phase', sa.String(24), server_default='QUEUED', nullable=False),
        sa.Column('total_rows', sa.Integer(), server_default='0', nullable=False),
        sa.Column('processed_rows', sa.Integer(), server_default='0', nullable=False),
        sa.Column('inserted_rows', sa.Integer(), server_default='0', nullable=False),
        sa.Column('error_rows', sa.Integer(), server_default='0', nullable=False),
        sa.Column('checkpoint', sa.Integer(), server_default='0', nullable=False),
        sa.Column('generation', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('lease_owner', sa.Text(), nullable=True),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failure_code', sa.Text(), nullable=True),
        sa.Column('request_id', sa.String(128), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('event_id', 'file_hash', name='uq_import_jobs_content'),
        sa.CheckConstraint("format IN ('CSV','JSON')", name='ck_import_jobs_format'),
        sa.CheckConstraint("mode IN ('ALL_OR_NOTHING','SKIP_INVALID')", name='ck_import_jobs_mode'),
        sa.CheckConstraint("status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED_VALIDATION','FAILED_SYSTEM')", name='ck_import_jobs_status'),
        sa.CheckConstraint('total_rows >= 0 AND processed_rows >= 0 AND inserted_rows >= 0 AND error_rows >= 0 AND checkpoint >= 0 AND generation >= 0', name='ck_import_jobs_counts'),
    )
    op.create_table('import_rows',
        sa.Column('job_id', sa.Uuid(), sa.ForeignKey('import_jobs.id', ondelete='CASCADE'), primary_key=True, nullable=False),
        sa.Column('row_number', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('source_line', sa.Integer(), nullable=True),
        sa.Column('zone_code', sa.Text(), nullable=True),
        sa.Column('row_label', sa.Text(), nullable=True),
        sa.Column('seat_number', sa.Text(), nullable=True),
        sa.Column('validation_state', sa.String(16), server_default='PENDING', nullable=False),
        sa.Column('resolved_zone_id', sa.Uuid(), sa.ForeignKey('zones.id', ondelete='RESTRICT'), nullable=True),
        sa.Column('applied_seat_id', sa.Uuid(), sa.ForeignKey('seats.id', ondelete='RESTRICT'), nullable=True),
        sa.CheckConstraint('row_number > 0', name='ck_import_rows_number'),
    )
    op.create_table('import_errors',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False),
        sa.Column('job_id', sa.Uuid(), sa.ForeignKey('import_jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('row_number', sa.Integer(), nullable=True),
        sa.Column('source_line', sa.Integer(), nullable=True),
        sa.Column('code', sa.String(100), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
    )
    op.create_table('event_streams',
        sa.Column('event_id', sa.Uuid(), sa.ForeignKey('events.id', ondelete='RESTRICT'), primary_key=True, nullable=False),
        sa.Column('current_version', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('published_version', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('publisher_generation', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('publisher_lease_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.clock_timestamp(), nullable=False),
        sa.CheckConstraint('published_version >= 0 AND current_version >= published_version AND publisher_generation >= 0', name='ck_event_stream_versions'),
    )
    op.create_table('seat_events',
        sa.Column('event_id', sa.Uuid(), sa.ForeignKey('events.id', ondelete='RESTRICT'), primary_key=True, nullable=False),
        sa.Column('version', sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column('changes', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.func.clock_timestamp(), nullable=False),
        sa.Column('request_id', sa.String(128), nullable=False),
        sa.CheckConstraint('version > 0', name='ck_seat_events_version'),
    )
    op.create_table('outbox_events',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column('kind', sa.String(64), nullable=False),
        sa.Column('aggregate_id', sa.Uuid(), nullable=False),
        sa.Column('aggregate_version', sa.BigInteger(), nullable=True),
        sa.Column('payload', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('request_id', sa.String(128), nullable=False),
        sa.Column('available_at', sa.DateTime(timezone=True), server_default=sa.func.clock_timestamp(), nullable=False),
        sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('generation', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.CheckConstraint('attempts >= 0 AND generation >= 0', name='ck_outbox_counts'),
    )
    op.create_table('list_snapshots',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column('session_id', sa.Uuid(), sa.ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('auth_version', sa.Integer(), nullable=False),
        sa.Column('filter_spec', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('sort_spec', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('page_size', sa.Integer(), nullable=False),
        sa.Column('total_count', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('auth_version >= 0 AND page_size > 0 AND total_count >= 0', name='ck_snapshots_fields'),
    )
    op.create_table('list_snapshot_items',
        sa.Column('snapshot_id', sa.Uuid(), sa.ForeignKey('list_snapshots.id', ondelete='CASCADE'), primary_key=True, nullable=False),
        sa.Column('position', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('event_id', sa.Uuid(), nullable=False),
        sa.Column('projection', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint('position >= 0', name='ck_snapshot_items_position'),
    )
    op.create_table('access_logs',
        sa.Column('event_id', sa.Uuid(), primary_key=True, nullable=False),
        sa.Column('request_id', sa.String(128), nullable=False),
        sa.Column('instance_id', sa.Text(), nullable=False),
        sa.Column('method', sa.String(16), nullable=False),
        sa.Column('route_template', sa.Text(), nullable=False),
        sa.Column('status_code', sa.Integer(), nullable=False),
        sa.Column('error_kind', sa.Text(), nullable=True),
        sa.Column('business_outcome', sa.Text(), nullable=True),
        sa.Column('duration_us', sa.BigInteger(), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.func.clock_timestamp(), nullable=False),
        sa.Column('clock_error_us', sa.BigInteger(), server_default='0', nullable=False),
        sa.CheckConstraint('duration_us >= 0 AND clock_error_us >= 0', name='ck_access_logs_timing'),
    )
    op.create_table('service_logs',
        sa.Column('event_id', sa.Uuid(), primary_key=True, nullable=False),
        sa.Column('request_id', sa.String(128), nullable=False),
        sa.Column('span_id', sa.Text(), nullable=True),
        sa.Column('parent_span_id', sa.Text(), nullable=True),
        sa.Column('service', sa.String(64), nullable=False),
        sa.Column('level', sa.String(16), nullable=False),
        sa.Column('code', sa.String(100), nullable=False),
        sa.Column('context', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.func.clock_timestamp(), nullable=False),
    )
    op.create_table('system_metrics',
        sa.Column('source_id', sa.Text(), primary_key=True, nullable=False),
        sa.Column('bucket_at', sa.DateTime(timezone=True), primary_key=True, nullable=False),
        sa.Column('window_seconds', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('request_count', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('error_count', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('business_counts', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('latency_summary', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('connections', sa.Integer(), server_default='0', nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('data_complete', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.clock_timestamp(), nullable=False),
        sa.CheckConstraint('window_seconds > 0 AND request_count >= 0 AND error_count >= 0 AND error_count <= request_count AND connections >= 0', name='ck_system_metrics_counts'),
    )
    op.create_table('alerts',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False),
        sa.Column('rule_id', sa.String(100), nullable=False),
        sa.Column('scope', sa.Text(), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('triggered_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('details', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_table('charges',
        sa.Column('id', sa.Uuid(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.Column('payment_key', sa.Text(), unique=True, nullable=False),
        sa.Column('order_reference', sa.Text(), nullable=False),
        sa.Column('amount_twd', sa.BigInteger(), nullable=False),
        sa.Column('currency', sa.String(3), server_default='TWD', nullable=False),
        sa.Column('outcome', sa.String(16), nullable=False),
        sa.Column('provider_event_id', sa.Text(), unique=True, nullable=False),
        sa.Column('callback_payload', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('callback_attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('next_callback_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('callback_delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('lease_generation', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reversal_key', sa.Text(), unique=True, nullable=True),
        sa.Column('reversed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('request_id', sa.String(128), nullable=False),
        sa.CheckConstraint("amount_twd >= 0 AND currency = 'TWD' AND callback_attempts >= 0 AND lease_generation >= 0", name='ck_gateway_charge_fields'),
        sa.CheckConstraint("outcome IN ('PENDING','SUCCESS','FAILURE')", name='ck_gateway_charge_outcome'),
        schema='gateway',
    )
    op.create_foreign_key('fk_orders_accepted_payment', 'orders', 'payments', ['accepted_payment_id', 'id'], ['id', 'order_id'])
    op.create_index('uq_users_email_lower', 'users', [sa.text('lower(email)')], unique=True)
    op.create_index('ix_sessions_user_id', 'sessions', ['user_id'])
    op.create_index('ix_sessions_expires_at', 'sessions', ['expires_at'])
    op.create_index('ix_events_owner_id', 'events', ['owner_id'])
    op.create_index('ix_events_status_sales', 'events', ['status', 'sales_start', 'id'])
    op.create_index('ix_events_created', 'events', ['created_at', 'id'])
    op.create_index('ix_orders_user_event_status_expiry', 'orders', ['user_id', 'event_id', 'status', 'expires_at'])
    op.create_index('ix_orders_pending_expiry', 'orders', ['expires_at'], postgresql_where=sa.text("status = 'PENDING_PAYMENT'"))
    op.create_index('ix_seats_held_until', 'seats', ['held_until'])
    op.create_index('ix_seats_allocation', 'seats', ['event_id', 'zone_id', 'status', 'id'])
    op.create_index('uq_items_sold_seat', 'order_items', ['seat_id'], unique=True, postgresql_where=sa.text('sold_at IS NOT NULL'))
    op.create_index('ix_items_order', 'order_items', ['order_id'])
    op.create_index('uq_payments_pending_order', 'payments', ['order_id'], unique=True, postgresql_where=sa.text("status = 'PENDING'"))
    op.create_index('ix_payments_retry', 'payments', ['status', 'next_retry_at'])
    op.create_index('ix_idempotency_keys_expires_at', 'idempotency_keys', ['expires_at'])
    op.create_index('ix_idempotency_lease', 'idempotency_keys', ['state', 'lease_until'])
    op.create_index('ix_import_jobs_lease', 'import_jobs', ['status', 'lease_until'])
    op.create_index('ix_import_rows_coordinates', 'import_rows', ['job_id', 'zone_code', 'row_label', 'seat_number'])
    op.create_index('ix_import_errors_row', 'import_errors', ['job_id', 'row_number'])
    op.create_index('uq_outbox_seat_version', 'outbox_events', ['aggregate_id', 'aggregate_version'], unique=True, postgresql_where=sa.text("kind = 'seat_changed'"))
    op.create_index('ix_outbox_pending', 'outbox_events', ['available_at'], postgresql_where=sa.text('published_at IS NULL'))
    op.create_index('ix_list_snapshots_expires_at', 'list_snapshots', ['expires_at'])
    op.create_index('ix_access_logs_request_id', 'access_logs', ['request_id'])
    op.create_index('ix_access_logs_observed_at', 'access_logs', ['observed_at'])
    op.create_index('ix_service_logs_request_id', 'service_logs', ['request_id'])
    op.create_index('ix_service_logs_observed_at', 'service_logs', ['observed_at'])
    op.create_index('ix_system_metrics_bucket', 'system_metrics', ['bucket_at'])
    op.create_index('uq_alerts_open_rule', 'alerts', ['rule_id', 'scope'], unique=True, postgresql_where=sa.text('resolved_at IS NULL'))
    op.create_index('ix_alerts_status_triggered', 'alerts', ['status', 'triggered_at'])


def downgrade():
    op.drop_constraint('fk_orders_accepted_payment', 'orders', type_='foreignkey')
    op.drop_table('charges', schema='gateway')
    op.drop_table('alerts')
    op.drop_table('system_metrics')
    op.drop_table('service_logs')
    op.drop_table('access_logs')
    op.drop_table('list_snapshot_items')
    op.drop_table('list_snapshots')
    op.drop_table('outbox_events')
    op.drop_table('seat_events')
    op.drop_table('event_streams')
    op.drop_table('import_errors')
    op.drop_table('import_rows')
    op.drop_table('import_jobs')
    op.drop_table('idempotency_keys')
    op.drop_table('payment_callbacks')
    op.drop_table('payments')
    op.drop_table('order_items')
    op.drop_table('seats')
    op.drop_table('user_event_quotas')
    op.drop_table('orders')
    op.drop_table('zones')
    op.drop_table('events')
    op.drop_table('sessions')
    op.drop_table('users')
    op.execute("DROP SCHEMA gateway")
