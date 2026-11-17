#!/usr/bin/env python3
"""Authenticate fixture accounts before the measured load interval."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time
from urllib.request import Request,urlopen


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',required=True);parser.add_argument('--fixture',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--workers',type=int,default=8)
    args=parser.parse_args()
    if args.output.exists(): parser.error('Output exists; choose a new private session manifest.')
    fixture=json.loads(args.fixture.read_text());start=time.monotonic()
    def login(account):
        request=Request(args.base_url.rstrip('/')+'/api/v1/auth/login',data=json.dumps({'email':account['email'],'password':account['password']}).encode(),headers={'Content-Type':'application/json'})
        with urlopen(request,timeout=60) as response: data=json.load(response)
        if not isinstance(data.get('token'),str): raise ValueError('Login response has no token')
        if data.get('user',{}).get('id')!=account['id']: raise ValueError('Authenticated user does not match fixture account')
        return {'id':account['id'],'email':account['email'],'token':data['token']}
    with ThreadPoolExecutor(max_workers=args.workers) as pool: accounts=list(pool.map(login,fixture['accounts']))
    fixture['accounts']=accounts;fixture['session_preparation_seconds']=time.monotonic()-start
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with os.fdopen(os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as output: json.dump(fixture,output)
    print(json.dumps({'accounts':len(accounts),'setup_seconds':fixture['session_preparation_seconds'],'output':str(args.output)}))

if __name__=='__main__': main()
