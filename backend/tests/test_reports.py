"""Report assertions observe the authenticated HTTP/exported XLSX seam.

Fixtures arrange PostgreSQL rows directly because Booking is a separate ticket;
all behavioral assertions use the downloadable artifact, not database side reads.
"""
from datetime import datetime, timedelta, timezone
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from openpyxl import load_workbook
from sqlalchemy import update

from app.models import Event, Order, OrderItem, Payment, Seat, User, Zone

pytestmark = pytest.mark.integration


def account(client, db_session, email, role='USER'):
    password = 'Report-test-123!'
    registered = client.post('/api/v1/auth/register', json={'email': email, 'password': password})
    assert registered.status_code == 201
    identifier = UUID(registered.json()['id'])
    if role != 'USER':
        db_session.execute(update(User).where(User.id == identifier).values(role=role))
        db_session.commit()
    logged = client.post('/api/v1/auth/login', json={'email': email, 'password': password})
    assert logged.status_code == 200
    return identifier, {'Authorization': 'Bearer ' + logged.json()['token']}


def event_fixture(db_session, owner):
    now = datetime.now(timezone.utc)
    event = Event(id=uuid4(), owner_id=owner, title='=1+1', description='Report fixture', category='Music', venue='Taipei', sales_start=now-timedelta(days=1), sales_end=now+timedelta(days=1))
    db_session.add(event)
    db_session.commit()
    return event


def test_owner_downloads_three_sheet_report_with_explicit_timezone_and_numeric_zeros(client, db_session):
    owner, headers = account(client, db_session, 'owner@example.test')
    event = event_fixture(db_session, owner)
    response = client.get(f'/api/v1/events/{event.id}/report', headers=headers)
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    assert response.headers['cache-control'] == 'no-store'
    workbook = load_workbook(BytesIO(response.content), read_only=True, data_only=False)
    try:
        assert workbook.sheetnames == ['Summary', 'Zones', 'Orders']
        summary = {row[0].value: row[1] for row in workbook['Summary'].iter_rows(min_row=2)}
        assert summary['活動名稱'].value == '=1+1'
        assert summary['活動名稱'].data_type == 's'
        assert summary['顯示時區'].value == 'Asia/Taipei'
        assert summary['已付款總額 TWD'].value == 0
        assert summary['已付款總額 TWD'].data_type == 'n'
        assert 'Asia/Taipei' in next(workbook['Orders'].values)[2]
    finally:
        workbook.close()


@pytest.mark.parametrize('role,owns,expected', [
    ('ADMIN', False, 200), ('VIEWER', False, 200),
    ('USER', True, 200), ('USER', False, 403),
])
def test_report_role_and_ownership_matrix(client, db_session, role, owns, expected):
    owner, _ = account(client, db_session, 'owner@example.test')
    event = event_fixture(db_session, owner)
    if owns:
        headers = client.post('/api/v1/auth/login', json={'email': 'owner@example.test', 'password': 'Report-test-123!'}).json()
        headers = {'Authorization': 'Bearer ' + headers['token']}
    else:
        _, headers = account(client, db_session, 'reader@example.test', role)
    response = client.get(f'/api/v1/events/{event.id}/report', headers=headers)
    assert response.status_code == expected


def test_report_rejects_anonymous_and_missing_event(client, db_session):
    _, headers = account(client, db_session, 'reader@example.test', 'VIEWER')
    assert client.get(f'/api/v1/events/{uuid4()}/report').status_code == 401
    assert client.get(f'/api/v1/events/{uuid4()}/report', headers=headers).status_code == 404


def test_sales_exclude_canceled_orders_and_preserve_numeric_money_timezone_and_reversals(client, db_session):
    owner, headers = account(client, db_session, 'owner@example.test')
    event = event_fixture(db_session, owner)
    created = datetime(2026, 11, 16, 18, 20, tzinfo=timezone.utc)
    zone = Zone(id=uuid4(), event_id=event.id, code='A', name='測試區', price_twd=15)
    paid = Order(id=uuid4(), event_id=event.id, user_id=owner, status='PENDING_PAYMENT', ticket_count=2, subtotal_twd=30, fee_twd=2, total_twd=32, expires_at=created+timedelta(minutes=5), created_at=created)
    canceled = Order(id=uuid4(), event_id=event.id, user_id=owner, status='CANCELED', ticket_count=1, subtotal_twd=15, fee_twd=1, total_twd=16, expires_at=created+timedelta(minutes=5), created_at=created)
    db_session.add_all([zone, paid, canceled])
    db_session.flush()
    payment = Payment(id=uuid4(), order_id=paid.id, status='SUCCEEDED', amount_twd=32, request_id='report-fixture-paid')
    reversal = Payment(id=uuid4(), order_id=canceled.id, status='REVERSAL_PENDING', amount_twd=16, request_id='report-fixture-late')
    db_session.add_all([payment, reversal])
    db_session.flush()
    paid.accepted_payment_id = payment.id
    paid.status = 'PAID'
    paid.paid_at = created+timedelta(minutes=1)
    for number in ['1', '2']:
        seat = Seat(id=uuid4(), event_id=event.id, zone_id=zone.id, row_label='A', seat_number=number, status='SOLD', current_order_id=paid.id)
        db_session.add(seat)
        db_session.flush()
        db_session.add(OrderItem(id=uuid4(), order_id=paid.id, event_id=event.id, seat_id=seat.id, zone_code_snapshot='A', row_label_snapshot='A', seat_number_snapshot=number, unit_price_twd=15, sold_at=paid.paid_at))
    db_session.commit()
    response = client.get(f'/api/v1/events/{event.id}/report', headers=headers)
    assert response.status_code == 200
    workbook = load_workbook(BytesIO(response.content), read_only=True, data_only=False)
    try:
        summary = dict(workbook['Summary'].iter_rows(min_row=2, values_only=True))
        assert summary['訂單總數'] == 2
        assert summary['已付款張數'] == 2
        assert summary['已付款票價 TWD'] == 30
        assert summary['已付款服務費 TWD'] == 2
        assert summary['已付款總額 TWD'] == 32
        assert summary['待沖正金額 TWD'] == 16
        zone_row = list(workbook['Zones'].values)[1]
        assert zone_row[:5] == ('A', '測試區', 15, 2, 30)
        assert isinstance(zone_row[5], datetime) and zone_row[5].tzinfo is None, 'snapshot time in the Asia/Taipei header timezone'
        details = {row[0]: row for row in workbook['Orders'].iter_rows(min_row=2, values_only=True)}
        row = details[str(paid.id)]
        assert row[2] == datetime(2026, 11, 17, 2, 20)
        assert row[3] == datetime(2026, 11, 17, 2, 21)
        assert row[5:10] == (2, 30, 2, 32, 'TWD')
        assert details[str(canceled.id)][1] == 'CANCELED'
        assert workbook['Orders']['G2'].data_type == 'n'
    finally:
        workbook.close()


def test_successful_http_download_removes_xlsx_and_write_only_temporary_files(client, db_session, tmp_path, monkeypatch):
    import tempfile
    owner, headers = account(client, db_session, 'owner@example.test')
    event = event_fixture(db_session, owner)
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    response = client.get(f'/api/v1/events/{event.id}/report', headers=headers)
    assert response.status_code == 200
    assert not list(tmp_path.glob('ticket-report-*'))
    assert not list(tmp_path.glob('openpyxl.*'))


def test_database_read_failure_removes_partial_export_files(client, db_session, tmp_path, monkeypatch):
    import tempfile
    from sqlalchemy import event as sa_event
    from app.database import get_engine
    owner, headers = account(client, db_session, 'owner@example.test')
    activity = event_fixture(db_session, owner)
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))

    def interrupt_detail_read(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith('SELECT orders.id, orders.status'):
            raise RuntimeError('simulated database interruption')

    engine = get_engine()
    sa_event.listen(engine, 'before_cursor_execute', interrupt_detail_read)
    try:
        with pytest.raises(RuntimeError, match='simulated database interruption'):
            client.get(f'/api/v1/events/{activity.id}/report', headers=headers)
    finally:
        sa_event.remove(engine, 'before_cursor_execute', interrupt_detail_read)
    assert not list(tmp_path.glob('ticket-report-*'))
    assert not list(tmp_path.glob('openpyxl.*'))


@pytest.mark.asyncio
async def test_http_send_failure_also_removes_owned_export(tmp_path):
    from app.api.reports import TemporaryReportResponse
    path = tmp_path / 'download.xlsx'
    path.write_bytes(b'disposable response fixture')
    response = TemporaryReportResponse(path)

    async def receive():
        return {'type': 'http.disconnect'}

    async def send(message):
        raise RuntimeError('client disconnected during send')

    scope = {'type': 'http', 'asgi': {'spec_version': '2.4'}, 'method': 'GET', 'headers': [], 'path': '/report', 'extensions': {}}
    with pytest.raises(RuntimeError, match='client disconnected'):
        await response(scope, receive, send)
    assert not path.exists()


def test_orders_committed_between_summary_and_detail_are_excluded_from_same_export(client, db_session):
    from sqlalchemy import event as sa_event
    from app.database import get_engine
    owner, headers = account(client, db_session, 'owner@example.test')
    activity = event_fixture(db_session, owner)
    engine = get_engine()
    inserted = False

    def concurrent_order(connection, cursor, statement, parameters, context, executemany):
        nonlocal inserted
        if not inserted and statement.startswith('SELECT orders.id, orders.status'):
            inserted = True
            with engine.begin() as writer:
                writer.execute(Order.__table__.insert().values(
                    id=uuid4(), event_id=activity.id, user_id=owner,
                    status='CANCELED', ticket_count=1, subtotal_twd=10,
                    fee_twd=1, total_twd=11,
                    expires_at=datetime.now(timezone.utc)+timedelta(minutes=5),
                ))

    sa_event.listen(engine, 'before_cursor_execute', concurrent_order)
    try:
        response = client.get(f'/api/v1/events/{activity.id}/report', headers=headers)
    finally:
        sa_event.remove(engine, 'before_cursor_execute', concurrent_order)
    assert response.status_code == 200
    workbook = load_workbook(BytesIO(response.content), read_only=True)
    try:
        assert dict(workbook['Summary'].iter_rows(min_row=2, values_only=True))['訂單總數'] == 0
        assert list(workbook['Orders'].iter_rows(min_row=2, values_only=True)) == []
    finally:
        workbook.close()
    newer = client.get(f'/api/v1/events/{activity.id}/report', headers=headers)
    assert newer.status_code == 200
    workbook = load_workbook(BytesIO(newer.content), read_only=True)
    try:
        assert dict(workbook['Summary'].iter_rows(min_row=2, values_only=True))['訂單總數'] == 1
        assert len(list(workbook['Orders'].iter_rows(min_row=2, values_only=True))) == 1
    finally:
        workbook.close()
