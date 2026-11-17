#!/usr/bin/env python3
"""Fail CI before testing when real PostgreSQL16 or Redis7 is unavailable."""
import argparse
import json
import os


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    url=os.environ.get('TEST_DATABASE_URL')
    redis_url=os.environ.get('REDIS_URL')
    if not url or not redis_url: parser.error('TEST_DATABASE_URL and REDIS_URL must be explicitly set.')
    from sqlalchemy import create_engine,text
    from sqlalchemy.engine import make_url
    from redis import Redis
    parsed=make_url(url).set(drivername='postgresql+psycopg')
    if not parsed.database or not parsed.database.startswith('ticket_test'):
        parser.error('An isolated ticket_test* database is required.')
    engine=create_engine(parsed,connect_args={'connect_timeout':5})
    try:
        with engine.connect() as connection:
            version=int(connection.scalar(text('SHOW server_version_num')))
            can_create=connection.execute(text('SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname=current_user')).scalar_one()
        if version//10000!=16: parser.error(f'Expected PostgreSQL16, got server_version_num={version}.')
        if not can_create: parser.error('Test role must create and drop its generated isolated databases.')
    finally:
        engine.dispose()
    client=Redis.from_url(redis_url,socket_connect_timeout=5,socket_timeout=5)
    try:
        if not client.ping(): parser.error('Redis PING failed.')
        redis_version=client.info('server')['redis_version']
        if redis_version.split('.')[0]!='7': parser.error(f'Expected Redis7, got {redis_version}.')
    finally:
        client.close()
    print(json.dumps({'postgres_server_version_num':version,'redis_version':redis_version,'database':parsed.database,'database_create_permission':bool(can_create)},indent=2))

if __name__=='__main__':main()
