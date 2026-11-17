from datetime import datetime,timedelta,timezone
from uuid import UUID,uuid4
import pytest

pytestmark=pytest.mark.integration


def account(client,email):
    credentials={"email":email,"password":"Buyer-test-123!"}
    assert client.post("/api/v1/auth/register",json=credentials).status_code==201
    response=client.post("/api/v1/auth/login",json=credentials).json()
    return {"Authorization":"Bearer "+response["token"]},response["user"]


def sale(client,db_session,count=8,price=10):
    from app.models import Seat
    headers,user=account(client,"owner@example.test")
    now=datetime.now(timezone.utc)
    payload={"title":"Sale","description":"","category":"music","venue":"Taipei","sales_start":(now-timedelta(hours=1)).isoformat(),"sales_end":(now+timedelta(days=1)).isoformat(),"zones":[{"code":"A","name":"A","price_twd":price}]}
    event=client.post("/api/v1/events",headers=headers,json=payload).json()
    seats=[Seat(event_id=UUID(event["id"]),zone_id=UUID(event["zones"][0]["id"]),row_label="1",seat_number=str(i+1)) for i in range(count)]
    db_session.add_all(seats);db_session.commit()
    assert client.post("/api/v1/events/"+event["id"]+"/publish",headers={**headers,"If-Match":str(event["version"])}).status_code==200
    return event,[str(s.id) for s in seats],headers


def book(client,headers,event,seats,key=None):
    return client.post("/api/v1/events/"+event["id"]+"/bookings",headers={**headers,"Idempotency-Key":key or str(uuid4())},json={"seat_ids":seats})


def test_booking_has_rounded_order_fee_and_retry_replays_same_order(client,db_session):
    event,seats,headers=sale(client,db_session)
    first=book(client,headers,event,seats[:1],"same-request")
    assert first.status_code==201
    assert first.json()["subtotal_twd"]==10
    assert first.json()["fee_twd"]==1
    assert first.json()["total_twd"]==11
    second=book(client,headers,event,seats[:1],"same-request")
    assert second.status_code==201
    assert second.json()==first.json()


def test_same_key_with_different_selection_is_conflict(client,db_session):
    event,seats,headers=sale(client,db_session)
    assert book(client,headers,event,seats[:1],'fixed-key').status_code==201
    conflict=book(client,headers,event,seats[1:2],'fixed-key')
    assert conflict.status_code==409
    assert conflict.json()['code']=='IDEMPOTENCY_KEY_REUSED'


def test_two_accounts_competing_for_one_seat_have_only_one_success(client,db_session):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    event,seats,owner=sale(client,db_session)
    other,_=account(client,'other@example.test')
    barrier=Barrier(2)
    def compete(headers):
        barrier.wait()
        return book(client,headers,event,seats[:1])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(compete,[owner,other]))
    assert sorted(response.status_code for response in responses)==[201,409]
    snapshot=client.get('/api/v1/events/'+event['id']+'/seats',headers=owner).json()
    assert sum(seat['status']=='HELD' for seat in snapshot['items'])==1


def test_concurrent_different_keys_cannot_exceed_account_quota(client,db_session):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    event,seats,headers=sale(client,db_session)
    barrier=Barrier(2)
    def compete(selection):
        barrier.wait()
        return book(client,headers,event,selection)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(compete,[seats[:3],seats[3:5]]))
    assert sorted(response.status_code for response in responses)==[201,409]
    orders=client.get('/api/v1/orders',headers=headers).json()['items']
    assert sum(order['ticket_count'] for order in orders if order['status']=='PENDING_PAYMENT')<=4


def test_region_allocation_is_full_and_distinct_across_accounts(client,db_session):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    event,seats,owner=sale(client,db_session)
    other,_=account(client,'other@example.test')
    barrier=Barrier(2)
    def reserve(headers):
        barrier.wait()
        return client.post('/api/v1/events/'+event['id']+'/bookings',headers={**headers,'Idempotency-Key':str(uuid4())},json={'zone_id':event['zones'][0]['id'],'quantity':4})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(reserve,[owner,other]))
    assert [response.status_code for response in responses]==[201,201]
    selected=[item['seat_id'] for response in responses for item in response.json()['items']]
    assert len(selected)==8
    assert len(set(selected))==8


def test_cancel_returns_quota_and_seats_and_emits_next_version(client,db_session):
    event,seats,headers=sale(client,db_session,count=4)
    first=book(client,headers,event,seats).json()
    assert client.get('/api/v1/events/'+event['id']+'/version',headers=headers).json()['version']==1
    canceled=client.post('/api/v1/orders/'+first['id']+'/cancel',headers=headers)
    assert canceled.status_code==200
    assert canceled.json()['status']=='CANCELED'
    assert client.post('/api/v1/orders/'+first['id']+'/cancel',headers=headers).json()['status']=='CANCELED'
    assert client.get('/api/v1/events/'+event['id']+'/version',headers=headers).json()['version']==2
    assert book(client,headers,event,seats).status_code==201


def force_expiry(db_session,order_id):
    from sqlalchemy import update
    from app.models import Order,Seat
    past=datetime.now(timezone.utc)-timedelta(days=1)
    db_session.execute(update(Order).where(Order.id==UUID(order_id)).values(expires_at=past).execution_options(synchronize_session=False))
    db_session.execute(update(Seat).where(Seat.current_order_id==UUID(order_id)).values(held_until=past).execution_options(synchronize_session=False))
    db_session.commit()


def test_old_expired_order_cannot_release_a_reclaimed_seat(client,db_session):
    event,seats,owner=sale(client,db_session,count=1)
    old=book(client,owner,event,seats).json()
    force_expiry(db_session,old['id'])
    other,_=account(client,'other@example.test')
    newer=book(client,other,event,seats)
    assert newer.status_code==201
    ended=client.post('/api/v1/orders/'+old['id']+'/cancel',headers=owner)
    assert ended.json()['status']=='EXPIRED'
    snapshot=client.get('/api/v1/events/'+event['id']+'/seats',headers=owner).json()
    assert snapshot['items'][0]['status']=='HELD'
    assert client.get('/api/v1/orders/'+newer.json()['id'],headers=other).json()['status']=='PENDING_PAYMENT'


def test_deletion_and_first_booking_cannot_both_succeed(client,db_session):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    event,seats,owner=sale(client,db_session,count=1)
    buyer,_=account(client,'other@example.test')
    barrier=Barrier(2)
    def deletion():
        barrier.wait()
        return client.delete('/api/v1/events/'+event['id'],headers=owner)
    def purchase():
        barrier.wait()
        return book(client,buyer,event,seats)
    with ThreadPoolExecutor(max_workers=2) as pool:
        left=pool.submit(deletion);right=pool.submit(purchase)
        deleted,reserved=left.result(),right.result()
    assert (deleted.status_code,reserved.status_code) in {(204,404),(409,201)}


def test_in_progress_and_expired_claims_have_distinct_recovery_behavior(client,db_session):
    from datetime import timedelta
    from app.idempotency import request_hash
    from app.models import IdempotencyKey
    event,seats,headers=sale(client,db_session,count=2)
    now=datetime.now(timezone.utc)
    for key,lease,selection in [('busy',now+timedelta(minutes=1),seats[:1]),('recover',now-timedelta(minutes=1),seats[1:2])]:
        db_session.add(IdempotencyKey(user_id=UUID(event['owner_id']),operation='booking:'+event['id'],key=key,request_hash=request_hash({'seat_ids':selection}),state='IN_PROGRESS',generation=1,lease_until=lease,expires_at=now+timedelta(days=1)))
    db_session.commit()
    busy=book(client,headers,event,seats[:1],'busy')
    assert busy.status_code==409
    assert busy.json()['code']=='IDEMPOTENCY_IN_PROGRESS'
    assert busy.headers['Retry-After']=='1'
    recovered=book(client,headers,event,seats[1:2],'recover')
    assert recovered.status_code==201
    assert book(client,headers,event,seats[1:2],'recover').json()==recovered.json()


def test_auth_releases_connection_for_catalog_and_claims_with_single_connection_pool(client,db_session,monkeypatch):
    from app.config import get_settings
    from app.database import get_engine
    event,seats,headers=sale(client,db_session,count=1)
    get_engine().dispose()
    get_engine.cache_clear()
    get_settings.cache_clear()
    with monkeypatch.context() as settings:
        settings.setenv('DATABASE_POOL_SIZE','1')
        settings.setenv('DATABASE_MAX_OVERFLOW','0')
        try:
            assert client.get('/api/v1/auth/me',headers=headers).status_code==200
            assert client.get('/api/v1/events',headers=headers).status_code==200
            assert book(client,headers,event,seats).status_code==201
        finally:
            get_engine().dispose()
            get_engine.cache_clear()
            get_settings.cache_clear()


def test_application_error_preserves_retry_after_and_request_id_over_http():
    from fastapi.testclient import TestClient
    from app.errors import AppError
    from app.main import create_app
    app=create_app()
    @app.get('/test-busy')
    def busy():
        raise AppError(409,'IDEMPOTENCY_IN_PROGRESS','Still processing',headers={'Retry-After':'1'})
    with TestClient(app) as test_client:
        result=test_client.get('/test-busy',headers={'X-Request-ID':'retry-header-check'})
    assert result.status_code==409
    assert result.headers['Retry-After']=='1'
    assert result.headers['X-Request-ID']=='retry-header-check'
    assert result.json()['request_id']=='retry-header-check'
