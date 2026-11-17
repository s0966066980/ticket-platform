#!/usr/bin/env python3
"""Read-only PostgreSQL 16 consistency audit after load tests."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

CHECKS={
'duplicate_sold_seat': "SELECT count(*) FROM (SELECT i.seat_id FROM order_items i JOIN orders o ON o.id=i.order_id WHERE (:event IS NULL OR o.event_id=:event) AND i.sold_at IS NOT NULL GROUP BY i.seat_id HAVING count(*)>1) bad",
'paid_item_seat_mismatch': "SELECT count(*) FROM order_items i JOIN orders o ON o.id=i.order_id JOIN seats s ON s.id=i.seat_id WHERE (:event IS NULL OR o.event_id=:event) AND o.status='PAID' AND (i.sold_at IS NULL OR s.status<>'SOLD' OR s.current_order_id IS DISTINCT FROM o.id)",
'sold_seat_order_mismatch': "SELECT count(*) FROM seats s LEFT JOIN orders o ON o.id=s.current_order_id WHERE (:event IS NULL OR s.event_id=:event) AND s.status='SOLD' AND (o.id IS NULL OR o.status<>'PAID' OR NOT EXISTS(SELECT 1 FROM order_items i WHERE i.order_id=o.id AND i.seat_id=s.id AND i.sold_at IS NOT NULL))",
'order_count_or_amount_mismatch': "SELECT count(*) FROM (SELECT o.id FROM orders o LEFT JOIN order_items i ON i.order_id=o.id WHERE (:event IS NULL OR o.event_id=:event) GROUP BY o.id HAVING count(i.id)<>o.ticket_count OR coalesce(sum(i.unit_price_twd),0)<>o.subtotal_twd OR o.fee_twd<>(o.subtotal_twd*5+50)/100 OR o.total_twd<>o.subtotal_twd+o.fee_twd) bad",
'quota_exceeded': "SELECT count(*) FROM (SELECT user_id,event_id FROM orders WHERE (:event IS NULL OR event_id=:event) AND (status='PAID' OR (status='PENDING_PAYMENT' AND expires_at>:now)) GROUP BY user_id,event_id HAVING sum(ticket_count)>4) bad",
'active_order_lost_hold': "SELECT count(*) FROM orders o JOIN order_items i ON i.order_id=o.id JOIN seats s ON s.id=i.seat_id WHERE (:event IS NULL OR o.event_id=:event) AND o.status='PENDING_PAYMENT' AND o.expires_at>:now AND (s.status<>'HELD' OR s.current_order_id IS DISTINCT FROM o.id OR s.held_by IS DISTINCT FROM o.user_id OR s.held_until IS DISTINCT FROM o.expires_at)",
'active_hold_invalid_order': "SELECT count(*) FROM seats s LEFT JOIN orders o ON o.id=s.current_order_id WHERE (:event IS NULL OR s.event_id=:event) AND s.status='HELD' AND s.held_until>:now AND (o.id IS NULL OR o.status<>'PENDING_PAYMENT' OR o.expires_at IS DISTINCT FROM s.held_until)",
'paid_payment_mismatch': "SELECT count(*) FROM orders o LEFT JOIN payments p ON p.id=o.accepted_payment_id WHERE (:event IS NULL OR o.event_id=:event) AND o.status='PAID' AND (p.id IS NULL OR p.order_id<>o.id OR p.status<>'SUCCEEDED' OR p.amount_twd<>o.total_twd OR p.currency<>o.currency)",
'sold_item_nonpaid_order': "SELECT count(*) FROM order_items i JOIN orders o ON o.id=i.order_id WHERE (:event IS NULL OR o.event_id=:event) AND i.sold_at IS NOT NULL AND o.status<>'PAID'",
'event_version_gap': "SELECT count(*) FROM event_streams s WHERE (:event IS NULL OR s.event_id=:event) AND (s.current_version<>(SELECT count(*) FROM seat_events e WHERE e.event_id=s.event_id) OR s.current_version<>coalesce((SELECT max(version) FROM seat_events e WHERE e.event_id=s.event_id),0) OR s.published_version>s.current_version)",
'event_missing_outbox': "SELECT count(*) FROM seat_events e WHERE (:event IS NULL OR e.event_id=:event) AND NOT EXISTS(SELECT 1 FROM outbox_events b WHERE b.kind='seat_changed' AND b.aggregate_id::text=e.event_id::text AND b.aggregate_version=e.version)",
}
INFO={
'expired_uncleaned_orders':"SELECT count(*) FROM orders WHERE (:event IS NULL OR event_id=:event) AND status='PENDING_PAYMENT' AND expires_at<=:now",
'pending_reversals':"SELECT count(*) FROM payments p JOIN orders o ON o.id=p.order_id WHERE (:event IS NULL OR o.event_id=:event) AND p.status='REVERSAL_PENDING'",
'unpublished_outbox':"SELECT count(*) FROM outbox_events b WHERE (:event IS NULL OR b.aggregate_id::text=:event::text) AND b.published_at IS NULL",
}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--event-id',type=uuid.UUID)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--require-settled',action='store_true',help='Also fail for pending reversals or unpublished outbox')
    args=parser.parse_args()
    url=os.environ.get('LOAD_DATABASE_URL','')
    if not url: parser.error('LOAD_DATABASE_URL is required (never printed).')
    from sqlalchemy import create_engine,text
    engine=create_engine(url.replace('postgresql://','postgresql+psycopg://',1))
    with engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
        connection.execute(text('SET TRANSACTION READ ONLY'))
        version=connection.execute(text('SHOW server_version_num')).scalar_one()
        if not 160000<=int(version)<170000: parser.error('PostgreSQL 16 required.')
        now=connection.execute(text('SELECT clock_timestamp()')).scalar_one()
        params={'event':args.event_id,'now':now}
        # Explicit UUID cast resolves the NULL event parameter even with server-side binds.
        checks={name:int(connection.execute(text(sql.replace(':event','CAST(:event AS uuid)')),params).scalar_one()) for name,sql in CHECKS.items()}
        info={name:int(connection.execute(text(sql.replace(':event::text','CAST(:event AS text)').replace(':event','CAST(:event AS uuid)')),params).scalar_one()) for name,sql in INFO.items()}
    engine.dispose()
    passed=not any(checks.values()) and (not args.require_settled or not (info['pending_reversals'] or info['unpublished_outbox']))
    result={'checked_at':datetime.now(timezone.utc).isoformat(),'database_snapshot_time':now.isoformat(),'event_id':str(args.event_id) if args.event_id else None,'postgres_version_num':version,'passed':passed,'violations':checks,'informational':info}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
    raise SystemExit(0 if passed else 1)

if __name__=='__main__': main()
