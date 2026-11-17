from datetime import datetime, timedelta, timezone
import pytest

pytestmark = pytest.mark.integration


def account(client, email="organizer@example.test"):
    password = "Organizer-test-123!"
    assert client.post("/api/v1/auth/register", json={"email":email,"password":password}).status_code == 201
    token = client.post("/api/v1/auth/login", json={"email":email,"password":password}).json()["token"]
    return {"Authorization": "Bearer " + token}


def event_payload(title="Concert"):
    now=datetime.now(timezone.utc)
    return {"title":title,"description":"Live show","category":"music","venue":"Taipei","sales_start":(now-timedelta(hours=1)).isoformat(),"sales_end":(now+timedelta(days=1)).isoformat(),"zones":[{"code":"A","name":"Zone A","price_twd":1000}]}


def test_owner_can_create_and_version_edit_draft_without_overwriting_changes(client):
    headers=account(client)
    created=client.post("/api/v1/events",headers=headers,json=event_payload())
    assert created.status_code == 201
    event=created.json()
    changed=client.patch("/api/v1/events/"+event["id"],headers={**headers,"If-Match":str(event["version"])},json={"title":"New title"})
    assert changed.status_code == 200
    stale=client.patch("/api/v1/events/"+event["id"],headers={**headers,"If-Match":str(event["version"])},json={"title":"Overwrite"})
    assert stale.status_code == 412
    assert client.get("/api/v1/events/"+event["id"],headers=headers).json()["title"] == "New title"


def add_seat(db_session, event):
    from uuid import UUID
    from app.models import Seat
    seat=Seat(event_id=UUID(event['id']),zone_id=UUID(event['zones'][0]['id']),row_label='1',seat_number='1')
    db_session.add(seat)
    db_session.commit()
    return seat


def test_two_simultaneous_edits_preserve_one_version(client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    headers=account(client)
    event=client.post('/api/v1/events',headers=headers,json=event_payload()).json()
    barrier=Barrier(2)
    def edit(title):
        barrier.wait()
        return client.patch('/api/v1/events/'+event['id'],headers={**headers,'If-Match':str(event['version'])},json={'title':title})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(edit,['First','Second']))
    assert sorted(response.status_code for response in responses) == [200,412]
    saved=client.get('/api/v1/events/'+event['id'],headers=headers).json()
    assert saved['version'] == event['version']+1
    assert saved['title'] == next(response.json()['title'] for response in responses if response.status_code==200)


def test_publish_requires_seats_and_freezes_configuration_but_allows_display_edits(client,db_session):
    headers=account(client)
    event=client.post('/api/v1/events',headers=headers,json=event_payload()).json()
    url='/api/v1/events/'+event['id']
    assert client.post(url+'/publish',headers={**headers,'If-Match':str(event['version'])}).status_code==409
    add_seat(db_session,event)
    published=client.post(url+'/publish',headers={**headers,'If-Match':str(event['version'])})
    assert published.status_code==200
    event=published.json()
    assert event['status']=='PUBLISHED'
    frozen=client.patch(url,headers={**headers,'If-Match':str(event['version'])},json={'sales_end':(datetime.now(timezone.utc)+timedelta(days=2)).isoformat()})
    assert frozen.status_code==409
    changed=client.patch(url,headers={**headers,'If-Match':str(event['version'])},json={'description':'Updated program'})
    assert changed.status_code==200
    assert changed.json()['description']=='Updated program'


def test_other_user_cannot_read_draft_or_modify_owners_activity(client):
    owner=account(client)
    event=client.post('/api/v1/events',headers=owner,json=event_payload()).json()
    other=account(client,'other@example.test')
    url='/api/v1/events/'+event['id']
    assert client.get(url,headers=other).status_code==404
    assert client.patch(url,headers={**other,'If-Match':str(event['version'])},json={'title':'Hijack'}).status_code==403
    assert client.delete(url,headers=other).status_code==403


def test_catalog_snapshot_retains_old_projection_order_and_deleted_item(client):
    headers=account(client)
    created={title:client.post('/api/v1/events',headers=headers,json=event_payload(title)).json() for title in ['A','B','C']}
    query={'sort':'title','limit':1}
    first=client.get('/api/v1/events',headers=headers,params=query).json()
    assert [event['title'] for event in first['items']]==['A']
    changed=client.patch('/api/v1/events/'+created['B']['id'],headers={**headers,'If-Match':str(created['B']['version'])},json={'title':'Z'})
    assert changed.status_code==200
    assert client.delete('/api/v1/events/'+created['C']['id'],headers=headers).status_code==204
    assert client.post('/api/v1/events',headers=headers,json=event_payload('D')).status_code==201
    second=client.get('/api/v1/events',headers=headers,params={**query,'snapshot_id':first['snapshot_id'],'cursor':first['next_cursor']}).json()
    assert [event['title'] for event in second['items']]==['B']
    third=client.get('/api/v1/events',headers=headers,params={**query,'snapshot_id':first['snapshot_id'],'cursor':second['next_cursor']}).json()
    assert [event['title'] for event in third['items']]==['C']
    assert third['next_cursor'] is None
    fresh=client.get('/api/v1/events',headers=headers,params={'sort':'title','limit':100}).json()
    assert [event['title'] for event in fresh['items']]==['A','D','Z']


def test_catalog_snapshot_expiry_requires_explicit_refresh(client,db_session):
    from uuid import UUID
    from sqlalchemy import update
    from app.models import ListSnapshot
    headers=account(client)
    client.post('/api/v1/events',headers=headers,json=event_payload())
    page=client.get('/api/v1/events',headers=headers).json()
    db_session.execute(update(ListSnapshot).where(ListSnapshot.id==UUID(page['snapshot_id'])).values(expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
    db_session.commit()
    expired=client.get('/api/v1/events',headers=headers,params={'snapshot_id':page['snapshot_id']})
    assert expired.status_code==410
    assert expired.json()['code']=='SNAPSHOT_EXPIRED'


def test_expired_hold_is_available_in_coherent_seat_snapshot_and_order_history_prevents_delete(client,db_session):
    from uuid import UUID
    from app.models import Order
    headers=account(client)
    event=client.post('/api/v1/events',headers=headers,json=event_payload()).json()
    seat=add_seat(db_session,event)
    old_time=datetime.now(timezone.utc)-timedelta(days=1)
    order=Order(event_id=UUID(event['id']),user_id=UUID(event['owner_id']),status='PENDING_PAYMENT',ticket_count=1,subtotal_twd=1000,fee_twd=50,total_twd=1050,expires_at=old_time)
    db_session.add(order)
    db_session.flush()
    seat.status='HELD'
    seat.current_order_id=order.id
    seat.held_by=UUID(event['owner_id'])
    seat.held_until=old_time
    db_session.commit()
    snapshot=client.get('/api/v1/events/'+event['id']+'/seats',headers=headers)
    assert snapshot.status_code==200
    assert snapshot.json()['items'][0]['status']=='AVAILABLE'
    assert snapshot.json()['items'][0]['held_until'] is None
    assert snapshot.json()['version']==0
    assert client.delete('/api/v1/events/'+event['id'],headers=headers).status_code==409
