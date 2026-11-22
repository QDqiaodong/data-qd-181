"""Exact science details: flat normalization source and dark error identity."""
import json
import unittest
from pathlib import Path

from astro_stack.archive import apply_batch, initialize
from astro_stack.release import export, issue, verify

from release_helpers import TempPaths, write_json


class FlatNormalizationTests(unittest.TestCase):

    def test_mean_flat_comes_from_full_archive_matrix(self):
        paths = TempPaths()
        self.addCleanup(paths.close)
        initialize(str(paths.archive), {
            "dataset_id": "flatds", "width": 2, "height": 1,
            "apertures": [], "products": None})
        apply_batch(str(paths.archive), {
            "batch_id": "b", "expected_revision": 0,
            "add_darks": [{"id": "d", "dark_exposure_s": 1,
                           "dark": [[0, 0]], "dark_variance": [[4, 4]]}],
            "add_flats": [{"id": "f", "flat": [[2, 4]],
                           "bad_mask": [[False, False]]}],
            "add_releases": [{"id": "r", "version": 1, "start_s": 0,
                              "end_s": 100, "dark_id": "d", "flat_id": "f"}],
            "retract_releases": [],
            "put_frames": [{"id": "F", "exposure_s": 1, "dx": 0, "dy": 0,
                            "saturation": 1000, "pixels": [[10, 20]],
                            "observed_at_s": 5, "variance": [[1, 1]],
                            "expected_version": 0}],
            "withdraw_frames": []})
        # request only pixel 1: recomputing mean_flat from region flats would
        # give 4 instead of the archive-wide mean 3
        request = {"request_id": "flat-1", "dataset_id": "flatds",
                   "revision": 1,
                   "region": {"x0": 1, "y0": 0, "x1": 2, "y1": 1}}
        receipt = issue(str(paths.library), str(paths.archive), request,
                        {"dataset_id": "flatds", "denied": []})
        output = paths.root / "package.json"
        export(str(paths.library), "flat-1", output)
        document = json.loads(output.read_text())
        self.assertEqual(document["flats"][0]["mean_flat"], "3")
        cell = document["cells"][0]
        self.assertEqual(cell["rate"], "15")            # 20 * 3/4
        self.assertEqual(cell["variance"], "45/16")     # 9/16 + 4*9/16
        result = verify(str(output), receipt["sha256"])
        self.assertEqual(result["variance"], "45/16")


class SharedDarkCorrelationTests(unittest.TestCase):

    def setUp(self):
        self.paths = TempPaths()
        self.addCleanup(self.paths.close)
        initialize(str(self.paths.archive), {
            "dataset_id": "c", "width": 3, "height": 1,
            "apertures": [], "products": None})
        apply_batch(str(self.paths.archive), {
            "batch_id": "b", "expected_revision": 0,
            "add_darks": [
                {"id": "d", "dark_exposure_s": 1,
                 "dark": [[0, 0, 0]], "dark_variance": [[9, 9, 9]]},
                {"id": "d2", "dark_exposure_s": 1,
                 "dark": [[0, 0, 0]], "dark_variance": [[9, 9, 9]]}],
            "add_flats": [
                {"id": "f", "flat": [[1, 1, 1]],
                 "bad_mask": [[False, True, False]]},
                {"id": "f2", "flat": [[1, 1, 1]],
                 "bad_mask": [[False, False, False]]}],
            "add_releases": [
                {"id": "rF", "version": 1, "start_s": 0, "end_s": 6,
                 "dark_id": "d", "flat_id": "f"},
                {"id": "rG", "version": 2, "start_s": 6, "end_s": 12,
                 "dark_id": "d", "flat_id": "f2"}],
            "retract_releases": [],
            "put_frames": [
                {"id": "F", "exposure_s": 1, "dx": 0, "dy": 0,
                 "saturation": 1000, "pixels": [[10, 0, 0]],
                 "observed_at_s": 3, "variance": [[1, 0, 0]],
                 "expected_version": 0},
                {"id": "G", "exposure_s": 1, "dx": 1, "dy": 0,
                 "saturation": 1000, "pixels": [[20, 0, 0]],
                 "observed_at_s": 9, "variance": [[1, 0, 0]],
                 "expected_version": 0}],
            "withdraw_frames": []})
        self.request = {"request_id": "k", "dataset_id": "c", "revision": 1,
                        "region": {"x0": 0, "y0": 0, "x1": 2, "y1": 1}}

    def _issue(self, request, library):
        receipt = issue(str(library), str(self.paths.archive), request,
                        {"dataset_id": "c", "denied": []})
        output = self.paths.root / f"{request['request_id']}.json"
        export(str(library), request["request_id"], output)
        return receipt, output

    def test_region_variance_includes_shared_dark_correlation(self):
        receipt, output = self._issue(
            self.request, self.paths.root / "lib-shared")
        document = json.loads(output.read_text())
        # one dark source (d, x=0) maps into both cells via different frames
        self.assertEqual([cell["variance"] for cell in document["cells"]],
                         ["10", "10"])
        # 1+1 raw terms + 9*(1+1)^2 shared-source term = 38, not 20
        self.assertEqual(document["region"]["variance"], "38")
        result = verify(str(output), receipt["sha256"])
        self.assertEqual(result["variance"], "38")

    def test_equal_variance_distinct_dark_ids_do_not_merge(self):
        apply_batch(str(self.paths.archive), {
            "batch_id": "b2", "expected_revision": 1,
            "add_darks": [], "add_flats": [],
            "add_releases": [{"id": "rG2", "version": 3, "start_s": 6,
                              "end_s": 12, "dark_id": "d2", "flat_id": "f2"}],
            "retract_releases": [], "put_frames": [],
            "withdraw_frames": []})
        request = dict(self.request, request_id="k2", revision=2)
        receipt, output = self._issue(request, self.paths.root / "lib-distinct")
        document = json.loads(output.read_text())
        # independent dark sources: 1+1 + 9 + 9 = 20
        self.assertEqual(document["region"]["variance"], "20")
        result = verify(str(output), receipt["sha256"])
        self.assertEqual(result["variance"], "20")


if __name__ == "__main__":
    unittest.main()
