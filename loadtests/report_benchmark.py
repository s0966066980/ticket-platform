#!/usr/bin/env python3
"""Stream an XLSX download and measure its single-process API peak RSS and limit."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import resource
import threading
import time
import uuid


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',required=True);parser.add_argument('--event-id',type=uuid.UUID,required=True)
    parser.add_argument('--api-pid',type=int,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    token=os.environ.get('LOAD_TOKEN')
    if not token: parser.error('LOAD_TOKEN must authenticate an authorized report reader.')
    import httpx
    from openpyxl import load_workbook
    args.output.parent.mkdir(parents=True,exist_ok=True)
    result={'scenario':'report','event_id':str(args.event_id),'started_at':datetime.now(timezone.utc).isoformat(),'api_pid':args.api_pid,'limit_bytes':512*1024*1024,'completed':False}
    peak=[0];errors=[];stop=threading.Event()
    hard_limit=resource.prlimit(args.api_pid,resource.RLIMIT_AS)[1]
    children=Path(f'/proc/{args.api_pid}/task/{args.api_pid}/children').read_text().strip()
    result['enforced_address_space_hard_limit_bytes']=hard_limit;result['single_process']=not children
    def sample():
        while not stop.is_set():
            try:
                for line in Path(f'/proc/{args.api_pid}/status').read_text().splitlines():
                    if line.startswith('VmRSS:'): peak[0]=max(peak[0],int(line.split()[1])*1024)
            except OSError as exc: errors.append(type(exc).__name__);return
            stop.wait(.02)
    thread=threading.Thread(target=sample,daemon=True);thread.start();start=time.monotonic()
    workbook_file=args.output.with_suffix('.xlsx')
    try:
        with httpx.Client(timeout=600,headers={'Authorization':'Bearer '+token}) as client:
            with client.stream('GET',args.base_url.rstrip('/')+f'/api/v1/events/{args.event_id}/report') as response:
                result['http_status']=response.status_code;response.raise_for_status()
                with workbook_file.open('wb') as target:
                    for chunk in response.iter_bytes(): target.write(chunk)
        result['download_seconds']=time.monotonic()-start;result['bytes']=workbook_file.stat().st_size
        stop.set();thread.join()
        book=load_workbook(workbook_file,read_only=True,data_only=True)
        try:
            result['sheets']=book.sheetnames
            sheet=book['Orders'];header=False;row_count=0;money_columns=[];invalid_money=0
            for row in sheet.iter_rows(values_only=True):
                if not header:
                    header=any(str(cell).strip().lower() in {'order_id','order id','訂單編號','訂單 id'} for cell in row if cell is not None)
                    if header: money_columns=[i for i,cell in enumerate(row) if any(word in str(cell).lower() for word in ['subtotal','fee','total','price','小計','服務費','總額','票價'])]
                elif row and row[0] is not None:
                    row_count+=1
                    invalid_money+=sum(i>=len(row) or isinstance(row[i],bool) or not isinstance(row[i],(int,float)) for i in money_columns)
            result['orders_header_found']=header;result['orders_rows']=row_count
            result['monetary_columns']=len(money_columns);result['invalid_monetary_cells']=invalid_money
            result['timezone_in_headers']=all(any('Asia/Taipei' in str(cell) for row in book[name].iter_rows(min_row=1,max_row=10,values_only=True) for cell in row if cell is not None) for name in ['Summary','Zones','Orders'])
        finally: book.close()
        result['completed']=True
    except Exception as exc:
        result['failure']=type(exc).__name__+': '+str(exc)
    finally:
        stop.set();thread.join();result['peak_api_rss_bytes']=peak[0];result['sampling_errors']=errors
    result['required_scenario_passed']=result['completed'] and result.get('orders_rows')==500000 and result.get('monetary_columns',0)>0 and result.get('invalid_monetary_cells')==0 and result.get('timezone_in_headers') and set(result.get('sheets',[]))=={'Summary','Zones','Orders'} and result['single_process'] and 0<hard_limit<=result['limit_bytes'] and 0<peak[0]<=result['limit_bytes'] and not errors
    args.output.write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
    raise SystemExit(0 if result['required_scenario_passed'] else 1)

if __name__=='__main__':main()
