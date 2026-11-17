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
    os.execv(sys.executable,[sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(args.port),'--workers','1'])

if __name__=='__main__':main()
