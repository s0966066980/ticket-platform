"""Public startup/test command contracts; no application internals."""
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PostgresCommandTests(unittest.TestCase):
    def test_check_finds_only_postgresql_16(self):
        result = subprocess.run(["bash", str(ROOT / "scripts/pg16.sh"), "check"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"PostgreSQL\) 16\.")

    def test_explicit_invalid_binary_directory_is_not_silently_replaced(self):
        env = dict(os.environ, PG_BIN="/nonexistent/ticket-postgres")
        result = subprocess.run(["bash", str(ROOT / "scripts/pg16.sh"), "check"], env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PostgreSQL 16 binaries", result.stderr)


class TestRunnerCommandTests(unittest.TestCase):
    def test_help_does_not_require_services_or_dependencies(self):
        result = subprocess.run(["bash", str(ROOT / "scripts/run_tests.sh"), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--backend-only", result.stdout)
        self.assertIn("--frontend-only", result.stdout)

    def test_database_prepare_help_is_available_without_database_access(self):
        import sys
        result = subprocess.run([sys.executable, str(ROOT / "scripts/prepare_database.py"), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("migration", result.stdout)


if __name__ == "__main__":
    unittest.main()
