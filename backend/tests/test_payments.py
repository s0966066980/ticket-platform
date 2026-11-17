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


def setup_order_for(client,db_session,email,event):
    from app.models import Seat
    headers,user=account(client,email)
    seat=Seat(event_id=UUID(event["id"]),zone_id=UUID(event["zones"][0]["id"]),row_label="9",seat_number=email)
    db_session.add(seat);db_session.commit()
    order=client.post("/api/v1/events/"+event["id"]+"/bookings",headers={**headers,"Idempotency-Key":str(uuid4())},json={"seat_ids":[str(seat.id)]}).json()
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


def sign(path,body,*,secret=None,timestamp=None,method="POST"):
    from app.config import get_settings
    from app.payments import signature
    timestamp=timestamp or str(int(time.time()))
    return {"Content-Type":"application/json","X-Gateway-Timestamp":timestamp,"X-Gateway-Signature":signature(secret or get_settings().gateway_secret,timestamp,method,path,body)}


def callback_body(payment_id,order,status="SUCCEEDED",event_id=None,charge_id=None,**overrides):
    payload={"provider_event_id":event_id or "evt-"+uuid4().hex,"payment_id":payment_id,"order_id":order["id"],"provider_charge_id":charge_id or "ch-"+payment_id,"amount_twd":order["total_twd"],"currency":"TWD","status":status,"occurred_at":datetime.now(timezone.utc).isoformat(),"request_id":"req-"+uuid4().hex}
    payload.update(overrides)
    return json.dumps(payload,sort_keys=True,separators=(",",":")).encode()


def deliver(client,body,**sign_options):
    return client.post(CALLBACK_PATH,content=body,headers=sign(CALLBACK_PATH,body,**sign_options))


def seat_states(client,headers,event):
    return [seat["status"] for seat in client.get("/api/v1/events/"+event["id"]+"/seats",headers=headers).json()["items"]]


def test_successful_callback_marks_order_paid_and_emits_next_seat_version(client,db_session):
    from sqlalchemy import select
    from app.models import OrderItem,OutboxEvent,Payment,PaymentCallback
    headers,event,order=setup_order(client,db_session,count=2)
    payment_id=pay(client,headers,order).json()["id"]
    body=callback_body(payment_id,order)
    accepted=deliver(client,body)
    assert accepted.status_code==200
    assert accepted.json()=={"status":"processed","result":"PAID","payment_id":payment_id}
    paid=client.get("/api/v1/orders/"+order["id"],headers=headers).json()
    assert paid["status"]=="PAID"
    assert paid["paid_at"] is not None
    assert paid["payments"]==[{"id":payment_id,"status":"SUCCEEDED"}]
    assert seat_states(client,headers,event)==["SOLD","SOLD"]
    assert client.get("/api/v1/events/"+event["id"]+"/version",headers=headers).json()["version"]==2
    outbox=db_session.scalars(select(OutboxEvent).where(OutboxEvent.kind=="seat_changed").order_by(OutboxEvent.aggregate_version)).all()
    assert [row.aggregate_version for row in outbox]==[1,2]
    assert sorted(change["status"] for change in outbox[-1].payload["changes"])==["SOLD","SOLD"]
    payment=db_session.get(Payment,UUID(payment_id))
    assert payment.provider_charge_id=="ch-"+payment_id and payment.succeeded_at is not None
    assert all(item.sold_at is not None for item in db_session.scalars(select(OrderItem).where(OrderItem.order_id==UUID(order["id"]))))
    record=db_session.scalar(select(PaymentCallback))
    assert record.result_code=="PAID" and record.payment_id==UUID(payment_id)
    assert client.post("/api/v1/orders/"+order["id"]+"/cancel",headers=headers).status_code==409
    assert pay(client,headers,order).json()["code"]=="ORDER_NOT_PAYABLE"


def test_failed_callback_keeps_order_pending_and_allows_retry_until_deadline(client,db_session):
    headers,event,order=setup_order(client,db_session)
    first=pay(client,headers,order,outcome="failure").json()["id"]
    assert pay(client,headers,order).json()["code"]=="PAYMENT_IN_PROGRESS"
    failed=deliver(client,callback_body(first,order,status="FAILED"))
    assert failed.status_code==200
    assert failed.json()["result"]=="PAYMENT_FAILED"
    pending=client.get("/api/v1/orders/"+order["id"],headers=headers).json()
    assert pending["status"]=="PENDING_PAYMENT"
    assert pending["expires_at"]==order["expires_at"]
    assert pending["payments"]==[{"id":first,"status":"FAILED"}]
    assert seat_states(client,headers,event)==["HELD"]
    retry=pay(client,headers,order)
    assert retry.status_code==202
    assert deliver(client,callback_body(retry.json()["id"],order)).json()["result"]=="PAID"
    paid=client.get("/api/v1/orders/"+order["id"],headers=headers).json()
    assert paid["status"]=="PAID"
    assert [p["status"] for p in paid["payments"]]==["FAILED","SUCCEEDED"]


def test_duplicate_callback_replays_and_conflicting_event_id_is_rejected(client,db_session):
    headers,event,order=setup_order(client,db_session)
    payment_id=pay(client,headers,order).json()["id"]
    body=callback_body(payment_id,order,event_id="evt-fixed")
    first=deliver(client,body)
    assert first.status_code==200
    second=deliver(client,body)
    assert second.status_code==200
    assert second.json()==first.json()
    assert client.get("/api/v1/events/"+event["id"]+"/version",headers=headers).json()["version"]==2
    conflict=deliver(client,callback_body(payment_id,order,event_id="evt-fixed",status="FAILED"))
    assert conflict.status_code==409
    assert conflict.json()["code"]=="CALLBACK_EVENT_CONFLICT"
    assert client.get("/api/v1/orders/"+order["id"],headers=headers).json()["status"]=="PAID"


def test_callback_requires_fresh_service_signature_not_user_token(client,db_session):
    headers,event,order=setup_order(client,db_session)
    payment_id=pay(client,headers,order).json()["id"]
    body=callback_body(payment_id,order)
    missing=client.post(CALLBACK_PATH,content=body,headers={**headers,"Content-Type":"application/json"})
    assert missing.status_code==401
    assert missing.json()["code"]=="INVALID_GATEWAY_SIGNATURE"
    forged=deliver(client,body,secret="not-the-shared-secret")
    assert forged.status_code==401
    expired=deliver(client,body,timestamp=str(int(time.time())-3600))
    assert expired.status_code==401
    assert expired.json()["code"]=="EXPIRED_GATEWAY_SIGNATURE"
    tampered=client.post(CALLBACK_PATH,content=body.replace(b'"SUCCEEDED"',b'"FAILED"'),headers=sign(CALLBACK_PATH,body))
    assert tampered.status_code==401
    wrong_path=client.post(CALLBACK_PATH,content=body,headers=sign("/api/v1/other",body))
    assert wrong_path.status_code==401
    assert client.get("/api/v1/orders/"+order["id"],headers=headers).json()["status"]=="PENDING_PAYMENT"
    assert deliver(client,b'{"nope":1}').status_code==422
    assert deliver(client,body).status_code==200


def test_callback_with_wrong_amount_or_order_is_rejected_without_side_effects(client,db_session):
    headers,event,order=setup_order(client,db_session)
    other_headers,_,other=setup_order_for(client,db_session,"other@example.test",event)
    payment_id=pay(client,headers,order).json()["id"]
    amount=deliver(client,callback_body(payment_id,order,amount_twd=order["total_twd"]-1))
    assert amount.status_code==409
    assert amount.json()["code"]=="PAYMENT_MISMATCH"
    wrong_order=deliver(client,callback_body(payment_id,order,order_id=other["id"]))
    assert wrong_order.status_code==409
    assert wrong_order.json()["code"]=="PAYMENT_MISMATCH"
    unknown=deliver(client,callback_body(str(uuid4()),order))
    assert unknown.status_code==404
    assert client.get("/api/v1/orders/"+order["id"],headers=headers).json()["status"]=="PENDING_PAYMENT"
    assert client.get("/api/v1/orders/"+order["id"],headers=headers).json()["payments"]==[{"id":payment_id,"status":"PENDING"}]
    assert deliver(client,callback_body(payment_id,order)).json()["result"]=="PAID"


def force_expiry(db_session,order_id):
    from sqlalchemy import update
    from app.models import Order,Seat
    past=datetime.now(timezone.utc)-timedelta(days=1)
    db_session.execute(update(Order).where(Order.id==UUID(order_id)).values(expires_at=past).execution_options(synchronize_session=False))
    db_session.execute(update(Seat).where(Seat.current_order_id==UUID(order_id)).values(held_until=past).execution_options(synchronize_session=False))
    db_session.commit()


def reversal_rows(db_session):
    from sqlalchemy import select
    from app.models import OutboxEvent
    return db_session.scalars(select(OutboxEvent).where(OutboxEvent.kind=="payment_reversal_requested")).all()


def test_late_success_after_cancel_keeps_order_canceled_and_queues_reversal(client,db_session):
    from app.models import Payment
    headers,event,order=setup_order(client,db_session)
    payment_id=pay(client,headers,order).json()["id"]
    assert client.post("/api/v1/orders/"+order["id"]+"/cancel",headers=headers).json()["status"]=="CANCELED"
    assert seat_states(client,headers,event)==["AVAILABLE"]
    body=callback_body(payment_id,order,charge_id="ch-late")
    late=deliver(client,body)
    assert late.status_code==200
    assert late.json()["result"]=="LATE_PAYMENT_REVERSAL_PENDING"
    canceled=client.get("/api/v1/orders/"+order["id"],headers=headers).json()
    assert canceled["status"]=="CANCELED"
    assert canceled["payments"]==[{"id":payment_id,"status":"REVERSAL_PENDING"}]
    assert seat_states(client,headers,event)==["AVAILABLE"]
    payment=db_session.get(Payment,UUID(payment_id))
    assert payment.reversal_key=="reverse:ch-late" and payment.reversed_at is None
    rows=reversal_rows(db_session)
    assert len(rows)==1 and rows[0].payload=={"payment_id":payment_id,"provider_charge_id":"ch-late","idempotency_key":"reverse:ch-late"}
    assert rows[0].published_at is None
    # Replaying the same provider event, or a further success event, queues nothing more.
    assert deliver(client,body).json()==late.json()
    assert deliver(client,callback_body(payment_id,order,charge_id="ch-late")).json()["result"]=="REVERSAL_PENDING"
    assert len(reversal_rows(db_session))==1


def test_late_success_after_expiry_converges_order_releases_seats_and_queues_reversal(client,db_session):
    headers,event,order=setup_order(client,db_session,count=2)
    payment_id=pay(client,headers,order).json()["id"]
    force_expiry(db_session,order["id"])
    assert client.get("/api/v1/events/"+event["id"]+"/version",headers=headers).json()["version"]==1
    late=deliver(client,callback_body(payment_id,order))
    assert late.status_code==200
    assert late.json()["result"]=="LATE_PAYMENT_REVERSAL_PENDING"
    expired=client.get("/api/v1/orders/"+order["id"],headers=headers).json()
    assert expired["status"]=="EXPIRED"
    assert expired["payments"]==[{"id":payment_id,"status":"REVERSAL_PENDING"}]
    assert seat_states(client,headers,event)==["AVAILABLE","AVAILABLE"]
    assert client.get("/api/v1/events/"+event["id"]+"/version",headers=headers).json()["version"]==2
    assert len(reversal_rows(db_session))==1
    assert client.post("/api/v1/events/"+event["id"]+"/bookings",headers={**headers,"Idempotency-Key":"again"},json={"seat_ids":[item["seat_id"] for item in order["items"]]}).status_code==201


def test_late_success_cannot_release_a_seat_reclaimed_by_a_newer_order(client,db_session):
    headers,event,order=setup_order(client,db_session)
    payment_id=pay(client,headers,order).json()["id"]
    force_expiry(db_session,order["id"])
    other,_=account(client,"other@example.test")
    newer=client.post("/api/v1/events/"+event["id"]+"/bookings",headers={**other,"Idempotency-Key":"newer"},json={"seat_ids":[order["items"][0]["seat_id"]]})
    assert newer.status_code==201
    assert deliver(client,callback_body(payment_id,order)).json()["result"]=="LATE_PAYMENT_REVERSAL_PENDING"
    assert client.get("/api/v1/orders/"+order["id"],headers=headers).json()["status"]=="EXPIRED"
    assert seat_states(client,headers,event)==["HELD"]
    assert client.get("/api/v1/orders/"+newer.json()["id"],headers=other).json()["status"]=="PENDING_PAYMENT"


def test_extra_success_on_paid_order_is_reversed_not_double_sold(client,db_session):
    headers,event,order=setup_order(client,db_session)
    first=pay(client,headers,order,outcome="failure").json()["id"]
    assert deliver(client,callback_body(first,order,status="FAILED")).json()["result"]=="PAYMENT_FAILED"
    second=pay(client,headers,order).json()["id"]
    assert deliver(client,callback_body(second,order)).json()["result"]=="PAID"
    # The provider later reports the first attempt as successful after all.
    extra=deliver(client,callback_body(first,order))
    assert extra.json()["result"]=="EXTRA_PAYMENT_REVERSAL_PENDING"
    paid=client.get("/api/v1/orders/"+order["id"],headers=headers).json()
    assert paid["status"]=="PAID"
    assert [p["status"] for p in paid["payments"]]==["REVERSAL_PENDING","SUCCEEDED"]
    assert client.get("/api/v1/events/"+event["id"]+"/version",headers=headers).json()["version"]==2
