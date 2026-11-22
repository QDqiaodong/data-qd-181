"""Acceptance flow: the continuous 2x1 classic-archive release scenario."""
import json
import unittest
from fractions import Fraction
from pathlib import Path

from astro_stack.archive import apply_batch
from astro_stack.release import export, issue, verify
from astro_stack.release.package import PackageError, canonical_bytes, sha256_hex

from release_helpers import (TempPaths, build_two_by_one_archive,
                             full_request, license_with,
                             replace_g_batch, two_by_one_definition)


def cells_by_status(document):
    return {cell["status"]: cell for cell in document["cells"]}


class ContinuousReleaseFlowTests(unittest.TestCase):

    def setUp(self):
        self.paths = TempPaths()
        self.addCleanup(self.paths.close)
        build_two_by_one_archive(self.paths.archive)

    def _issue_export_verify(self, request_id, license_value, revision=1,
                             library=None):
        request = full_request(request_id, revision=revision)
        library = library or self.paths.library
        receipt = issue(str(library), str(self.paths.archive), request,
                        license_value)
        output = self.paths.root / f"{request_id}.json"
        export_receipt = export(str(library), request_id, output)
        self.assertEqual(receipt, export_receipt)
        document = json.loads(output.read_text())
        self.assertEqual(sha256_hex(canonical_bytes(document)),
                         receipt["sha256"])
        result = verify(str(output), receipt["sha256"])
        return receipt, output, document, result

    def test_unrestricted_delivery_matches_the_spec_numbers(self):
        receipt, output, document, result = self._issue_export_verify(
            "req-open", license_with([]))
        self.assertEqual(set(document), {
            "format", "request", "frame", "cells", "region", "frames",
            "releases", "darks", "flats", "contributions"})
        self.assertEqual(document["frame"], {"width": 2, "height": 1})
        self.assertEqual([(c["x"], c["status"]) for c in document["cells"]],
                         [(0, "published"), (1, "published")])
        first, second = document["cells"]
        self.assertEqual((first["rate"], first["variance"],
                          first["exposure_s"], first["contributor_count"]),
                         ("10", "5", 1, 1))
        self.assertEqual((second["rate"], second["variance"],
                          second["exposure_s"], second["contributor_count"]),
                         ("25", "5/2", 2, 2))
        self.assertEqual(document["region"], {
            "rate_sum": "35", "variance": "23/2",
            "counts": {"published": 2, "withheld": 0, "uncovered": 0}})
        self.assertEqual(result["rate_sum"], "35")
        self.assertEqual(result["variance"], "23/2")
        self.assertEqual(receipt, {"request_id": "req-open",
                                   "dataset_id": "ds-2x1", "revision": 1,
                                   "sha256": receipt["sha256"]})
        # evidence identifies frame version, detector coordinates and bindings
        frame_ids = {frame["frame_id"]: frame for frame in document["frames"]}
        self.assertEqual(frame_ids["F"]["frame_version"], 1)
        self.assertEqual(frame_ids["G"]["frame_version"], 1)
        self.assertEqual(frame_ids["G"]["dx"], 1)
        release = document["releases"][0]
        self.assertEqual((release["dark_id"], release["flat_id"]), ("d", "f"))
        dark = document["darks"][0]
        self.assertEqual(dark["dark_exposure_s"], 1)
        self.assertEqual({(s["x"], s["y"], s["dark"], s["dark_variance"])
                          for s in dark["samples"]},
                         {(0, 0, 0, 4), (1, 0, 0, 4)})
        flat = document["flats"][0]
        self.assertEqual(flat["mean_flat"], "1")
        self.assertEqual({s["flat"] for s in flat["samples"]}, {1})
        # no source paths or wall-clock fields leak into the package
        text = output.read_text()
        self.assertNotIn(str(self.paths.archive), text)
        self.assertNotIn("time", text.lower().replace("observed_at_s", ""))

    def test_denying_g_origin_withholds_only_the_second_cell(self):
        denied = [("G", 1, 0, 0)]
        receipt, output, document, result = self._issue_export_verify(
            "req-deny-g0", license_with(denied))
        first, second = document["cells"]
        self.assertEqual(first["status"], "published")
        self.assertEqual(first["rate"], "10")
        self.assertEqual(first["variance"], "5")
        self.assertEqual(second, {"x": 1, "y": 0, "status": "withheld"})
        self.assertEqual(document["region"], {
            "rate_sum": "10", "variance": "5",
            "counts": {"published": 1, "withheld": 1, "uncovered": 0}})
        self.assertEqual(result["rate_sum"], "10")
        self.assertEqual(result["variance"], "5")
        # neither G contribution survives: only F remains in every table
        self.assertEqual([frame["frame_id"] for frame in document["frames"]],
                         ["F"])
        frame_positions = {(s["x"], s["y"])
                           for frame in document["frames"]
                           for s in frame["samples"]}
        self.assertEqual(frame_positions, {(0, 0)})
        for entry in document["contributions"]:
            self.assertEqual(document["frames"][entry["frame"]]["frame_id"],
                             "F")
        # the withheld cell names no blocking identity and no pixel evidence
        self.assertEqual(set(second), {"x", "y", "status"})
        self.assertEqual(document["darks"][0]["samples"],
                         [{"dark": 0, "dark_variance": 4, "x": 0, "y": 0}])
        # recomputing the published subset would still yield 10/5, but that
        # subset is never presented as the original revision's second cell
        self.assertEqual(len(document["cells"]), 2)

    def test_denied_sample_shifted_outside_blocks_nothing(self):
        denied = [("G", 1, 1, 0)]
        receipt, output, document, result = self._issue_export_verify(
            "req-outside", license_with(denied),
            library=self.paths.root / "lib-outside")
        self.assertEqual([c["status"] for c in document["cells"]],
                         ["published", "published"])
        self.assertEqual(document["region"]["rate_sum"], "35")
        self.assertEqual(result["variance"], "23/2")

    def test_later_frame_version_restores_publication_under_old_denial(self):
        apply_batch(str(self.paths.archive), replace_g_batch(1))
        denied = [("G", 1, 0, 0)]
        receipt, output, document, result = self._issue_export_verify(
            "req-rev2", license_with(denied), revision=2,
            library=self.paths.root / "lib-rev2")
        self.assertEqual([c["status"] for c in document["cells"]],
                         ["published", "published"])
        g_record = next(frame for frame in document["frames"]
                        if frame["frame_id"] == "G")
        self.assertEqual(g_record["frame_version"], 2)
        self.assertEqual(document["region"]["rate_sum"], "35")
        self.assertEqual(document["region"]["variance"], "23/2")

    def test_identical_facts_produce_identical_package_bytes(self):
        request = full_request("req-det")
        first_library = self.paths.root / "lib-a"
        second_library = self.paths.root / "lib-b"
        receipt_a = issue(str(first_library), str(self.paths.archive), request,
                          license_with([]))
        receipt_b = issue(str(second_library), str(self.paths.archive),
                          dict(request), license_with([]))
        out_a = self.paths.root / "a.json"
        out_b = self.paths.root / "b.json"
        export(str(first_library), "req-det", out_a)
        export(str(second_library), "req-det", out_b)
        self.assertEqual(out_a.read_bytes(), out_b.read_bytes())
        self.assertEqual(receipt_a["sha256"], receipt_b["sha256"])

    def test_tampered_digest_and_internally_inconsistent_packages_rejected(self):
        receipt, output, document, _ = self._issue_export_verify(
            "req-tamper", license_with([("G", 1, 0, 0)]),
            library=self.paths.root / "lib-tamper")
        # untrusted digest never substitutes for the separately held receipt
        with self.assertRaises(PackageError):
            verify(str(output), "0" * 64)
        # digest-valid package carrying a wrong region variance
        evil = json.loads(output.read_text())
        evil["region"]["variance"] = "999/7"
        evil_path = self.paths.root / "evil-variance.json"
        evil_path.write_bytes(canonical_bytes(evil))
        with self.assertRaisesRegex(PackageError, "region variance mismatch"):
            verify(str(evil_path), sha256_hex(evil_path.read_bytes()))
        # digest-valid package smuggling evidence into the withheld cell
        evil2 = json.loads(output.read_text())
        withheld = next(cell for cell in evil2["cells"]
                        if cell["status"] == "withheld")
        withheld.update({"rate": "25", "variance": "5/2", "exposure_s": 2,
                         "contributor_count": 0, "contributions": []})
        evil2_path = self.paths.root / "evil-withheld.json"
        evil2_path.write_bytes(canonical_bytes(evil2))
        with self.assertRaises(PackageError):
            verify(str(evil2_path), sha256_hex(evil2_path.read_bytes()))
        # digest-valid package with an unbound redundant contribution
        evil3 = json.loads(output.read_text())
        evil3["contributions"].append(dict(evil3["contributions"][0]))
        evil3_path = self.paths.root / "evil-redundant.json"
        evil3_path.write_bytes(canonical_bytes(evil3))
        with self.assertRaises(PackageError):
            verify(str(evil3_path), sha256_hex(evil3_path.read_bytes()))

    def test_uncovered_region_is_a_successful_empty_package(self):
        from astro_stack.archive import initialize
        archive = self.paths.root / "shifted"
        initialize(str(archive), two_by_one_definition("ds-shift"))
        apply_batch(str(archive), {
            "batch_id": "b", "expected_revision": 0,
            "add_darks": [{"id": "d", "dark_exposure_s": 1,
                           "dark": [[0, 0]], "dark_variance": [[4, 4]]}],
            "add_flats": [{"id": "f", "flat": [[1, 1]],
                           "bad_mask": [[False, False]]}],
            "add_releases": [{"id": "r", "version": 1, "start_s": 0,
                              "end_s": 100, "dark_id": "d", "flat_id": "f"}],
            "retract_releases": [],
            "put_frames": [{"id": "F", "exposure_s": 1, "dx": 1, "dy": 0,
                            "saturation": 1000, "pixels": [[10, 20]],
                            "observed_at_s": 5, "variance": [[1, 1]],
                            "expected_version": 0}],
            "withdraw_frames": []})
        request = full_request("req-uncovered", dataset_id="ds-shift",
                               region={"x0": 0, "y0": 0, "x1": 1, "y1": 1})
        receipt = issue(str(self.paths.root / "lib-uncovered"), str(archive),
                        request, license_with([], "ds-shift"))
        output = self.paths.root / "uncovered.json"
        export(str(self.paths.root / "lib-uncovered"), "req-uncovered", output)
        document = json.loads(output.read_text())
        self.assertEqual(document["cells"],
                         [{"x": 0, "y": 0, "status": "uncovered"}])
        self.assertEqual(document["region"], {
            "rate_sum": None, "variance": None,
            "counts": {"published": 0, "withheld": 0, "uncovered": 1}})
        for table in ("frames", "releases", "darks", "flats", "contributions"):
            self.assertEqual(document[table], [])
        result = verify(str(output), receipt["sha256"])
        self.assertIsNone(result["rate_sum"])
        self.assertIsNone(result["variance"])

    def test_rational_outputs_are_reduced_fraction_strings(self):
        receipt, output, packaged, result = self._issue_export_verify(
            "req-frac", license_with([]),
            library=self.paths.root / "lib-frac")
        for cell in packaged["cells"]:
            self.assertEqual(str(Fraction(cell["rate"])), cell["rate"])
            self.assertEqual(str(Fraction(cell["variance"])),
                             cell["variance"])
        self.assertEqual(str(Fraction(packaged["region"]["variance"])),
                         packaged["region"]["variance"])


if __name__ == "__main__":
    unittest.main()
