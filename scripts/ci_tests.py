#!/usr/bin/env python3
"""Public CLI regression tests for the CI evidence gate."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]

class EvidenceGateTests(unittest.TestCase):
    def run_report(self,content):
        with tempfile.TemporaryDirectory() as directory:
            report=Path(directory)/'junit.xml'
            report.write_text(content)
            return subprocess.run([sys.executable,str(ROOT/'scripts/ci_assert_junit.py'),str(report)],capture_output=True,text=True)

    def test_real_executed_passing_case_is_accepted(self):
        result=self.run_report('<testsuites><testsuite><testcase name="public_api"/></testsuite></testsuites>')
        self.assertEqual(result.returncode,0,result.stderr)

    def test_one_skipped_database_case_cannot_hide_among_passes(self):
        result=self.run_report('<testsuites><testsuite><testcase name="health"/><testcase name="database_booking"><skipped message="DB missing"/></testcase></testsuite></testsuites>')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('database_booking',result.stdout)

    def test_empty_or_failing_evidence_is_rejected(self):
        for content in ['<testsuites/>','<testsuite><testcase name="booking"><failure message="oversold"/></testcase></testsuite>']:
            with self.subTest(content=content):
                self.assertNotEqual(self.run_report(content).returncode,0)

if __name__=='__main__': unittest.main()
