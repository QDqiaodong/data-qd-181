"""Loss-of-contact retrieval, crash atomicity, concurrency and export safety."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from astro_stack.archive import apply_batch
from astro_stack.release import export, issue, verify
from astro_stack.release.package import sha256_hex
from astro_stack.release.store import Library, ReleaseError

from release_helpers import (TempPaths, build_two_by_one_archive,
                             full_request, license_with, replace_g_batch)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


class LossOfContactTests(unittest.TestCase):

    def setUp(self):
        self.paths = TempPaths()
        self.addCleanup(self.paths.close)
        build_two_by_one_archive(self.paths.archive)
        self.request = full_request("req-persist")
        self.license = license_with([])
        self.receipt = issue(str(self.paths.library),
                             str(self.paths.archive), self.request, self.license)

    def test_retry_ignores_changed_license_and_missing_sources(self):
        # swap in a restrictive license under a new request to prove the new
        # package really changes
        restrictive = license_with([("G", 1, 0, 0)])
        new_receipt = issue(
            str(self.paths.root / "lib-restrictive"),
            str(self.paths.archive),
            full_request("req-restricted"), restrictive)
        new_output = self.paths.root / "restricted.json"
        export(str(self.paths.root / "lib-restrictive"), "req-restricted",
               new_output)
        restricted_doc = json.loads(new_output.read_text())
        self.assertEqual(
            [cell["status"] for cell in restricted_doc["cells"]],
            ["published", "withheld"])
        self.assertNotEqual(new_receipt["sha256"], self.receipt["sha256"])
        # now remove the source archive and license files entirely
        hidden_archive = self.paths.root / "archive-hidden"
        os.rename(self.paths.archive, hidden_archive)
        hidden_license = self.paths.root / "license-hidden.json"
        license_path = self.paths.root / "license.json"
        license_path.write_text(json.dumps(self.license))
        os.rename(license_path, hidden_license)
        # same canonical request (different formatting) returns the old digest
        retry_path = self.paths.root / "request-pretty.json"
        retry_path.write_text(json.dumps(self.request, indent=4))
        retry_receipt = issue(str(self.paths.library),
                              str(self.paths.root / "gone"),
                              retry_path, str(self.paths.root / "gone-lic"))
        self.assertEqual(retry_receipt, self.receipt)
        output = self.paths.root / "retrieved.json"
        export_receipt = export(str(self.paths.library), "req-persist", output)
        self.assertEqual(export_receipt, self.receipt)
        # verify recomputes both cells fully offline
        result = verify(str(output), self.receipt["sha256"])
        self.assertEqual(result["counts"],
                         {"published": 2, "withheld": 0, "uncovered": 0})
        self.assertEqual(result["rate_sum"], "35")
        self.assertEqual(result["variance"], "23/2")
        os.rename(hidden_archive, self.paths.archive)

    def test_same_id_different_request_conflicts_without_touching_archive(self):
        hidden = self.paths.root / "archive-hidden"
        os.rename(self.paths.archive, hidden)
        conflict = dict(self.request,
                        region={"x0": 1, "y0": 0, "x1": 2, "y1": 1})
        with self.assertRaises(ReleaseError):
            issue(str(self.paths.library),
                  str(self.paths.root / "gone"), conflict, self.license)
        os.rename(hidden, self.paths.archive)

    def test_failed_issue_does_not_consume_the_request_id(self):
        bad_license = license_with([("G", 1, 99, 0)])
        with self.assertRaises(ValueError):
            issue(str(self.paths.library), str(self.paths.archive),
                  full_request("req-fresh"), bad_license)
        receipt = issue(str(self.paths.library), str(self.paths.archive),
                        full_request("req-fresh"), self.license)
        self.assertEqual(receipt["request_id"], "req-fresh")


class CrashAtomicityTests(unittest.TestCase):

    def setUp(self):
        self.paths = TempPaths()
        self.addCleanup(self.paths.close)
        build_two_by_one_archive(self.paths.archive)

    def test_crash_before_commit_leaves_no_readable_half_package(self):
        request = full_request("req-crash-before")

        def hook(stage, library, request_id):
            if stage == "before_state_replace":
                raise RuntimeError("simulated termination")

        with self.assertRaises(RuntimeError):
            issue(str(self.paths.library), str(self.paths.archive), request,
                  license_with([]), fault_hook=hook)
        with Library(str(self.paths.library)) as library:
            self.assertEqual(library.state["issued"], [])
            self.assertIsNone(library.lookup(request))
        # reopen cleans the staged, never-committed package
        self.assertEqual(list((self.paths.library / "packages").iterdir()), [])
        # the id remains available and issues normally under the current license
        receipt = issue(
            str(self.paths.library), str(self.paths.archive), request,
            license_with([("G", 1, 0, 0)]))
        output = self.paths.root / "after.json"
        export(str(self.paths.library), "req-crash-before", output)
        document = json.loads(output.read_text())
        self.assertEqual(
            [cell["status"] for cell in document["cells"]],
            ["published", "withheld"])
        self.assertEqual(receipt["sha256"], sha256_hex(output.read_bytes()))

    def test_crash_after_commit_leaves_one_complete_retrievable_issuance(self):
        request = full_request("req-crash-after")

        def hook(stage, library, request_id):
            if stage == "after_state_replace":
                raise RuntimeError("simulated termination")

        with self.assertRaises(RuntimeError):
            issue(str(self.paths.library), str(self.paths.archive), request,
                  license_with([]), fault_hook=hook)
        output = self.paths.root / "committed.json"
        receipt = export(str(self.paths.library), "req-crash-after", output)
        result = verify(str(output), receipt["sha256"])
        self.assertEqual(result["rate_sum"], "35")
        # exact retry returns the same receipt without source access
        retry = issue(str(self.paths.library),
                      str(self.paths.root / "missing-archive"),
                      request, license_with([]))
        self.assertEqual(retry, receipt)

    def test_reopen_reports_corrupt_ledger_and_missing_package(self):
        request = full_request("req-corrupt")
        issue(str(self.paths.library), str(self.paths.archive), request,
              license_with([]))
        (self.paths.library / "state.json").write_text("{not json")
        with self.assertRaises(ReleaseError):
            Library(str(self.paths.library))

        healthy = self.paths.root / "lib-healthy"
        receipt = issue(str(healthy), str(self.paths.archive),
                        full_request("req-missing-pkg"), license_with([]))
        package_files = list((healthy / "packages").glob("*.json"))
        self.assertEqual(len(package_files), 1)
        os.unlink(package_files[0])
        with self.assertRaises(ReleaseError):
            Library(str(healthy))
        with self.assertRaises(ReleaseError):
            export(str(healthy), "req-missing-pkg",
                   self.paths.root / "never.json")

    def test_tampered_committed_package_is_reported_not_rebuilt(self):
        request = full_request("req-tampered-store")
        issue(str(self.paths.library), str(self.paths.archive), request,
              license_with([]))
        package_file = next((self.paths.library / "packages").glob("*.json"))
        package_file.write_bytes(package_file.read_bytes() + b" ")
        with self.assertRaises(ReleaseError):
            Library(str(self.paths.library))


class ConcurrencyTests(unittest.TestCase):

    def setUp(self):
        self.paths = TempPaths()
        self.addCleanup(self.paths.close)
        build_two_by_one_archive(self.paths.archive)

    def _worker(self):
        return textwrap.dedent('''
import json, sys, time
from pathlib import Path
from astro_stack.release import issue
library, archive, request_path, license_path, marker = sys.argv[1:6]
request = json.loads(Path(request_path).read_text())
license_value = json.loads(Path(license_path).read_text())
for _ in range(250):
    if Path(marker).exists():
        break
    time.sleep(0.02)
try:
    receipt = issue(library, archive, request, license_value)
    print(json.dumps({"ok": True, "receipt": receipt}))
except Exception as exc:
    print(json.dumps({"ok": False, "error": str(exc)}))
''')

    def _run_pair(self, request_a, request_b):
        workspace = self.paths.root / "workers"
        workspace.mkdir()
        script = workspace / "worker.py"
        script.write_text(self._worker())
        path_a = workspace / "a.json"
        path_b = workspace / "b.json"
        path_a.write_text(json.dumps(request_a))
        path_b.write_text(json.dumps(request_b))
        license_path = workspace / "license.json"
        license_path.write_text(json.dumps(license_with([])))
        marker = workspace / "go"
        env = dict(os.environ, PYTHONPATH=str(SRC))
        procs = [subprocess.Popen(
            [sys.executable, str(script), str(self.paths.library),
             str(self.paths.archive), str(path), str(license_path),
             str(marker)],
            stdout=subprocess.PIPE, text=True, env=env)
            for path in (path_a, path_b)]
        marker.write_text("go")
        return [json.loads(proc.communicate(timeout=30)[0]) for proc in procs]

    def test_different_requests_same_id_at_most_one_wins(self):
        results = self._run_pair(
            full_request("race-id"),
            full_request("race-id",
                         region={"x0": 1, "y0": 0, "x1": 2, "y1": 1}))
        self.assertEqual(sum(1 for result in results if result["ok"]), 1)
        self.assertTrue(any("already bound" in result.get("error", "")
                            for result in results))
        with Library(str(self.paths.library)) as library:
            self.assertEqual(len(library.state["issued"]), 1)

    def test_same_canonical_request_both_succeed_with_one_issuance(self):
        request = full_request("race-same")
        results = self._run_pair(request, dict(request))
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(results[0]["receipt"], results[1]["receipt"])
        with Library(str(self.paths.library)) as library:
            self.assertEqual(len(library.state["issued"]), 1)


class LibraryInitializationTests(unittest.TestCase):

    def setUp(self):
        self.paths = TempPaths()
        self.addCleanup(self.paths.close)

    def test_manual_initialize_is_create_only(self):
        from astro_stack.release.store import initialize
        build_two_by_one_archive(self.paths.archive)
        initialize(str(self.paths.library))
        with self.assertRaises(FileExistsError):
            initialize(str(self.paths.library))
        receipt = issue(str(self.paths.library), str(self.paths.archive),
                        full_request("auto"), license_with([]))
        self.assertEqual(receipt["request_id"], "auto")

    def test_non_empty_directory_cannot_become_a_library(self):
        (self.paths.library).mkdir()
        (self.paths.library / "junk").write_text("x")
        with self.assertRaises(ReleaseError):
            Library(str(self.paths.library), create=True)

    def test_opening_a_file_path_is_rejected(self):
        path = self.paths.root / "a-file"
        path.write_text("not a directory")
        with self.assertRaises(ReleaseError):
            Library(str(path))


class ExportProtectionTests(unittest.TestCase):

    def setUp(self):
        self.paths = TempPaths()
        self.addCleanup(self.paths.close)
        build_two_by_one_archive(self.paths.archive)
        issue(str(self.paths.library), str(self.paths.archive),
              full_request("req-export"), license_with([]))

    def test_export_never_overwrites_an_existing_target(self):
        output = self.paths.root / "export.json"
        output.write_text("keeper")
        with self.assertRaises(FileExistsError):
            export(str(self.paths.library), "req-export", output)
        self.assertEqual(output.read_text(), "keeper")

    def test_export_unknown_request_fails_without_creating_output(self):
        output = self.paths.root / "missing.json"
        with self.assertRaises(ReleaseError):
            export(str(self.paths.library), "unknown", output)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
