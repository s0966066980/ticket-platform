#!/usr/bin/env python3
"""Require executed tests, zero skips and zero failures in a JUnit artifact."""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report',type=Path)
    args=parser.parse_args()
    try:
        cases=list(ET.parse(args.report).getroot().iter('testcase'))
    except (OSError,ET.ParseError) as exc:
        parser.exit(1,f'Missing or invalid test evidence: {type(exc).__name__}\n')
    skipped=[case.get('name','unknown') for case in cases if case.find('skipped') is not None]
    failed=[case.get('name','unknown') for case in cases if case.find('failure') is not None or case.find('error') is not None]
    summary={'testcases':len(cases),'skipped':skipped,'failed':failed,'passed':bool(cases) and not skipped and not failed}
    print(json.dumps(summary,indent=2))
    raise SystemExit(0 if summary['passed'] else 1)

if __name__=='__main__':main()
