"""Consistent, bounded-memory sales exports.

The PostgreSQL transaction remains REPEATABLE READ/READ ONLY while all three
sheets are generated. Only scalar aggregates and bounded cursor batches enter
Python; XLSX output and OpenPyXL worksheet XML are temporary files, not BytesIO.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import os
from pathlib import Path
import tempfile
from uuid import UUID
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill
from sqlalchemy import Connection, Engine, func, select
from sqlalchemy.orm import Session

from app.auth import Principal
from app.errors import AppError
from app.models import Event, Order, OrderItem, Payment, Zone

TIMEZONE = ZoneInfo('Asia/Taipei')
BATCH_SIZE = 1000
_ILLEGAL_XML = dict.fromkeys([*range(9), 11, 12, *range(14, 32)])


def _append(sheet, values, *, header=False):
    cells = []
    for value in values:
        if isinstance(value, datetime):
            value = value.astimezone(TIMEZONE).replace(tzinfo=None)
        if isinstance(value, str):
            # Event names, zone codes and imported seat labels are untrusted.
            # Explicit string cells prevent Excel interpreting '=...' as formulas.
            cell = WriteOnlyCell(sheet, value=value.translate(_ILLEGAL_XML))
            cell.data_type = 's'
        else:
            cell = WriteOnlyCell(sheet, value=value)
            if isinstance(value, datetime):
                cell.number_format = 'yyyy-mm-dd hh:mm:ss'
            elif isinstance(value, (int, Decimal)):
                cell.number_format = '#,##0'
        if header:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='174D3D')
        cells.append(cell)
    sheet.append(cells)


def _sheet(workbook, name, headers):
    sheet = workbook.create_sheet(name)
    sheet.freeze_panes = 'A2'
    _append(sheet, headers, header=True)
    return sheet


def _discard_workbook(workbook):
    """Close generators and discard OpenPyXL 3.x's temporary worksheet files.

    Workbook.close() alone does not clean an unsaved write-only sheet on error.
    This narrowly scoped adapter covers that library-specific failure path.
    """
    for sheet in workbook.worksheets:
        try:
            sheet.close()
        except Exception:
            pass  # Preserve the original export error, including disk failures.
        writer = getattr(sheet, '_writer', None)
        if writer is not None:
            try:
                writer.cleanup()
            except Exception:
                pass
    workbook.close()


def _write_summary(connection, sheet, event, generated_at):
    paid = Order.status == 'PAID'
    aggregate = connection.execute(select(
        func.count(Order.id).label('orders'),
        func.count(Order.id).filter(paid).label('paid_orders'),
        func.coalesce(func.sum(Order.ticket_count).filter(paid), 0).label('paid_tickets'),
        func.coalesce(func.sum(Order.subtotal_twd).filter(paid), 0).label('subtotal'),
        func.coalesce(func.sum(Order.fee_twd).filter(paid), 0).label('fees'),
        func.coalesce(func.sum(Order.total_twd).filter(paid), 0).label('total'),
        func.count(Order.id).filter(Order.status == 'PENDING_PAYMENT').label('pending'),
        func.count(Order.id).filter(Order.status == 'CANCELED').label('canceled'),
        func.count(Order.id).filter(Order.status == 'EXPIRED').label('expired'),
    ).where(Order.event_id == event['id'])).mappings().one()
    reversals = connection.execute(select(
        func.count(Payment.id).label('count'),
        func.coalesce(func.sum(Payment.amount_twd), 0).label('amount'),
    ).join(Order, Payment.order_id == Order.id).where(
        Order.event_id == event['id'], Payment.status == 'REVERSAL_PENDING',
    )).mappings().one()
    for row in [
        ('活動編號', str(event['id'])), ('活動名稱', event['title']),
        ('顯示時區', 'Asia/Taipei'), ('幣別', 'TWD'), ('產生時間 (Asia/Taipei)', generated_at),
        ('訂單總數', aggregate['orders']), ('已付款訂單數', aggregate['paid_orders']),
        ('已付款張數', aggregate['paid_tickets']), ('已付款票價 TWD', aggregate['subtotal']),
        ('已付款服務費 TWD', aggregate['fees']), ('已付款總額 TWD', aggregate['total']),
        ('待付款訂單數', aggregate['pending']), ('已取消訂單數', aggregate['canceled']),
        ('已逾時訂單數', aggregate['expired']), ('待沖正筆數', reversals['count']),
        ('待沖正金額 TWD', reversals['amount']),
    ]:
        _append(sheet, row)


def _write_zones(connection, sheet, event_id):
    sold = select(
        OrderItem.zone_code_snapshot.label('code'),
        func.count(OrderItem.id).label('tickets'),
        func.sum(OrderItem.unit_price_twd).label('revenue'),
    ).join(Order, Order.id == OrderItem.order_id).where(
        Order.event_id == event_id, Order.status == 'PAID',
    ).group_by(OrderItem.zone_code_snapshot).subquery()
    statement = select(
        Zone.code, Zone.name, Zone.price_twd,
        func.coalesce(sold.c.tickets, 0), func.coalesce(sold.c.revenue, 0),
    ).outerjoin(sold, sold.c.code == Zone.code).where(Zone.event_id == event_id).order_by(Zone.code)
    with connection.execution_options(yield_per=BATCH_SIZE).execute(statement) as result:
        for row in result:
            _append(sheet, row)


def _write_orders(connection, sheet, event_id):
    # Core columns avoid an ORM identity map retaining half a million objects.
    statement = select(
        Order.id, Order.status, Order.created_at, Order.paid_at, Order.expires_at,
        Order.ticket_count, Order.subtotal_twd, Order.fee_twd, Order.total_twd, Order.currency,
    ).where(Order.event_id == event_id).order_by(Order.created_at, Order.id)
    with connection.execution_options(yield_per=BATCH_SIZE).execute(statement) as result:
        for row in result:
            _append(sheet, (str(row.id), *tuple(row)[1:]))


def build_report(session: Session, event_id: UUID, principal: Principal) -> Path:
    """Return an owned XLSX path. The response must delete it in a finally block."""
    bind = session.get_bind()
    engine: Engine = bind.engine if isinstance(bind, Connection) else bind
    workbook = None
    path = None
    try:
        # Auth's shared session has already autobegun. A separate connection is
        # necessary to set isolation before the report's first query/snapshot.
        with engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
            with connection.begin():
                connection.exec_driver_sql('SET TRANSACTION READ ONLY')
                event = connection.execute(select(
                    Event.id, Event.owner_id, Event.title,
                ).where(Event.id == event_id, Event.deleted_at.is_(None))).mappings().one_or_none()
                if event is None:
                    raise AppError(404, 'EVENT_NOT_FOUND', 'Activity not found')
                if principal.role not in {'ADMIN', 'VIEWER'} and event['owner_id'] != principal.id:
                    raise AppError(403, 'REPORT_FORBIDDEN', 'Only the activity owner can download this report')
                generated_at = connection.scalar(select(func.clock_timestamp()))
                descriptor, filename = tempfile.mkstemp(prefix='ticket-report-', suffix='.xlsx')
                os.close(descriptor)
                path = Path(filename)
                workbook = Workbook(write_only=True)
                summary = _sheet(workbook, 'Summary', ['項目', '數值／時間 (Asia/Taipei)'])
                _write_summary(connection, summary, event, generated_at)
                zones = _sheet(workbook, 'Zones', ['區域代碼', '區域名稱', '票價 TWD', '已付款張數', '票價營收 TWD（不含服務費）'])
                _write_zones(connection, zones, event_id)
                orders = _sheet(workbook, 'Orders', [
                    '訂單編號', '狀態', '建立時間 (Asia/Taipei)', '付款時間 (Asia/Taipei)',
                    '保留期限 (Asia/Taipei)', '張數', '票價小計 TWD', '服務費 TWD', '總額 TWD', '幣別',
                ])
                _write_orders(connection, orders, event_id)
                workbook.save(path)
                workbook.close()
        return path
    except BaseException:
        if workbook is not None:
            _discard_workbook(workbook)
        if path is not None:
            path.unlink(missing_ok=True)
        raise
