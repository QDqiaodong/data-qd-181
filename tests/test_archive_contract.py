"""Schema, boundary, report-shape and CLI contract tests."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from astro_stack.archive import apply_batch, initialize, render
from astro_stack.io import load

from archive_helpers import (batch, campaign_definition, classic_definition,
                             dark, flat, frame, release, write_json)
from archive_helpers import TempArchive

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.archive = TempArchive()
        self.addCleanup(self.archive.close)
        initialize(str(self.archive.path), classic_definition())

    def test_initialize_never_overwrites_and_leaves_no_partial_archive(self):
        path = self.archive.path
        with self.assertRaises(OSError):
            initialize(str(path), classic_definition())
        # existing history is intact
        self.assertEqual(render(str(path), 0)["schema_version"], 3)

    def test_unknown_and_missing_fields_rejected(self):
        bad = classic_definition()
        del bad["width"]
        with self.assertRaises(ValueError):
            with TempArchive() as other:
                initialize(str(other.path), bad)
        bad = classic_definition()
        bad["extra"] = 1
        with self.assertRaises(ValueError):
            with TempArchive() as other:
                initialize(str(other.path), bad)
        payload = batch("b1", 0, add_darks=[dark("d0")], add_flats=[flat()],
                        add_releases=[release("r1", 1)],
                        put_frames=[frame("fr", 0)])
        payload["put_frames"][0]["unexpected"] = 1
        with self.assertRaises(ValueError):
            apply_batch(str(self.archive.path), payload)

    def test_booleans_are_not_integers(self):
        payload = batch("b1", 0, put_frames=[frame("fr", 0)])
        payload["put_frames"][0]["exposure_s"] = True
        with self.assertRaises(ValueError):
            apply_batch(str(self.archive.path), payload)
        payload = batch("b1", 0, add_flats=[flat()])
        payload["add_flats"][0]["flat"][0][0] = True
        with self.assertRaises(ValueError):
            apply_batch(str(self.archive.path), payload)

    def test_id_rules_and_independent_namespaces(self):
        # frame id may equal a dark id: separate namespaces
        receipt = apply_batch(str(self.archive.path), batch(
            "b1", 0, add_darks=[dark("same")], add_flats=[flat()],
            add_releases=[release("r1", 1, dark_id="same")],
            put_frames=[frame("same", 0)]))
        self.assertEqual(receipt["revision"], 1)
        for payload in (
                batch("b", 0, add_darks=[dark("")], add_flats=[flat()]),
                batch("b", 0, add_darks=[dark("d0")],
                      add_flats=[flat()], add_releases=[release("r1", 0)]),
                batch("b", 0, add_darks=[dark("d0")], add_flats=[flat()],
                      add_releases=[release("r1", 1, start=5, end=5)]),
                batch("b", 0, add_flats=[
                    flat(values=[[0]], mask=[[True]])]),
        ):
            with self.assertRaises(ValueError):
                with TempArchive() as other:
                    initialize(str(other.path), classic_definition())
                    apply_batch(str(other.path), payload)

    def test_duplicate_ids_within_array_and_cross_array_frame_conflict(self):
        payload = batch("b1", 0,
                        add_darks=[dark("d0"), dark("d0")],
                        add_flats=[flat()])
        with self.assertRaises(ValueError):
            apply_batch(str(self.archive.path), payload)
        payload = batch("b1", 0, add_darks=[dark("d0")], add_flats=[flat()],
                        put_frames=[frame("fr", 0)],
                        withdraw_frames=[{"id": "fr", "expected_version": 0}])
        with self.assertRaises(ValueError):
            apply_batch(str(self.archive.path), payload)

    def test_references_versions_and_immutability(self):
        apply_batch(str(self.archive.path), batch(
            "b1", 0, add_darks=[dark("d0")], add_flats=[flat()],
            add_releases=[release("r1", 1)], put_frames=[frame("fr", 0)]))
        with self.assertRaises(ValueError):  # master id cannot be reused
            apply_batch(str(self.archive.path), batch("b2", 1,
                                                      add_darks=[dark("d0")]))
        with self.assertRaises(ValueError):  # version number is globally unique
            apply_batch(str(self.archive.path), batch(
                "b2", 1, add_releases=[release("r2", 1)]))
        with self.assertRaises(ValueError):  # cannot retract twice
            apply_batch(str(self.archive.path),
                        batch("b2", 1, retract_releases=["r1", "r1"]))
        apply_batch(str(self.archive.path),
                    batch("b2", 1, retract_releases=["r1"]))
        with self.assertRaises(ValueError):
            apply_batch(str(self.archive.path),
                        batch("b3", 2, retract_releases=["r1"]))
        with self.assertRaises(ValueError):  # add+retract same id in one batch
            apply_batch(str(self.archive.path), batch(
                "b3", 2, add_releases=[release("r2", 2)],
                retract_releases=["r2"]))

    def test_empty_batch_and_array_limits_rejected(self):
        with self.assertRaises(ValueError):
            apply_batch(str(self.archive.path), batch("b1", 0))
        too_many = batch("b1", 0, retract_releases=[f"r{i}" for i in range(51)])
        with self.assertRaises(ValueError):
            apply_batch(str(self.archive.path), too_many)

    def test_frame_id_quota_is_not_released_by_withdrawal(self):
        archive = TempArchive()
        self.addCleanup(archive.close)
        initialize(str(archive.path), campaign_definition())
        apply_batch(str(archive.path), batch(
            "b1", 0, add_darks=[dark("d0")], add_flats=[flat()],
            add_releases=[release("r1", 1)],
            put_frames=[frame(f"f{i}", 0, observed_at=i) for i in range(14)]))
        # withdraw one and try to introduce a 15th distinct id
        apply_batch(str(archive.path), batch(
            "b2", 1, withdraw_frames=[{"id": "f0", "expected_version": 1}]))
        with self.assertRaisesRegex(ValueError, "at most 14"):
            apply_batch(str(archive.path), batch(
                "b3", 2, put_frames=[frame("f14", 0)]))
        # reusing a known id to restore is still allowed
        apply_batch(str(archive.path), batch(
            "b3", 2, put_frames=[frame("f0", 2)]))

    def test_strict_json_file_loading(self):
        for text in ('{"x": 1, "x": 2}', '[1, NaN]', '{"x": Infinity}',
                     '["legacy", 1e999]'):
            with tempfile.TemporaryDirectory() as directory:
                path = write_json(directory, "bad.json", {})
                path.write_text(text)
                with self.assertRaises(ValueError):
                    load(path)
        big = bytearray(b'{"a":')
        big.extend(b"1" * 8_000_000)
        big.extend(b"}")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "big.json")
            path.write_bytes(bytes(big))
            with self.assertRaises(ValueError):
                load(path)


class ReportShapeTests(unittest.TestCase):
    def test_revision_zero_empty_archive_renders(self):
        archive = TempArchive()
        self.addCleanup(archive.close)
        initialize(str(archive.path), classic_definition())
        report = render(str(archive.path), 0)
        self.assertEqual(set(report), {"schema_version", "dataset_id",
                                       "revision", "frame_bindings", "report",
                                       "uncertainty", "summary"})
        self.assertEqual(report["schema_version"], 3)
        self.assertEqual(report["frame_bindings"], [])
        self.assertEqual(report["summary"],
                         {"frames_seen": 0, "ready_frames": 0,
                          "blocked_frames": 0, "withdrawn_frames": 0})
        classic = report["report"]
        self.assertEqual(classic["schema_version"], 1)
        self.assertEqual(classic["frame_audit"], [])
        self.assertEqual(classic["rate_image"], [[None]])
        self.assertEqual(classic["exposure_s"], [[0]])
        self.assertEqual(classic["contributor_count"], [[0]])
        self.assertEqual(classic["summary"]["frames"], 0)
        self.assertEqual(report["uncertainty"]["variance_image"], [[None]])
        self.assertEqual(report["uncertainty"]["aperture_order"], ["a"])
        self.assertEqual(report["uncertainty"]["aperture_covariance"],
                         [[None]])

    def test_empty_campaign_delivery_excludes_blocked_from_unused(self):
        archive = TempArchive()
        self.addCleanup(archive.close)
        initialize(str(archive.path), campaign_definition())
        apply_batch(str(archive.path), batch(
            "b1", 0, add_darks=[dark("d0")], add_flats=[flat()],
            put_frames=[frame("fr", 0)]))  # no release covers it
        report = render(str(archive.path), 1)
        self.assertEqual(report["frame_bindings"][0]["status"], "no_release")
        self.assertIsNone(report["uncertainty"])
        self.assertEqual(report["report"]["products"], [])
        self.assertEqual(report["report"]["unused_frame_ids"], [])
        self.assertEqual(report["report"]["unfulfilled_product_ids"], ["p"])
        self.assertEqual(report["summary"]["blocked_frames"], 1)

    def test_incomplete_aperture_nulls_full_covariance_row_and_column(self):
        definition = classic_definition(apertures=[
            {"id": "edge", "x": 0, "y": 0, "radius": 1},
            {"id": "live", "x": 0, "y": 0, "radius": 0}])
        archive = TempArchive()
        self.addCleanup(archive.close)
        initialize(str(archive.path), definition)
        apply_batch(str(archive.path), batch(
            "b1", 0, add_darks=[dark("d0")], add_flats=[flat()],
            add_releases=[release("r1", 1)], put_frames=[frame("fr", 0)]))
        report = render(str(archive.path), 1)
        uncertainty = report["uncertainty"]
        self.assertEqual(uncertainty["aperture_order"], ["edge", "live"])
        self.assertEqual(uncertainty["aperture_covariance"],
                         [[None, None], [None, "1"]])
        # the incomplete aperture is still measured in the classic report
        aperture = next(a for a in report["report"]["apertures"]
                        if a["aperture_id"] == "edge")
        self.assertFalse(aperture["complete"])
        self.assertGreater(aperture["outside_pixels"], 0)

    def test_ready_but_unused_frame_is_still_in_frame_bindings(self):
        definition = campaign_definition(products=[{
            "id": "p", "start_s": 0, "end_s": 7, "priority": 1, "max_frames": 1,
            "requirements": [{"aperture_id": "a", "min_exposure_s": 1,
                              "max_variance": "100"}]}])
        archive = TempArchive()
        self.addCleanup(archive.close)
        initialize(str(archive.path), definition)
        apply_batch(str(archive.path), batch(
            "b1", 0, add_darks=[dark("d0")], add_flats=[flat()],
            add_releases=[release("r1", 1, start=0, end=100)],
            put_frames=[frame("in", 0, observed_at=5),
                        frame("late", 0, observed_at=50)]))
        report = render(str(archive.path), 1)
        bound_ids = {row["frame_id"] for row in report["frame_bindings"]}
        self.assertEqual(bound_ids, {"in", "late"})
        self.assertEqual(report["report"]["unused_frame_ids"], ["late"])
        for row in report["frame_bindings"]:
            if row["frame_id"] == "late":
                # ready via its release, but outside every product time window
                self.assertEqual(row["status"], "ready")
                self.assertEqual(row["release_id"], "r1")

    def test_blocked_ready_mix_keeps_blocked_out_of_unused_ids(self):
        products = [{"id": "p", "start_s": 0, "end_s": 3, "priority": 1,
                     "max_frames": 1,
                     "requirements": [{"aperture_id": "a", "min_exposure_s": 1,
                                       "max_variance": "2"}]}]
        archive = TempArchive()
        self.addCleanup(archive.close)
        initialize(str(archive.path), campaign_definition(products=products))
        apply_batch(str(archive.path), batch(
            "b1", 0, add_darks=[dark("d0")], add_flats=[flat()],
            add_releases=[release("r1", 1, start=0, end=100)],
            put_frames=[frame("covered", 0, observed_at=5),
                        frame("orphan", 0, observed_at=500)]))
        report = render(str(archive.path), 1)
        statuses = {row["frame_id"]: row["status"]
                    for row in report["frame_bindings"]}
        self.assertEqual(statuses["covered"], "ready")
        self.assertEqual(statuses["orphan"], "no_release")
        # the product window excludes both; only the ready frame is "unused"
        self.assertEqual(report["report"]["unused_frame_ids"], ["covered"])
        self.assertEqual(report["report"]["unfulfilled_product_ids"], ["p"])

    def test_revision_must_be_an_existing_integer(self):
        archive = TempArchive()
        self.addCleanup(archive.close)
        initialize(str(archive.path), classic_definition())
        for bad_revision in (-1, 1, 99):
            with self.assertRaises(ValueError):
                render(str(archive.path), bad_revision)


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "astro_stack.cli_archive", *args],
            cwd=ROOT, capture_output=True, text=True,
            env=dict(__import__("os").environ, PYTHONPATH=str(SRC)))

    def test_full_cli_lifecycle_and_report_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            archive = directory / "archive"
            definition_path = write_json(directory, "definition.json",
                                         classic_definition())
            batch_path = write_json(directory, "batch.json", batch(
                "b1", 0, add_darks=[dark("d0")], add_flats=[flat()],
                add_releases=[release("r1", 1)],
                put_frames=[frame("fr", 0)]))
            report_path = directory / "nested" / "report.json"
            init = self.run_cli("--archive", str(archive),
                                "--initialize", str(definition_path))
            self.assertEqual(init.returncode, 0, init.stderr)
            self.assertEqual(json.loads(init.stdout),
                             {"dataset_id": "ds", "revision": 0})
            applied = self.run_cli("--archive", str(archive),
                                   "--apply", str(batch_path))
            self.assertEqual(applied.returncode, 0, applied.stderr)
            rendered = self.run_cli("--archive", str(archive),
                                    "--revision", "1",
                                    "--output", str(report_path))
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            published = json.loads(report_path.read_text())
            self.assertEqual(json.loads(rendered.stdout), published["summary"])
            # create-only: existing report is never overwritten
            blocked = self.run_cli("--archive", str(archive),
                                   "--revision", "1",
                                   "--output", str(report_path))
            self.assertEqual(blocked.returncode, 2)
            before = report_path.read_text()
            # a failed render also leaves the archive and report untouched
            failed = self.run_cli("--archive", str(archive),
                                  "--revision", "9",
                                  "--output", str(directory / "other.json"))
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(report_path.read_text(), before)
            self.assertFalse((directory / "other.json").exists())

    def test_mutual_exclusion_and_bad_inputs_exit_two(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory, "archive")
            definition_path = write_json(directory, "definition.json",
                                         classic_definition())
            init = self.run_cli("--archive", str(archive),
                                "--initialize", str(definition_path))
            self.assertEqual(init.returncode, 0)
            no_output = self.run_cli("--archive", str(archive),
                                     "--revision", "0")
            self.assertEqual(no_output.returncode, 2)
            bad_path = write_json(directory, "bad.json", {"width": 1})
            bad_apply = self.run_cli("--archive", str(archive),
                                     "--apply", str(bad_path))
            self.assertEqual(bad_apply.returncode, 2)
            duplicate = self.run_cli("--archive", str(archive),
                                     "--initialize", str(definition_path))
            self.assertEqual(duplicate.returncode, 2)


class LegacyEntryCompatibilityTests(unittest.TestCase):
    def test_legacy_service_and_cli_contract_unchanged(self):
        from astro_stack.service import run
        legacy = json.loads((ROOT / "fixtures" / "input.json").read_text())
        result = run(copy.deepcopy(legacy))
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["rate_image"],
                         [["10", "40/3", "70/3"], ["40", "130/3", "60"]])
        campaign = json.loads((ROOT / "fixtures" / "campaign.json").read_text())
        campaign_result = run(campaign)
        self.assertEqual(campaign_result["schema_version"], 2)
        self.assertEqual(campaign_result["covariance"],
                         [["5/2", "2"], ["2", "3"]])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "legacy.json")
            proc = subprocess.run(
                [sys.executable, "-m", "astro_stack.cli",
                 "--source", str(ROOT / "fixtures" / "input.json"),
                 "--output", str(output)],
                cwd=ROOT, capture_output=True, text=True,
                env=dict(__import__("os").environ, PYTHONPATH=str(SRC)))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(output.read_text())["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
