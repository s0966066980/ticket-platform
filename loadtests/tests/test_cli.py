import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]

class FixtureCliTests(unittest.TestCase):
    def test_seat_files_preserve_unique_coordinates_and_matching_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT/'scripts/generate_load_data.py'), 'seats', '--rows','120','--output',directory], capture_output=True, text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            with (Path(directory)/'seats.csv').open() as source:
                rows=list(csv.DictReader(source))
            self.assertEqual(len(rows),120)
            self.assertEqual(rows[0],{'zone_code':'A','row_label':'R00001','seat_number':'1'})
            self.assertEqual(rows,json.loads((Path(directory)/'seats.json').read_text()))
            self.assertEqual(len({tuple(row.values()) for row in rows}),120)

    def test_consistency_help_requires_no_database_or_driver(self):
        result=subprocess.run([sys.executable,str(ROOT/'scripts/check_consistency.py'),'--help'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('--event-id',result.stdout)

if __name__ == '__main__': unittest.main()
