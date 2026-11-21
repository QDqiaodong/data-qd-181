"""Acceptance tests: calibration corrections must change the science, not just labels."""
import unittest
from fractions import Fraction

from astro_stack.archive import apply_batch, initialize, render

from archive_helpers import (bootstrap, batch, campaign_definition,
                             classic_definition, dark, flat, frame, release)
from archive_helpers import TempArchive


class RecalibrationChangesScienceTests(unittest.TestCase):
    """Acceptance behavior 1: corrected dark changes signal, variance and delivery."""

    def test_higher_version_release_recomputes_signal_and_variance(self):
        archive = TempArchive()
        self.addCleanup(archive.close)
        bootstrap(archive.path, classic_definition(), [
            batch("b1", 0,
                  add_darks=[dark("d0", 0, 1), dark("d1", 2, 4)],
                  add_flats=[flat()],
                  add_releases=[release("r1", 1, dark_id="d0")],
                  put_frames=[frame("fr", 0)]),
            batch("b2", 1, add_releases=[release("r2", 2, dark_id="d1")]),
        ])
        first = render(str(archive.path), 1)
        second = render(str(archive.path), 2)
        reopened_first = render(str(archive.path), 1)
        self.assertEqual(first["report"]["rate_image"], [["10"]])
        self.assertEqual(first["uncertainty"]["variance_image"], [["1"]])
        self.assertEqual(second["report"]["rate_image"], [["8"]])
        self.assertEqual(second["uncertainty"]["variance_image"], [["4"]])
        # the historical revision stays reproducible after the correction
        self.assertEqual(reopened_first, first)
        self.assertEqual(first["revision"], 1)
        self.assertEqual(second["revision"], 2)

    def test_variance_gate_blocks_previously_good_campaign_product(self):
        archive = TempArchive()
        self.addCleanup(archive.close)
        bootstrap(archive.path, campaign_definition(), [
            batch("b1", 0,
                  add_darks=[dark("d0", 0, 1), dark("d1", 2, 4)],
                  add_flats=[flat()],
                  add_releases=[release("r1", 1, dark_id="d0")],
                  put_frames=[frame("fr", 0)]),
        ])
        first = render(str(archive.path), 1)
        self.assertEqual(first["report"]["summary"]["delivered_products"], 1)
        apply_batch(str(archive.path),
                    batch("b2", 1, add_releases=[release("r2", 2, dark_id="d1")]))
        second = render(str(archive.path), 2)
        # dark variance 4 exceeds the product requirement max_variance 2
        self.assertEqual(second["report"]["products"], [])
        self.assertEqual(second["report"]["unfulfilled_product_ids"], ["p"])
        self.assertEqual(second["report"]["unused_frame_ids"], ["fr"])
        # revision 1 still shows the old, once-valid delivery
        self.assertEqual(render(str(archive.path), 1)["report"]["summary"]
                         ["delivered_products"], 1)


class SharedDarkIdentityTests(unittest.TestCase):
    """Acceptance behavior 2: dark random-source identity is (dark_id, x, y)."""

    def _two_product_definition(self):
        return {
            "dataset_id": "ds", "width": 2, "height": 1,
            "apertures": [{"id": "a0", "x": 0, "y": 0, "radius": 0},
                          {"id": "a1", "x": 1, "y": 0, "radius": 0}],
            "products": [
                {"id": "p0", "start_s": 0, "end_s": 5, "priority": 1,
                 "max_frames": 1,
                 "requirements": [{"aperture_id": "a0", "min_exposure_s": 1,
                                   "max_variance": "100"}]},
                {"id": "p1", "start_s": 5, "end_s": 15, "priority": 1,
                 "max_frames": 1,
                 "requirements": [{"aperture_id": "a1", "min_exposure_s": 1,
                                   "max_variance": "100"}]}]}

    def test_same_dark_id_correlates_products_even_via_distinct_releases(self):
        archive = TempArchive()
        self.addCleanup(archive.close)
        definition = self._two_product_definition()
        bootstrap(archive.path, definition, [
            batch("b1", 0,
                  add_darks=[dark("d0", 0, 1, width=2),
                             dark("d1", 0, 1, width=2)],
                  add_flats=[flat(values=[[1, 1]])],
                  add_releases=[
                      release("r0", 1, dark_id="d0", start=0, end=5),
                      release("r1", 2, dark_id="d0", start=5, end=15)],
                  put_frames=[
                      frame("f0", 0, observed_at=0, pixels=[[0, 0]],
                            frame_variance=[[0, 0]]),
                      frame("f1", 0, observed_at=10, dx=1,
                            pixels=[[0, 0]], frame_variance=[[0, 0]])]),
        ])
        report = render(str(archive.path), 1)["report"]
        self.assertEqual(len(report["products"]), 2)
        self.assertEqual(report["covariance"], [["1", "1"], ["1", "1"]])
        apply_batch(str(archive.path), batch(
            "b2", 1,
            add_releases=[release("r2", 3, dark_id="d1", start=5, end=15)]))
        changed = render(str(archive.path), 2)["report"]
        # identical numbers, distinct dark id: variances stay 1, covariance 0
        self.assertEqual(changed["covariance"], [["1", "0"], ["0", "1"]])
        # revision 1 keeps the shared correlation
        self.assertEqual(render(str(archive.path), 1)["report"]["covariance"],
                         [["1", "1"], ["1", "1"]])

    def test_shift_and_flat_normalization_keep_detector_identity(self):
        archive = TempArchive()
        self.addCleanup(archive.close)
        bootstrap(archive.path, self._two_product_definition(), [
            batch("b1", 0,
                  add_darks=[{
                      "id": "d0", "dark_exposure_s": 1,
                      "dark": [[0, 0]], "dark_variance": [[9, 1]]}],
                  add_flats=[
                      flat("f0", values=[[2, 4]]),
                      flat("f1", values=[[1, 1]])],
                  add_releases=[
                      release("r0", 1, dark_id="d0", flat_id="f0",
                              start=0, end=5),
                      release("r1", 2, dark_id="d0", flat_id="f1",
                              start=5, end=15)],
                  put_frames=[
                      frame("f0", 0, observed_at=0, pixels=[[0, 0]],
                            frame_variance=[[0, 0]]),
                      frame("f1", 0, observed_at=10, dx=1, pixels=[[0, 0]],
                            frame_variance=[[0, 0]])]),
        ])
        report = render(str(archive.path), 1)["report"]
        # both products read detector pixel (d0, x=0): g0 = mean3/flat2 = 3/2,
        # g1 = mean1/flat1 = 1; cov = 9 * 3/2 * 1 = 27/2
        self.assertEqual(report["covariance"],
                         [["81/4", "27/2"], ["27/2", "9"]])
        p0 = next(p for p in report["products"] if p["product_id"] == "p0")
        p1 = next(p for p in report["products"] if p["product_id"] == "p1")
        # f0 (no shift) covers both output pixels; f1 (shift +1) maps its
        # detector x=0 sample onto output coordinate 1
        self.assertEqual(p0["stack"]["rate_image"], [["0", "0"]])
        self.assertEqual(p1["stack"]["rate_image"], [[None, "0"]])


class ReleaseLifecycleTests(unittest.TestCase):
    """Acceptance behavior 3: retraction, partial windows and frame corrections."""

    def _bootstrap(self):
        archive = TempArchive()
        self.addCleanup(archive.close)
        bootstrap(archive.path, classic_definition(), [
            batch("b1", 0, add_darks=[dark("d0")], add_flats=[flat()],
                  add_releases=[release("r1", 1, start=0, end=100)],
                  put_frames=[frame("fr", 0)]),
            batch("b2", 1, add_releases=[release("r2", 2, start=0, end=100)]),
            batch("b3", 2, retract_releases=["r2"]),
        ])
        return archive

    def test_retracted_highest_version_blocks_without_falling_back(self):
        archive = self._bootstrap()
        binding = render(str(archive.path), 3)["frame_bindings"][0]
        self.assertEqual(binding["status"], "retracted_release")
        self.assertEqual(binding["release_id"], "r2")
        self.assertEqual(binding["dark_id"], "d0")
        # r1 remains registered and unretracted but is not silently reused
        self.assertEqual(render(str(archive.path), 3)["summary"]
                         ["blocked_frames"], 1)

    def test_higher_version_covering_half_window_revives_only_that_half(self):
        archive = self._bootstrap()
        apply_batch(str(archive.path), batch(
            "b4", 3, add_releases=[release("r3", 3, start=0, end=50)]))
        inside = render(str(archive.path), 4)["frame_bindings"][0]
        self.assertEqual(inside["status"], "ready")
        self.assertEqual(inside["release_id"], "r3")
        # move the same frame to t=80, outside the half window: the retracted
        # r2 is still the highest covering release, so the frame stays blocked
        apply_batch(str(archive.path), batch(
            "b5", 4, put_frames=[frame("fr", 1, observed_at=80)]))
        outside = render(str(archive.path), 5)["frame_bindings"][0]
        self.assertEqual(outside["status"], "retracted_release")
        self.assertEqual(outside["frame_version"], 2)
        self.assertEqual(render(str(archive.path), 3)["frame_bindings"][0]
                         ["status"], "retracted_release")

    def test_frame_replace_withdraw_restore_and_version_chain(self):
        archive = TempArchive()
        self.addCleanup(archive.close)
        bootstrap(archive.path, classic_definition(), [
            batch("b1", 0, add_darks=[dark("d0")], add_flats=[flat()],
                  add_releases=[release("r1", 1)],
                  put_frames=[frame("fr", 0, value=10)]),
        ])
        # full replacement corrects the pixel fact; versions increment
        apply_batch(str(archive.path),
                    batch("b2", 1, put_frames=[frame("fr", 1, value=30)]))
        rep = render(str(archive.path), 2)
        self.assertEqual(rep["frame_bindings"][0]["frame_version"], 2)
        self.assertEqual(rep["report"]["rate_image"], [["30"]])
        # wrong expected_version is rejected and consumes nothing
        with self.assertRaises(ValueError):
            apply_batch(str(archive.path),
                        batch("bad", 2, withdraw_frames=[{"id": "fr",
                                                          "expected_version": 1}]))
        apply_batch(str(archive.path),
                    batch("b3", 2,
                          withdraw_frames=[{"id": "fr", "expected_version": 2}]))
        withdrawn = render(str(archive.path), 3)
        self.assertEqual(withdrawn["frame_bindings"][0]["status"], "withdrawn")
        self.assertEqual(withdrawn["frame_bindings"][0]["frame_version"], 3)
        self.assertIsNone(withdrawn["frame_bindings"][0]["release_id"])
        self.assertEqual(withdrawn["summary"]["withdrawn_frames"], 1)
        # restoring a withdrawn frame requires its current version
        with self.assertRaises(ValueError):
            apply_batch(str(archive.path),
                        batch("bad2", 3, put_frames=[frame("fr", 2)]))
        apply_batch(str(archive.path),
                    batch("b4", 3, put_frames=[frame("fr", 3, value=12)]))
        restored = render(str(archive.path), 4)
        self.assertEqual(restored["frame_bindings"][0]["status"], "ready")
        self.assertEqual(restored["frame_bindings"][0]["frame_version"], 4)
        self.assertEqual(restored["report"]["rate_image"], [["12"]])
        # historical revisions retain the old facts
        self.assertEqual(render(str(archive.path), 1)["report"]
                         ["rate_image"], [["10"]])

    def test_same_batch_release_can_reference_same_batch_master(self):
        archive = TempArchive()
        self.addCleanup(archive.close)
        receipt = initialize(str(archive.path), classic_definition())
        self.assertEqual(receipt, {"dataset_id": "ds", "revision": 0})
        receipt = apply_batch(str(archive.path), batch(
            "b1", 0, add_darks=[dark("d0")], add_flats=[flat()],
            add_releases=[release("r1", 1)], put_frames=[frame("fr", 0)]))
        self.assertEqual(receipt, {"dataset_id": "ds", "batch_id": "b1",
                                   "revision": 1})


if __name__ == "__main__":
    unittest.main()
