from datetime import datetime,timedelta,timezone
import hashlib,hmac,json,time
from uuid import UUID,uuid4
import pytest

pytestmark=pytest.mark.integration


def setup_order(client,db_session):
    from app.models import Seat
    credentials={"email":"payer@example.test","password":"Payer-test-123!"}
    assert client.post("/api/v1/auth/register",json=credentials).status_code==201
    token=client.post("/api/v1/auth/login",json=credentials).json()["token"]
    headers={"Authorization":"Bearer "+token}
    now=datetime.now(timezone.utc)
    event=client.post("/api/v1/events",headers=headers,json={"title":"Show","description":"","category":"music","venue":"Taipei","sales_start":(now-timedelta(hours=1)).isoformat(),"sales_end":(now+timedelta(days=1)).isoformat(),"zones":[{"code":"A","name":"A","price_twd":100}]}).json()
    seat=Seat(event_id=UUID(event["id"]),zone_id=UUID(event["zones"][0]["id"]),row_label="1",seat_number="1")
    db_session.add(seat);db_session.commit()
    assert client.post("/api/v1/events/"+event["id"]+"/publish",headers={**headers,"If-Match":str(event["version"])}).status_code==200
    order=client.post("/api/v1/events/"+event["id"]+"/bookings",headers={**headers,"Idempotency-Key":str(uuid4())},json={"seat_ids":[str(seat.id)]}).json()
    return headers,event,order


def test_payment_initiation_is_idempotent_and_uses_server_order_total(client,db_session):
    headers,event,order=setup_order(client,db_session)
    url="/api/v1/orders/"+order["id"]+"/payments"
    first=client.post(url,headers={**headers,"Idempotency-Key":"pay-once"},json={"outcome":"success","delay_seconds":0})
    assert first.status_code==202
    repeated=client.post(url,headers={**headers,"Idempotency-Key":"pay-once"},json={"outcome":"success","delay_seconds":0})
    assert repeated.json()==first.json()
    assert client.post(url,headers={**headers,"Idempotency-Key":"another"},json={"outcome":"success","delay_seconds":0}).status_code==409
