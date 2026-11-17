"""Payment seams: HTTP API, signed callbacks, the mock gateway app and worker entry points."""
from datetime import datetime,timedelta,timezone
import json,time
from uuid import UUID,uuid4
import pytest

pytestmark=pytest.mark.integration
CALLBACK_PATH='/api/v1/payments/callback'


def account(client,email,role='USER'):
    credentials={"email":email,"password":"Payer-test-123!"}
    assert client.post("/api/v1/auth/register",json=credentials).status_code==201
    if role!='USER':
        from sqlalchemy import update
        from app.database import new_session
        from app.models import User
        with new_session() as session:
            session.execute(update(User).where(User.email==email).values(role=role));session.commit()
    response=client.post("/api/v1/auth/login",json=credentials).json()
    return {"Authorization":"Bearer "+response["token"]},response["user"]


def setup_order(client,db_session,count=1,price=100):
    from app.models import Seat
    headers,user=account(client,"payer@example.test")
    now=datetime.now(timezone.utc)
    event=client.post("/api/v1/events",headers=headers,json={"title":"Show","description":"","category":"music","venue":"Taipei","sales_start":(now-timedelta(hours=1)).isoformat(),"sales_end":(now+timedelta(days=1)).isoformat(),"zones":[{"code":"A","name":"A","price_twd":price}]}).json()
    seats=[Seat(event_id=UUID(event["id"]),zone_id=UUID(event["zones"][0]["id"]),row_label="1",seat_number=str(i+1)) for i in range(count)]
    db_session.add_all(seats);db_session.commit()
    assert client.post("/api/v1/events/"+event["id"]+"/publish",headers={**headers,"If-Match":str(event["version"])}).status_code==200
    order=client.post("/api/v1/events/"+event["id"]+"/bookings",headers={**headers,"Idempotency-Key":str(uuid4())},json={"seat_ids":[str(s.id) for s in seats]}).json()
    assert order["status"]=="PENDING_PAYMENT"
    return headers,event,order


def pay(client,headers,order,outcome="success",key=None,delay=0):
    return client.post("/api/v1/orders/"+order["id"]+"/payments",headers={**headers,"Idempotency-Key":key or str(uuid4())},json={"outcome":outcome,"delay_seconds":delay})


def test_payment_initiation_is_idempotent_and_uses_server_order_total(client,db_session):
    from app.models import Payment
    headers,event,order=setup_order(client,db_session)
    first=pay(client,headers,order,key="pay-once")
    assert first.status_code==202
    assert first.json()["status"]=="PENDING"
    repeated=pay(client,headers,order,key="pay-once")
    assert repeated.status_code==202
    assert repeated.json()==first.json()
    payment=db_session.get(Payment,UUID(first.json()["id"]))
    assert payment.amount_twd==order["total_twd"]==105
    assert client.get("/api/v1/orders/"+order["id"],headers=headers).json()["payments"]==[{"id":first.json()["id"],"status":"PENDING"}]


def test_second_pending_attempt_and_reused_key_are_conflicts(client,db_session):
    headers,event,order=setup_order(client,db_session)
    assert pay(client,headers,order,key="first").status_code==202
    reused=pay(client,headers,order,outcome="failure",key="first")
    assert reused.status_code==409
    assert reused.json()["code"]=="IDEMPOTENCY_KEY_REUSED"
    second=pay(client,headers,order,key="second")
    assert second.status_code==409
    assert second.json()["code"]=="PAYMENT_IN_PROGRESS"
    assert pay(client,headers,order,key="second").json()==second.json()


def test_concurrent_attempts_with_different_keys_leave_one_pending_payment(client,db_session):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    headers,event,order=setup_order(client,db_session)
    barrier=Barrier(2)
    def attempt(key):
        barrier.wait()
        return pay(client,headers,order,key=key)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(attempt,["left","right"]))
    assert sorted(response.status_code for response in responses)==[202,409]
    assert len(client.get("/api/v1/orders/"+order["id"],headers=headers).json()["payments"])==1


def test_in_progress_claim_and_viewer_are_rejected(client,db_session):
    from app.idempotency import request_hash
    from app.models import IdempotencyKey
    headers,event,order=setup_order(client,db_session)
    now=datetime.now(timezone.utc)
    db_session.add(IdempotencyKey(user_id=UUID(order["user_id"]),operation="payment:"+order["id"],key="busy",request_hash=request_hash({"outcome":"success","delay_seconds":0.0}),state="IN_PROGRESS",generation=1,lease_until=now+timedelta(minutes=1),expires_at=now+timedelta(days=1)))
    db_session.commit()
    busy=pay(client,headers,order,key="busy")
    assert busy.status_code==409
    assert busy.json()["code"]=="IDEMPOTENCY_IN_PROGRESS"
    assert busy.headers["Retry-After"]=="1"
    viewer,_=account(client,"viewer@example.test",role="VIEWER")
    assert pay(client,viewer,order).status_code==403
    stranger,_=account(client,"stranger@example.test")
    assert pay(client,stranger,order).status_code==404
    assert pay(client,headers,order,outcome="maybe").status_code==422
    assert client.post("/api/v1/orders/"+order["id"]+"/payments",headers=headers,json={"outcome":"success"}).status_code==422


def test_canceled_or_expired_order_is_not_payable(client,db_session):
    from sqlalchemy import update
    from app.models import Order
    headers,event,order=setup_order(client,db_session,count=2)
    db_session.execute(update(Order).where(Order.id==UUID(order["id"])).values(expires_at=datetime.now(timezone.utc)-timedelta(minutes=1)));db_session.commit()
    expired=pay(client,headers,order)
    assert expired.status_code==409
    assert expired.json()["code"]=="ORDER_NOT_PAYABLE"
