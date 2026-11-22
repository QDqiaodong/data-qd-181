"""End-to-end astro-release CLI tests."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

from release_helpers import (TempPaths, build_two_by_one_archive,
                             full_request, license_with, write_json)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


class ReleaseCliTests(unittest.TestCase):

    def setUp(self):
        self.paths = TempPaths()
        self.addCleanup(self.paths.close)
        build_two_by_one_archive(self.paths.archive)
        self.request_path = write_json(
            self.paths.root, "request.json", full_request("cli-1"))
        self.license_path = write_json(
            self.paths.root, "license.json", license_with([]))

    def run_cli(self, *arguments):
        env = dict(__import__("os").environ, PYTHONPATH=str(SRC))
        return subprocess.run(
            [sys.executable, "-m", "astro_stack.cli_release", *arguments],
            capture_output=True, text=True, env=env)

    def test_issue_export_verify_round_trip(self):
        issued = self.run_cli(
            "issue", "--library", str(self.paths.library),
            "--archive", str(self.paths.archive),
            "--request", str(self.request_path),
            "--license", str(self.license_path))
        self.assertEqual(issued.returncode, 0, issued.stderr)
        receipt = json.loads(issued.stdout)
        self.assertEqual(set(receipt),
                         {"request_id", "dataset_id", "revision", "sha256"})
        output = self.paths.root / "package.json"
        exported = self.run_cli(
            "export", "--library", str(self.paths.library),
            "--request-id", "cli-1", "--output", str(output))
        self.assertEqual(exported.returncode, 0, exported.stderr)
        verified = self.run_cli(
            "verify", "--package", str(output),
            "--expected-sha256", receipt["sha256"])
        self.assertEqual(verified.returncode, 0, verified.stderr)
        result = json.loads(verified.stdout)
        self.assertTrue(result["digest_match"])
        self.assertEqual(result["rate_sum"], "35")
        self.assertEqual(result["variance"], "23/2")

    def test_failures_exit_nonzero_without_success_looking_output(self):
        bad_digest = self.run_cli(
            "verify", "--package", str(self.request_path),
            "--expected-sha256", "0" * 64)
        self.assertEqual(bad_digest.returncode, 2)
        output = self.paths.root / "exists.json"
        output.write_text("keeper")
        self.run_cli("issue", "--library", str(self.paths.library),
                     "--archive", str(self.paths.archive),
                     "--request", str(self.request_path),
                     "--license", str(self.license_path))
        protected = self.run_cli(
            "export", "--library", str(self.paths.library),
            "--request-id", "cli-1", "--output", str(output))
        self.assertEqual(protected.returncode, 2)
        self.assertEqual(output.read_text(), "keeper")
        missing = self.run_cli(
            "export", "--library", str(self.paths.library),
            "--request-id", "never-issued",
            "--output", str(self.paths.root / "never.json"))
        self.assertEqual(missing.returncode, 2)
        self.assertFalse((self.paths.root / "never.json").exists())


if __name__ == "__main__":
    unittest.main()
