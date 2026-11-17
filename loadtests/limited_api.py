#!/usr/bin/env python3
"""Launch one API process under a strict 512MiB address-space limit for export tests."""
import argparse
import os
import resource
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8000)
    args=parser.parse_args()
    limit=512*1024*1024
    resource.setrlimit(resource.RLIMIT_AS,(limit,limit))
    # RLIMIT_AS counts reserved address space, not memory in use: glibc reserves a
    # 64 MiB arena per malloc-contending thread and 8 MiB per thread stack, so a
    # process using ~110 MiB RSS was observed at 420 MiB VmSize and once failed a
    # request at the cap. Bound those reservations so the limit measures the export.
    os.environ.setdefault('MALLOC_ARENA_MAX','2')
    stack=2*1024*1024
    resource.setrlimit(resource.RLIMIT_STACK,(stack,resource.getrlimit(resource.RLIMIT_STACK)[1]))
    # No --workers flag: with it uvicorn becomes a supervisor whose child does the
    # export, and the measured PID would be the wrong process.
    os.execv(sys.executable,[sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(args.port)])

if __name__=='__main__':main()
