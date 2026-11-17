"""3000 ready users, then 60 measured steady seconds; see loadtests/README.md."""
from collections import Counter
import itertools
import json
import os
from pathlib import Path
import platform
import random
import time
import uuid

from locust import HttpUser,LoadTestShape,between,events,task
from locust.exception import StopUser

TARGET=int(os.environ.get('LOAD_USERS','3000'))
STEADY=float(os.environ.get('LOAD_STEADY_SECONDS','60'))
SPAWN=float(os.environ.get('LOAD_SPAWN_RATE','100'))
OUTPUT=Path(os.environ.get('LOAD_RESULT','loadtests/results/booking.json'))
if min(TARGET,STEADY,SPAWN)<=0: raise ValueError('Positive users, duration and spawn rate required.')
fixture={};accounts=None;ready=0;outcomes=Counter();steady_start=None;steady_epoch=None;finished_at=None;environment=None;steady_stats=None;steady_outcomes=None

@events.init.add_listener
def setup(environment,**kwargs):
    globals()['environment']=environment
    options=environment.parsed_options
    if getattr(options,'master',False) or getattr(options,'worker',False):
        raise RuntimeError('This harness is single-process; use an explicitly coordinated account partition before distributed execution.')
    manifest=os.environ.get('LOAD_FIXTURE')
    if not manifest: raise RuntimeError('LOAD_FIXTURE must be the private preauthenticated manifest.')
    fixture.update(json.loads(Path(manifest).read_text()))
    if len(fixture['accounts'])<TARGET or any(not a.get('token') for a in fixture['accounts'][:TARGET]):
        raise RuntimeError('Prepare at least LOAD_USERS distinct authenticated accounts first.')
    if len({a['id'] for a in fixture['accounts'][:TARGET]})!=TARGET: raise RuntimeError('Distinct fixture accounts are required.')
    if not fixture['seat_ids']: raise RuntimeError('Fixture has no available seat IDs.')
    globals()['accounts']=iter(fixture['accounts'][:TARGET])

@events.quitting.add_listener
def save(environment,**kwargs):
    stop=finished_at or time.monotonic()
    elapsed=stop-steady_start if steady_start else 0
    result={'scenario':'booking','target_users':TARGET,'ready_users':ready,'steady_seconds':elapsed,'steady_completed':bool(finished_at),
        'steady_started_epoch':steady_epoch,'steady_finished_epoch':steady_epoch+elapsed if steady_epoch else None,
        'required_scenario_completed':TARGET==3000 and fixture.get('seat_count')==10000 and STEADY>=60 and bool(finished_at),'business_http_counts':steady_outcomes if steady_outcomes is not None else dict(outcomes),'steady_statistics':steady_stats,
        'python':platform.python_version(),'platform':platform.platform(),'cpu_count':os.cpu_count(),'event_id':fixture.get('event_id'),
        'measurement':'Locust statistics reset when all configured users finish on_start; raw CSV/HTML must accompany this file.'}
    OUTPUT.parent.mkdir(parents=True,exist_ok=True);OUTPUT.write_text(json.dumps(result,indent=2))
    if not finished_at: environment.process_exit_code=1

class TicketBuyer(HttpUser):
    wait_time=between(0.05,0.2)
    def on_start(self):
        global ready
        try: account=next(accounts)
        except StopIteration: raise StopUser()
        self.client.headers.update({'Authorization':'Bearer '+account['token']})
        self.pending=None;self.payment=None;ready+=1

    @task
    def purchase(self):
        if steady_start is None: return
        if self.payment:
            action=self.payment;url=f"/api/v1/orders/{action['order_id']}/payments";name='payment'
        else:
            if self.pending is None:
                body={'zone_id':fixture['zone_id'],'quantity':1} if random.random()<0.7 else {'seat_ids':[random.choice(fixture['seat_ids'])]}
                self.pending={'key':str(uuid.uuid4()),'body':body}
            action=self.pending;url=f"/api/v1/events/{fixture['event_id']}/bookings";name='booking'
        with self.client.post(url,json=action['body'],headers={'Idempotency-Key':action['key']},name=name,catch_response=True,timeout=15) as response:
            try: data=response.json()
            except ValueError: data={}
            code=data.get('code','')
            outcomes[f'{name}:{response.status_code}:{code}']+=1
            if 200<=response.status_code<300:
                if name=='booking':
                    if not data.get('id'): response.failure('Successful booking missing order ID');return
                    self.payment={'key':str(uuid.uuid4()),'body':{'outcome':'success','delay_seconds':0},'order_id':data['id']};self.pending=None
                else: self.payment=None
                response.success()
            elif response.status_code==409 and code!='IDEMPOTENCY_KEY_REUSED':
                response.success() # Expected business contention is still recorded separately above.
                if code!='IDEMPOTENCY_IN_PROGRESS':
                    if name=='booking': self.pending=None
                    else: self.payment=None
            elif response.status_code==0 or response.status_code>=500:
                response.failure('Transport/infrastructure failure; same key retained')
            else:
                response.failure(f'Unexpected HTTP {response.status_code} {code}')
                if name=='booking': self.pending=None
                else: self.payment=None

class RequiredPlateau(LoadTestShape):
    def tick(self):
        global steady_start,steady_epoch,finished_at,steady_stats,steady_outcomes
        if steady_start is None:
            if self.get_run_time()>180:
                self.runner.environment.process_exit_code=1
                return None
            if ready>=TARGET and self.get_current_user_count()==TARGET:
                steady_start=time.monotonic();steady_epoch=time.time();outcomes.clear();self.runner.stats.reset_all()
            return TARGET,SPAWN
        if self.get_current_user_count()!=TARGET:
            self.runner.environment.process_exit_code=1
            return None
        if time.monotonic()-steady_start>=STEADY:
            finished_at=time.monotonic()
            total=self.runner.stats.total
            steady_stats={'requests':total.num_requests,'failures':total.num_failures,'rps':total.num_requests/(finished_at-steady_start),
                'p50_ms':total.get_response_time_percentile(.50),'p95_ms':total.get_response_time_percentile(.95),'p99_ms':total.get_response_time_percentile(.99)}
            steady_outcomes=dict(outcomes)
            return None
        return TARGET,SPAWN
