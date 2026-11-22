"""Pixel-level eligibility: masks, saturation, shifts and denial interaction."""
import json
import unittest
from pathlib import Path

from astro_stack.archive import apply_batch, initialize
from astro_stack.release import export, issue, verify

from release_helpers import TempPaths


class EligibilityTests(unittest.TestCase):

    def setUp(self):
        self.paths = TempPaths()
        self.addCleanup(self.paths.close)
        initialize(str(self.paths.archive), {
            "dataset_id": "m", "width": 2, "height": 1,
            "apertures": [], "products": None})
        apply_batch(str(self.paths.archive), {
            "batch_id": "b", "expected_revision": 0,
            "add_darks": [{"id": "d", "dark_exposure_s": 1,
                           "dark": [[0, 0]], "dark_variance": [[1, 1]]}],
            "add_flats": [{"id": "f", "flat": [[1, 1]],
                           "bad_mask": [[True, False]]}],
            "add_releases": [{"id": "r", "version": 1, "start_s": 0,
                              "end_s": 100, "dark_id": "d", "flat_id": "f"}],
            "retract_releases": [],
            "put_frames": [
                {"id": "F", "exposure_s": 1, "dx": 0, "dy": 0,
                 "saturation": 100, "pixels": [[10, 20]], "observed_at_s": 5,
                 "variance": [[1, 1]], "expected_version": 0},
                {"id": "G", "exposure_s": 1, "dx": 0, "dy": 0,
                 "saturation": 30, "pixels": [[30, 40]], "observed_at_s": 5,
                 "variance": [[1, 1]], "expected_version": 0}],
            "withdraw_frames": []})

    def test_denied_invalid_samples_do_not_block_and_leave_no_evidence(self):
        license_value = {"dataset_id": "m", "denied": [
            {"frame_id": "F", "frame_version": 1, "x": 0, "y": 0},
            {"frame_id": "G", "frame_version": 1, "x": 0, "y": 0}]}
        request = {"request_id": "m1", "dataset_id": "m", "revision": 1,
                   "region": {"x0": 0, "y0": 0, "x1": 2, "y1": 1}}
        receipt = issue(str(self.paths.library), str(self.paths.archive),
                        request, license_value)
        output = self.paths.root / "package.json"
        export(str(self.paths.library), "m1", output)
        document = json.loads(output.read_text())
        self.assertEqual(document["cells"][0]["status"], "uncovered")
        self.assertEqual(document["cells"][1]["status"], "published")
        positions = {(sample["x"], sample["y"])
                     for frame in document["frames"]
                     for sample in frame["samples"]}
        self.assertEqual(positions, {(1, 0)})
        result = verify(str(output), receipt["sha256"])
        self.assertEqual(result["counts"]["uncovered"], 1)

    def test_saturated_sample_is_not_published_even_without_denial(self):
        request = {"request_id": "m2", "dataset_id": "m", "revision": 1,
                   "region": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}}
        receipt = issue(str(self.paths.root / "lib2"),
                        str(self.paths.archive), request,
                        {"dataset_id": "m", "denied": []})
        output = self.paths.root / "package2.json"
        export(str(self.paths.root / "lib2"), "m2", output)
        document = json.loads(output.read_text())
        self.assertEqual(document["cells"][0]["status"], "uncovered")
        self.assertEqual(document["contributions"], [])
        verify(str(output), receipt["sha256"])


if __name__ == "__main__":
    unittest.main()
