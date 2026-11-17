#!/usr/bin/env python3
"""Generate reproducible fixtures in an explicitly disposable PostgreSQL database."""
import argparse
import csv
import json
import os
from pathlib import Path
import uuid
from urllib.parse import urlparse


def seats(rows, output):
    output.mkdir(parents=True, exist_ok=True)
    with (output/'seats.csv').open('w', newline='') as csv_file, (output/'seats.json').open('w') as json_file:
        writer = csv.DictWriter(csv_file, fieldnames=['zone_code','row_label','seat_number'])
        writer.writeheader()
        json_file.write('[\n')
        for index in range(rows):
            row = {'zone_code':'A', 'row_label':f'R{index//100+1:05d}', 'seat_number':str(index%100+1)}
            writer.writerow(row)
            json_file.write(('' if index == 0 else ',\n')+json.dumps(row))
        json_file.write('\n]\n')
    print(json.dumps({'rows':rows,'csv':str(output/'seats.csv'),'json':str(output/'seats.json')}))


def database_fixture(args):
    url = os.environ.get('LOAD_DATABASE_URL','')
    database = urlparse(url).path.removeprefix('/')
    if not url.startswith(('postgresql://','postgresql+psycopg://')) or not (database.startswith('ticket_test') or database.endswith('_load')):
        raise SystemExit('LOAD_DATABASE_URL must point to a disposable ticket_test* or *_load PostgreSQL database.')
    import psycopg
    from argon2 import PasswordHasher
    run = str(uuid.uuid4())
    password = os.environ.get('LOAD_PASSWORD','Load-fixture-only-2026!')
    password_hash = PasswordHasher().hash(password)
    count = args.orders if args.scenario == 'report' else 0
    paid = min(count//10, args.seats, args.users*4)
    key = lambda kind, number: uuid.UUID(__import__('hashlib').md5(f'{run}:{kind}:{number}'.encode()).hexdigest())
    event, zone = key('event',0), key('zone',0)
    with psycopg.connect(url.replace('postgresql+psycopg://','postgresql://',1)) as conn:
        version = conn.execute('SHOW server_version_num').fetchone()[0]
        if not 160000 <= int(version) < 170000: raise SystemExit('PostgreSQL 16 required.')
        values={'run':run,'users':args.users,'seats':args.seats,'orders':count,'paid':paid,'password_hash':password_hash,'event':event,'zone':zone,'owner':key('user',0)}
        conn.execute("""INSERT INTO users(id,email,password_hash,role,is_active,auth_version,created_at)
            SELECT md5(%(run)s||':user:'||i)::uuid, 'load-'||%(run)s||'-'||i||'@example.test',%(password_hash)s,'USER',true,0,clock_timestamp()-interval '2 days'
            FROM generate_series(0,%(users)s-1) i""",values)
        conn.execute("""INSERT INTO events(id,owner_id,title,description,category,venue,status,sales_start,sales_end,version,created_at)
            VALUES(%(event)s,%(owner)s,'Load fixture','Direct SQL benchmark data; not API test evidence','benchmark','fixture','PUBLISHED',clock_timestamp()-interval '2 days',clock_timestamp()+interval '1 day',1,clock_timestamp()-interval '2 days')""",values)
        conn.execute("INSERT INTO zones(id,event_id,code,name,price_twd,created_at) VALUES(%(zone)s,%(event)s,'A','Benchmark zone',100,clock_timestamp()-interval '2 days')",values)
        conn.execute("""INSERT INTO seats(id,event_id,zone_id,row_label,seat_number,status,version,created_at)
            SELECT md5(%(run)s||':seat:'||i)::uuid,%(event)s,%(zone)s,'R'||lpad(((i-1)/100+1)::text,5,'0'),((i-1)%%100+1)::text,'AVAILABLE',0,clock_timestamp()-interval '2 days'
            FROM generate_series(1,%(seats)s) i""",values)
        if count:
            # Historical canceled/expired orders reuse seats legitimately; paid seats are unique.
            conn.execute("""INSERT INTO orders(id,event_id,user_id,status,ticket_count,subtotal_twd,fee_twd,total_twd,currency,expires_at,canceled_at,expired_at,created_at)
                SELECT md5(%(run)s||':order:'||i)::uuid,%(event)s,
                md5(%(run)s||':user:'||CASE WHEN i<=%(paid)s THEN ((i-1)/4)::text ELSE ((i-1)%%%(users)s)::text END)::uuid,
                CASE WHEN i<=%(paid)s THEN 'PENDING_PAYMENT' WHEN i%%2=0 THEN 'CANCELED' ELSE 'EXPIRED' END,
                1,100,5,105,'TWD',clock_timestamp()-interval '1 day'+interval '5 minutes',
                CASE WHEN i>%(paid)s AND i%%2=0 THEN clock_timestamp()-interval '1 day'+interval '2 minutes' END,
                CASE WHEN i>%(paid)s AND i%%2<>0 THEN clock_timestamp()-interval '1 day'+interval '5 minutes' END,clock_timestamp()-interval '1 day'
                FROM generate_series(1,%(orders)s) i""",values)
            conn.execute("""INSERT INTO payments(id,order_id,status,amount_twd,currency,provider_charge_id,request_id,succeeded_at,created_at)
                SELECT md5(%(run)s||':payment:'||i)::uuid,md5(%(run)s||':order:'||i)::uuid,'SUCCEEDED',105,'TWD',%(run)s||':'||i,%(run)s,clock_timestamp()-interval '1 day'+interval '1 minute',clock_timestamp()-interval '1 day'
                FROM generate_series(1,%(paid)s) i""",values)
            conn.execute("""UPDATE orders SET status='PAID',accepted_payment_id=md5(%(run)s||':payment:'||i)::uuid,paid_at=clock_timestamp()-interval '1 day'+interval '1 minute'
                FROM generate_series(1,%(paid)s) i WHERE orders.id=md5(%(run)s||':order:'||i)::uuid""",values)
            conn.execute("""INSERT INTO order_items(id,order_id,event_id,seat_id,zone_code_snapshot,row_label_snapshot,seat_number_snapshot,unit_price_twd,sold_at)
                SELECT md5(%(run)s||':item:'||i)::uuid,md5(%(run)s||':order:'||i)::uuid,%(event)s,
                md5(%(run)s||':seat:'||((i-1)%%%(seats)s+1))::uuid,'A','R'||lpad((((i-1)%%%(seats)s)/100+1)::text,5,'0'),((i-1)%%%(seats)s%%100+1)::text,100,
                CASE WHEN i<=%(paid)s THEN clock_timestamp()-interval '1 day'+interval '1 minute' END FROM generate_series(1,%(orders)s) i""",values)
            conn.execute("""UPDATE seats SET status='SOLD',current_order_id=md5(%(run)s||':order:'||i)::uuid,version=1
                FROM generate_series(1,%(paid)s) i WHERE seats.id=md5(%(run)s||':seat:'||i)::uuid""",values)
        conn.execute('INSERT INTO event_streams(event_id,current_version,published_version) VALUES(%(event)s,0,0)',values)
        conn.execute('INSERT INTO user_event_quotas(user_id,event_id,max_tickets) SELECT id,%(event)s,4 FROM users WHERE email LIKE %(prefix)s',dict(values,prefix=f'load-{run}-%'))
    manifest={'scenario':args.scenario,'run_id':run,'event_id':str(event),'zone_id':str(zone),'seat_count':args.seats,'order_count':count,'paid_orders':paid,'direct_sql_fixture':True,
        'accounts':[{'id':str(key('user',i)),'email':f'load-{run}-{i}@example.test','password':password} for i in range(args.users)],
        'seat_ids':[str(key('seat',i)) for i in range(paid+1,args.seats+1)]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    descriptor=os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as target: json.dump(manifest,target)
    print(json.dumps({k:v for k,v in manifest.items() if k not in {'accounts','seat_ids'}}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='scenario',required=True)
    seat=sub.add_parser('seats',help='100K CSV/JSON import fixtures without a database')
    seat.add_argument('--rows',type=int,default=100000);seat.add_argument('--output',type=Path,required=True)
    for scenario in ['booking','report']:
        command=sub.add_parser(scenario,help='Create isolated DB fixture and private account manifest')
        command.add_argument('--users',type=int,default=3000);command.add_argument('--seats',type=int,default=10000)
        command.add_argument('--orders',type=int,default=500000 if scenario=='report' else 0)
        command.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.scenario=='seats':
        if not 1<=args.rows<=100000: parser.error('--rows must be 1..100000')
        seats(args.rows,args.output)
    else:
        if min(args.users,args.seats)<1 or args.orders<0: parser.error('Positive users/seats and nonnegative orders required')
        if args.output.exists(): parser.error('Output already exists; choose a new manifest path before adding data')
        database_fixture(args)

if __name__=='__main__': main()
