#!/usr/bin/env python3
"""Hold 2000 authenticated subscribers while measuring real seat mutations."""
import argparse
import asyncio
from datetime import datetime,timezone
import json
import math
import os
from pathlib import Path
import platform
import resource
import time
from urllib.parse import urlsplit,urlunsplit
import uuid


def percentile(values,p):
    return sorted(values)[max(0,math.ceil(len(values)*p)-1)] if values else None

async def run(args):
    import httpx
    from websockets.asyncio.client import connect
    manifest=json.loads(args.fixture.read_text())
    token=manifest['accounts'][0]['token'];event=manifest['event_id']
    base=urlsplit(args.base_url)
    ws_url=urlunsplit(('wss' if base.scheme=='https' else 'ws',base.netloc,f'/ws/events/{event}','',''))
    connections=[];tasks=[];ready=asyncio.Queue();gate=asyncio.Event();sem=asyncio.Semaphore(args.connect_concurrency)
    state={'baseline':0,'latest':0,'pending':None,'active':0,'resyncs':0,'frames':0,'errors':[]}
    samples=[];mutations=[];steady_start=None;authenticated_count=0
    result={'scenario':'websocket','requested_connections':args.connections,'required_connections':2000,'required_steady_seconds':60,
        'started_at':datetime.now(timezone.utc).isoformat(),'platform':platform.platform(),'cpu_count':os.cpu_count(),'event_id':event,'completed':False}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.with_suffix('.deliveries.jsonl').open('w') as raw:
      async with httpx.AsyncClient(base_url=args.base_url,headers={'Authorization':'Bearer '+token},timeout=30) as client:
        async def snapshot_version():
            response=await client.get(f'/api/v1/events/{event}/seats');response.raise_for_status()
            return response.json()['version']
        async def reader(index):
            nonlocal authenticated_count
            ws=None;authenticated=False
            try:
                async with sem:
                    ws=await connect(ws_url,proxy=None,max_queue=128,open_timeout=30,close_timeout=3)
                    connections.append(ws)
                    await ws.send(json.dumps({'type':'auth','token':token}))
                    reply=json.loads(await asyncio.wait_for(ws.recv(),10))
                    if reply.get('type')!='auth_ok': raise RuntimeError('WS application authentication rejected')
                    authenticated=True;authenticated_count+=1;state['active']+=1;await ready.put(index)
                await gate.wait();version=state['baseline']
                async for message in ws:
                    frame=json.loads(message);state['frames']+=1
                    if frame.get('type')=='heartbeat':
                        state['latest']=max(state['latest'],frame['version'])
                        if frame['version']>version:
                            state['resyncs']+=1;version=await snapshot_version()
                        continue
                    if frame.get('type')!='seat_changed': continue
                    current=frame['version'];state['latest']=max(state['latest'],current)
                    pending=state['pending']
                    # Record actual receipt even when a newer snapshot has already
                    # restored state, but never attribute an older phase's frame.
                    if pending and current>pending['minimum_version'] and index not in pending['seen']:
                        for patch in frame.get('changes',[]):
                            if patch.get('id',patch.get('seat_id'))==pending['seat_id'] and patch.get('status')==pending['status']:
                                elapsed=(time.monotonic()-pending['start'])*1000
                                pending['seen'].add(index);samples.append(elapsed)
                                raw.write(json.dumps({'connection':index,'phase':pending['phase'],'version':current,'status':pending['status'],'latency_ms':elapsed})+'\n')
                                if len(pending['seen'])==args.connections: pending['delivered'].set()
                    if current<=version: continue
                    if current!=version+1:
                        state['resyncs']+=1;version=await snapshot_version();continue
                    version=current
            except asyncio.CancelledError: raise
            except Exception as exc:
                state['errors'].append({'connection':index,'error':type(exc).__name__+': '+str(exc)})
            finally:
                if authenticated: state['active']-=1
        try:
            tasks=[asyncio.create_task(reader(i)) for i in range(args.connections)]
            await asyncio.wait_for(asyncio.gather(*(ready.get() for _ in range(args.connections))),args.warmup_timeout)
            state['baseline']=await snapshot_version();state['latest']=state['baseline'];gate.set();steady_start=time.monotonic()
            seat=manifest['seat_ids'][0]
            while time.monotonic()-steady_start<args.seconds:
                if state['active']!=args.connections or state['errors']: raise RuntimeError('A subscriber disconnected during steady interval')
                for status in ['HELD','AVAILABLE']:
                    pending={'seat_id':seat,'status':status,'phase':len(mutations),'start':time.monotonic(),'seen':set(),'delivered':asyncio.Event(),'minimum_version':state['latest']}
                    state['pending']=pending
                    if status=='HELD':
                        response=await client.post(f'/api/v1/events/{event}/bookings',json={'seat_ids':[seat]},headers={'Idempotency-Key':str(uuid.uuid4())})
                        response.raise_for_status();order_id=response.json()['id']
                    else:
                        response=await client.post(f'/api/v1/orders/{order_id}/cancel');response.raise_for_status()
                    try: await asyncio.wait_for(pending['delivered'].wait(),args.propagation_timeout)
                    except asyncio.TimeoutError: pass
                    mutations.append({'phase':pending['phase'],'seat_id':seat,'status':status,'delivered':len(pending['seen']),'expected':args.connections,'seconds':time.monotonic()-pending['start']})
                    state['pending']=None
                    if mutations[-1]['delivered']!=args.connections: raise RuntimeError('Not all subscribers received a mutation within propagation timeout')
                await asyncio.sleep(args.mutation_interval)
            result['completed']=True
        except Exception as exc:
            result['failure']=type(exc).__name__+': '+str(exc)
        finally:
            result['steady_seconds']=time.monotonic()-steady_start if steady_start else 0
            result['active_before_cleanup']=state['active']
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            await asyncio.gather(*(ws.close() for ws in connections),return_exceptions=True)
    result.update(authenticated_connections=authenticated_count,resyncs=state['resyncs'],received_frames=state['frames'],errors=state['errors'],mutations=mutations,
        delivery_samples=len(samples),latency_scope='HTTP mutation initiation to each observed seat_changed frame, client monotonic clock',
        percentile_method='nearest-rank',p50_ms=percentile(samples,.50),p95_ms=percentile(samples,.95),p99_ms=percentile(samples,.99))
    result['required_scenario_passed']=result['completed'] and args.connections==2000 and result['steady_seconds']>=60 and len(mutations)>0 and result['active_before_cleanup']==args.connections and not result['errors']
    args.output.write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
    return 0 if result['required_scenario_passed'] else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',required=True);parser.add_argument('--fixture',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--connections',type=int,default=2000);parser.add_argument('--seconds',type=float,default=60)
    parser.add_argument('--connect-concurrency',type=int,default=100);parser.add_argument('--warmup-timeout',type=float,default=180)
    parser.add_argument('--propagation-timeout',type=float,default=10);parser.add_argument('--mutation-interval',type=float,default=1)
    args=parser.parse_args()
    if min(args.connections,args.seconds,args.connect_concurrency,args.propagation_timeout,args.warmup_timeout)<=0: parser.error('Positive connection/time limits required')
    if resource.getrlimit(resource.RLIMIT_NOFILE)[0]<args.connections+256: parser.error('Raise file-descriptor soft limit above connections+256 before testing.')
    raise SystemExit(asyncio.run(run(args)))

if __name__=='__main__':main()
