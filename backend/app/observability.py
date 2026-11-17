"""Durable telemetry handoff and idempotent PostgreSQL collection.

Worker API (synchronous): collect_telemetry(), refresh_metrics(),
record_service_log(), connection_delta(), emit_connection_gauge().
CLI: python -m app.observability collect | ingest --input events.jsonl.
The ingest CLI is an operator boundary for replaying the versioned JSON event
protocol; it owns its transaction and never acknowledges input before commit.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import suppress
from datetime import datetime,timedelta,timezone
from functools import lru_cache
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from uuid import UUID,uuid4,uuid5,NAMESPACE_URL

from redis import Redis
from redis.exceptions import RedisError,ResponseError
from sqlalchemy import select,text,func
from sqlalchemy.dialects.postgresql import insert
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.database import new_session
from app.models import AccessLog,ServiceLog,SystemMetric,Alert,OutboxEvent,Payment

INSTANCE_ID=str(uuid4())
STREAM='ticket:telemetry:v1'
GROUP='postgres-v1'
LOGGER=logging.getLogger('ticket.telemetry')
_SECRET=re.compile(r'password|token|secret|authorization|cookie|signature',re.I)
_TRACE=re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')


def _safe(value,depth=0):
    if depth>5:return '[TRUNCATED]'
    if isinstance(value,dict):return {str(k)[:100]:('[REDACTED]' if _SECRET.search(str(k)) else _safe(v,depth+1)) for k,v in list(value.items())[:100]}
    if isinstance(value,(list,tuple)):return [_safe(v,depth+1) for v in value[:100]]
    if isinstance(value,str):return value[:2048]
    if value is None or isinstance(value,(int,float,bool)):return value
    return str(value)[:2048]


def _trace(value=None):
    if not value:
        from app.main import REQUEST_ID
        value=REQUEST_ID.get()
    return value if isinstance(value,str) and _TRACE.fullmatch(value) else str(uuid4())


def _atomic_json(path:Path,data):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    descriptor,name=tempfile.mkstemp(dir=path.parent,prefix='.pending-')
    try:
        with os.fdopen(descriptor,'w') as output:
            json.dump(data,output,separators=(',',':'));output.flush();os.fsync(output.fileno())
        os.replace(name,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        with suppress(FileNotFoundError):os.unlink(name)


class _Runtime:
    def __init__(self,database_url,redis_url,spool):
        self.client=Redis.from_url(redis_url,decode_responses=True,socket_connect_timeout=.2,socket_timeout=.2)
        self.spool=Path(spool)/('spool-'+INSTANCE_ID)
        self.clock_lock=threading.Lock();self.lock=threading.Lock()
        self.anchor=None;self.next_probe=0.;self.config_until=0.;self.durable_redis=False
        self.connections=0;self.lost=0;self.handoff_max_us=0;self.last_gauge=0.

    def clock(self):
        current=time.monotonic()
        if current>=self.next_probe and self.clock_lock.acquire(blocking=False):
            try:
                self.next_probe=current+1
                start=time.monotonic()
                try:
                    with new_session() as session:db_time=session.scalar(text('SELECT clock_timestamp()'))
                    stop=time.monotonic();self.anchor=(db_time,(start+stop)/2,int((stop-start)*500000))
                except Exception:
                    LOGGER.warning('telemetry DB clock unavailable')
            finally:self.clock_lock.release()
        if self.anchor:
            db_time,mono,error=self.anchor;age=max(0,time.monotonic()-mono)
            return db_time+timedelta(seconds=age),error+int(age*1000000),age<5
        return datetime.now(timezone.utc),60_000_000,False

    def emit(self,event):
        start=time.monotonic()
        delivered=False
        try:
            if start>=self.config_until:
                self.durable_redis=(self.client.config_get('appendonly').get('appendonly')=='yes' and self.client.config_get('appendfsync').get('appendfsync')=='always')
                self.config_until=start+5
            if self.durable_redis:
                self.client.xadd(STREAM,{'event':json.dumps(event,separators=(',',':'))})
                delivered=True
        except (RedisError,OSError):self.config_until=0
        if not delivered:
            try:_atomic_json(self.spool/f"record-{event['event_id']}.json",event);delivered=True
            except OSError:
                with self.lock:self.lost+=1
                LOGGER.error('TELEMETRY_PERSISTENCE_FAILED request_id=%s',event.get('request_id',''))
        with self.lock:self.handoff_max_us=max(self.handoff_max_us,int((time.monotonic()-start)*1000000))
        return delivered


@lru_cache(maxsize=8)
def _runtime(database_url,redis_url,spool):return _Runtime(database_url,redis_url,spool)


def _get_runtime():
    s=get_settings();return _runtime(s.database_url,s.redis_url,str(s.telemetry_spool_dir))


def _event(kind,**fields):
    observed,error,valid=_get_runtime().clock()
    return {'protocol':1,'kind':kind,'event_id':str(uuid4()),'instance_id':INSTANCE_ID,
            'observed_at':observed.isoformat(),'clock_error_us':error,'clock_valid':valid,**fields}


def record_service_log(service,code,*,request_id=None,level='INFO',context=None,span_id=None,parent_span_id=None):
    event=_event('service',request_id=_trace(request_id),service=service[:64],code=code[:100],level=level[:16],
                 context=_safe(context or {}),span_id=span_id,parent_span_id=parent_span_id)
    _get_runtime().emit(event);return event['event_id']


def connection_delta(delta):
    runtime=_get_runtime()
    with runtime.lock:
        runtime.connections=max(0,runtime.connections+delta);count=runtime.connections
        due=time.monotonic()-runtime.last_gauge>=1 or count==0
    if due:emit_connection_gauge()
    return count


def emit_connection_gauge():
    runtime=_get_runtime()
    with runtime.lock:
        count=runtime.connections;lost=runtime.lost;handoff=runtime.handoff_max_us
        runtime.last_gauge=time.monotonic();runtime.handoff_max_us=0
    event=_event('gauge',connections=count,lost_events=lost,handoff_us=handoff,request_id='heartbeat')
    runtime.emit(event)


class TelemetryMiddleware:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope.get('path') in {'/health','/ready','/docs','/redoc','/openapi.json'}:
            return await self.app(scope,receive,send)
        start=time.monotonic();status=500;body=bytearray();recorded=False;content_json=False
        async def record(exception=False):
            nonlocal recorded
            if recorded:return
            recorded=True;code=None
            if content_json and body:
                with suppress(ValueError,UnicodeError):code=json.loads(body).get('code')
            request_id=_trace(scope.get('state',{}).get('request_id'))
            route=getattr(scope.get('route'),'path','UNMATCHED')
            elapsed=max(0,int((time.monotonic()-start)*1000000))
            def handoff():
                event=_event('access',request_id=request_id,method=scope.get('method','GET'),route_template=route,
                    status_code=status,error_kind='UNHANDLED_EXCEPTION' if exception else ('HTTP_5XX' if status>=500 else None),
                    business_outcome=str(code or f'HTTP_{status}')[:100] if 400<=status<500 else None,duration_us=elapsed)
                _get_runtime().emit(event)
            await run_in_threadpool(handoff)
        async def observed_send(message):
            nonlocal status,content_json
            if message['type']=='http.response.start':
                status=message['status'];content_json=any(k.lower()==b'content-type' and b'json' in v for k,v in message.get('headers',[]))
            elif message['type']=='http.response.body':
                if content_json and len(body)<8192:body.extend(message.get('body',b'')[:8192-len(body)])
                if not message.get('more_body',False):await record()
            await send(message)
        try:await self.app(scope,receive,observed_send)
        except Exception:
            await record(True);raise


def install_observability(app):
    app.add_middleware(TelemetryMiddleware)
    @app.on_event('startup')
    async def start_gauges():
        async def heartbeat():
            while True:
                await asyncio.sleep(1)
                try:await asyncio.to_thread(emit_connection_gauge)
                except Exception:LOGGER.exception('telemetry heartbeat failed')
        app.state.telemetry_heartbeat=asyncio.create_task(heartbeat())
    @app.on_event('shutdown')
    async def stop_gauges():
        task=getattr(app.state,'telemetry_heartbeat',None)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):await task


def _normalize(event,now):
    if event.get('protocol')!=1 or event.get('kind') not in {'access','service','gauge'}:raise ValueError('Unknown telemetry protocol/kind')
    event=dict(event);event['event_id']=UUID(str(event['event_id']))
    observed=datetime.fromisoformat(event['observed_at'])
    if observed.tzinfo is None:raise ValueError('Telemetry time must include timezone')
    if not event.get('clock_valid',False):
        observed=now;event['clock_error_us']=max(5_000_001,int(event.get('clock_error_us',0)))
    event['observed_at']=observed;event['request_id']=_trace(event.get('request_id'))
    return event


def ingest_events(events,session_factory=new_session):
    """Operator/collector entry: commit one batch; caller may ACK only on return."""
    with session_factory() as session:
        now=session.scalar(text('SELECT clock_timestamp()'));access=[];service=[]
        for original in events:
            try:event=_normalize(original,now)
            except (ValueError,KeyError,TypeError):
                digest=hashlib.sha256(json.dumps(original,sort_keys=True,default=str).encode()).hexdigest()
                service.append({'event_id':uuid5(NAMESPACE_URL,'invalid-telemetry:'+digest),'request_id':'telemetry-invalid','service':'collector','level':'ERROR','code':'TELEMETRY_INVALID_RECORD','context':{'sha256':digest},'observed_at':now});continue
            if event['kind']=='access':
                access.append({key:event[key] for key in ['event_id','request_id','instance_id','method','route_template','status_code','error_kind','business_outcome','duration_us','observed_at','clock_error_us']})
            elif event['kind']=='service':
                service.append({key:event.get(key) for key in ['event_id','request_id','service','level','code','context','span_id','parent_span_id','observed_at']})
                service[-1]['context']=_safe(service[-1]['context'] or {})
            else:
                complete=bool(event.get('clock_valid')) and not event.get('lost_events',0)
                values=dict(source_id='gauge:'+str(event['instance_id']),bucket_at=event['observed_at'].replace(microsecond=0),window_seconds=1,
                    request_count=0,error_count=0,business_counts={},latency_summary={'handoff_us':event.get('handoff_us',0),'lost_events':event.get('lost_events',0)},
                    connections=max(0,int(event['connections'])),last_seen_at=event['observed_at'],data_complete=complete)
                stmt=insert(SystemMetric).values(**values)
                session.execute(stmt.on_conflict_do_update(index_elements=['source_id','bucket_at','window_seconds'],
                    set_={k:getattr(stmt.excluded,k) for k in ['connections','last_seen_at','data_complete','latency_summary']},where=SystemMetric.last_seen_at<=stmt.excluded.last_seen_at))
        if access:session.execute(insert(AccessLog).values(access).on_conflict_do_nothing(index_elements=['event_id']))
        if service:session.execute(insert(ServiceLog).values(service).on_conflict_do_nothing(index_elements=['event_id']))
        session.commit()
    return len(events)


def _redis_batch(limit):
    client=_get_runtime().client
    try:client.xgroup_create(STREAM,GROUP,id='0-0',mkstream=True)
    except ResponseError as exc:
        if 'BUSYGROUP' not in str(exc):raise
    claimed=client.xautoclaim(STREAM,GROUP,INSTANCE_ID,min_idle_time=10000,start_id='0-0',count=limit)
    messages=list(claimed[1]) if claimed else []
    if len(messages)<limit:
        for _,records in client.xreadgroup(GROUP,INSTANCE_ID,{STREAM:'>'},count=limit-len(messages),block=1):messages.extend(records)
    return messages


def ingest_nginx_logs(session_factory=new_session,limit=1000):
    source=Path(os.environ.get('NGINX_LOG_PATH','/data/proxy/access.jsonl'))
    if not source.exists():return 0
    checkpoint=get_settings().telemetry_spool_dir/'nginx-checkpoint.json'
    previous={}
    with suppress(OSError,ValueError):previous=json.loads(checkpoint.read_text())
    stat=source.stat();identity={'device':stat.st_dev,'inode':stat.st_ino}
    offset=previous.get('offset',0) if all(previous.get(k)==v for k,v in identity.items()) and previous.get('offset',0)<=stat.st_size else 0
    events=[]
    with source.open('rb') as lines:
        lines.seek(offset)
        for _ in range(limit):
            position=lines.tell();line=lines.readline(1_000_001)
            if not line or not line.endswith(b'\n'):break
            try:data=json.loads(line)
            except (ValueError,UnicodeError):data={'request_id':'proxy-invalid','observed_at':datetime.now(timezone.utc).isoformat(),'parse_error':True}
            events.append({'protocol':1,'kind':'service','event_id':str(uuid5(NAMESPACE_URL,f'nginx:{stat.st_dev}:{stat.st_ino}:{position}:'+hashlib.sha256(line).hexdigest())),
                'request_id':data.get('request_id'),'instance_id':'nginx','service':'nginx','level':'INFO','code':'HTTP_PROXY_REQUEST',
                'context':_safe({k:v for k,v in data.items() if k not in {'request_id','observed_at'}}),
                'observed_at':data.get('observed_at',datetime.now(timezone.utc).isoformat()),'clock_valid':True,'clock_error_us':0})
            offset=lines.tell()
    if events:
        ingest_events(events,session_factory)
        _atomic_json(checkpoint,{**identity,'offset':offset})
    return len(events)


def collect_telemetry(session_factory=new_session,limit=1000):
    if not 1<=limit<=10000:raise ValueError('Telemetry batch limit must be 1..10000')
    counts={'stream':0,'spool':0,'nginx':0,'redis_available':True}
    try:
        messages=_redis_batch(limit);events=[]
        for identity,fields in messages:
            try:events.append(json.loads(fields['event']))
            except (ValueError,KeyError):events.append({'invalid_stream_id':identity})
        if messages:
            ingest_events(events,session_factory) # DB commit MUST precede acknowledgment.
            ids=[identity for identity,_ in messages]
            client=_get_runtime().client;client.xack(STREAM,GROUP,*ids);client.xdel(STREAM,*ids)
        counts['stream']=len(messages)
    except (RedisError,OSError):counts['redis_available']=False
    files=[];events=[]
    root=get_settings().telemetry_spool_dir
    if root.exists():
        for path in root.glob('spool-*/record-*.json'):
            if len(files)>=limit:break
            if path.is_symlink():continue
            try:
                if path.stat().st_size>1_000_000:events.append({'oversized_spool':path.name})
                else:events.append(json.loads(path.read_text()))
                files.append(path)
            except FileNotFoundError:continue
            except ValueError:events.append({'invalid_spool':path.name});files.append(path)
    if files:
        ingest_events(events,session_factory)
        for path in files:
            with suppress(FileNotFoundError):path.unlink()
        counts['spool']=len(files)
    counts['nginx']=ingest_nginx_logs(session_factory,limit)
    return counts
