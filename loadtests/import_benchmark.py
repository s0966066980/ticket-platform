#!/usr/bin/env python3
"""Create a draft event, upload seats and measure import completion with raw poll evidence."""
import argparse
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import time
import uuid


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',required=True);parser.add_argument('--file',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--timeout',type=float,default=180)
    parser.add_argument('--mode',choices=['all_or_nothing','skip_invalid'],default='all_or_nothing')
    args=parser.parse_args()
    token=os.environ.get('LOAD_TOKEN')
    if not token: parser.error('LOAD_TOKEN must authenticate an event creator.')
    import httpx
    now=datetime.now(timezone.utc)
    result={'scenario':'import','file_bytes':args.file.stat().st_size,'mode':args.mode,'polls':[],'completed':False}
    with httpx.Client(base_url=args.base_url,headers={'Authorization':'Bearer '+token},timeout=60) as client:
        response=client.post('/api/v1/events',json={'title':'Import benchmark '+str(uuid.uuid4()),'description':'Measured import fixture','category':'benchmark','venue':'fixture','sales_start':(now+timedelta(hours=1)).isoformat(),'sales_end':(now+timedelta(days=1)).isoformat(),'zones':[{'code':'A','name':'Benchmark','price_twd':100}]})
        response.raise_for_status();event=response.json();result['event_id']=event['id']
        start=time.monotonic()
        with args.file.open('rb') as source: response=client.post(f"/api/v1/events/{event['id']}/seats/import",files={'file':(args.file.name,source)},data={'mode':args.mode})
        response.raise_for_status();job=response.json();accepted=time.monotonic()
        result.update(job_id=job['id'],upload_and_accept_seconds=accepted-start)
        while time.monotonic()-start<args.timeout:
            response=client.get(f"/api/v1/imports/{job['id']}");response.raise_for_status();job=response.json()
            result['polls'].append({'elapsed_seconds':time.monotonic()-start,'job':job})
            if job['status'] in {'SUCCEEDED','FAILED_VALIDATION','FAILED_SYSTEM'}:
                result['completed']=job['status']=='SUCCEEDED';break
            time.sleep(0.5)
        result['accept_to_observed_finish_seconds']=time.monotonic()-accepted
        result['total_seconds']=time.monotonic()-start
        result['required_scenario_passed']=result['completed'] and job.get('inserted_rows')==100000 and result['total_seconds']<=60
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!='polls'},indent=2))
    raise SystemExit(0 if result['required_scenario_passed'] else 1)

if __name__=='__main__':main()
