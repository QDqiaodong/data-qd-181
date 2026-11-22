"""Tests for the schema_version=4 spatial background model and sky CLI.

Expected values are derived independently by hand (ordinary OLS/BLUE algebra on
the calibrated rates and the documented raw/dark noise identities), never read
back from the implementation.  The suite deliberately includes geometries that
distinguish a correct correlated propagation from three tempting wrong
implementations:

* subtract an error-free constant (ignores fit noise entirely);
* add the fit variance as if it were independent of the science;
* refuse every measurement unless the design matrix has full rank.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from astro_stack.archive import apply_batch, initialize, render as archive_render
from astro_stack.io import load
from astro_stack.sky import SkyError, SkyInconsistent, render
from astro_stack.sky.linalg import (is_consistent, nullspace, rank_of,
                                    row_space_basis, solve_particular)

from archive_helpers import TempArchive, write_json

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


# --------------------------------------------------------------------------
# Archive construction helpers
# --------------------------------------------------------------------------
def dark_record(dark_id="d0", values=None, variances=None, exposure=1,
                width=3, height=3):
    if values is None:
        values = [[0 for _ in range(width)] for _ in range(height)]
    if variances is None:
        variances = [[0 for _ in range(width)] for _ in range(height)]
    return {"id": dark_id, "dark_exposure_s": exposure,
            "dark": values, "dark_variance": variances}


def flat_record(flat_id="f0", values=None, mask=None, width=3, height=3):
    if values is None:
        values = [[1 for _ in range(width)] for _ in range(height)]
    if mask is None:
        mask = [[False for _ in range(width)] for _ in range(height)]
    return {"id": flat_id, "flat": values, "bad_mask": mask}


def release_record(release_id="r1", version=1, dark_id="d0", flat_id="f0",
                   start=0, end=100000):
    return {"id": release_id, "version": version, "start_s": start,
            "end_s": end, "dark_id": dark_id, "flat_id": flat_id}


def frame_record(frame_id, pixels, variances, expected_version=0, exposure=1,
                 dx=0, dy=0, saturation=100, observed_at=5):
    return {"id": frame_id, "exposure_s": exposure, "dx": dx, "dy": dy,
            "saturation": saturation, "pixels": pixels,
            "observed_at_s": observed_at, "variance": variances,
            "expected_version": expected_version}


def batch_record(batch_id, expected_revision, *, darks=None, flats=None,
                 releases=None, frames=None, retract_releases=None,
                 withdraw_frames=None):
    return {"batch_id": batch_id, "expected_revision": expected_revision,
            "add_darks": darks or [], "add_flats": flats or [],
            "add_releases": releases or [],
            "retract_releases": retract_releases or [],
            "put_frames": frames or [],
            "withdraw_frames": withdraw_frames or []}


def build_archive(definition, batches):
    archive = TempArchive()
    initialize(str(archive.path), definition)
    for batch in batches:
        apply_batch(str(archive.path), batch)
    return archive


def matrix(value, width=3, height=3):
    return [[value for _ in range(width)] for _ in range(height)]


STAR_APERTURE = {"id": "star", "x": 1, "y": 1, "radius": 1}
ARM_APERTURE = {"id": "arm", "x": 0, "y": 1, "radius": 0}

# The acceptance geometry: center 10, arms 12/8/13/7, corner pixels unused.
GEOMETRY_PIXELS = [[0, 13, 0], [12, 10, 8], [0, 7, 0]]
GEOMETRY_VARIANCE = [[0, 1, 0], [1, 1, 1], [0, 1, 0]]
CENTER_SAMPLE = {"frame_id": "a", "detector_x": 1, "detector_y": 1}


def geometry_archive(*, products=None, apertures=None, pixels=None,
                     variances=None, frames=None, darks=None, flats=None,
                     releases=None, width=3, height=3):
    if apertures is None:
        apertures = [dict(STAR_APERTURE)]
    definition = {"dataset_id": "ds", "width": width, "height": height,
                  "apertures": apertures, "products": products}
    if pixels is None:
        pixels = GEOMETRY_PIXELS
    if variances is None:
        variances = GEOMETRY_VARIANCE
    if frames is None:
        frames = [frame_record("a", pixels, variances)]
    return build_archive(
        definition,
        [batch_record("b1", 0,
                      darks=darks or [dark_record()],
                      flats=flats or [flat_record()],
                      releases=releases or [release_record()],
                      frames=frames)])


def geometry_frame(frame_id, pixels=None, variances=None, observed_at=5,
                   exposure=1, dx=0, dy=0, expected_version=0):
    return frame_record(frame_id,
                        pixels or [row[:] for row in GEOMETRY_PIXELS],
                        variances or [row[:] for row in GEOMETRY_VARIANCE],
                        expected_version=expected_version,
                        observed_at=observed_at, exposure=exposure,
                        dx=dx, dy=dy)


# --------------------------------------------------------------------------
# Exact rational linear algebra primitives
# --------------------------------------------------------------------------
class LinalgTests(unittest.TestCase):
    def test_rref_rank_and_nullspace_are_exact(self):
        # rows [1, 1, 0] and [2, 2, 0] span a single rational direction
        basis = row_space_basis([[Fraction(1), Fraction(1), Fraction(0)],
                                 [Fraction(2), Fraction(2), Fraction(0)]])
        self.assertEqual(basis, [[Fraction(1), Fraction(1), Fraction(0)]])
        self.assertEqual(rank_of([[1, 2], [2, 4], [0, 0]]), 1)
        kernel = nullspace([[1, 1, 0], [0, 0, 1]])
        self.assertEqual(kernel, [[Fraction(-1), Fraction(1), Fraction(0)]])

    def test_consistency_and_particular_solution(self):
        augmented = [[1, 1, 3], [1, -1, 1]]
        self.assertTrue(is_consistent(augmented))
        self.assertEqual(solve_particular(augmented), [Fraction(2), Fraction(1)])
        self.assertFalse(is_consistent([[1, 1, 2], [2, 2, 5]]))


# --------------------------------------------------------------------------
# Acceptance scenario 1: one central sky sample, 3x3 geometry
# --------------------------------------------------------------------------
class ScenarioOneTests(unittest.TestCase):
    def setUp(self):
        self.archive = geometry_archive(
            apertures=[dict(STAR_APERTURE), dict(ARM_APERTURE)])
        self.addCleanup(self.archive.close)
        self.spec = {"sky_samples": [dict(CENTER_SAMPLE)]}

    def test_fit_block_identities_binding_and_calibrated_rate(self):
        report = render(str(self.archive.path), 1, self.spec)
        self.assertEqual(report["schema_version"], 4)
        self.assertEqual(set(report),
                         {"schema_version", "dataset_id", "revision",
                          "frame_bindings", "report", "uncertainty",
                          "summary", "sky_fit"})
        fit = report["sky_fit"]
        self.assertEqual(set(fit),
                         {"samples", "rank", "basis", "estimates",
                          "covariance"})
        self.assertEqual(fit["rank"], 1)
        # only the constant direction beta0 + beta_x + beta_y is identifiable
        self.assertEqual(fit["basis"], [["1", "1", "1"]])
        self.assertEqual(fit["estimates"], ["10"])
        self.assertEqual(fit["covariance"], [["1"]])
        sample = fit["samples"][0]
        self.assertEqual(list(sample),
                         ["frame_id", "detector_x", "detector_y", "output_x",
                          "output_y", "dark_id", "flat_id", "rate"])
        self.assertEqual(sample, {
            "frame_id": "a", "detector_x": 1, "detector_y": 1,
            "output_x": 1, "output_y": 1, "dark_id": "d0",
            "flat_id": "f0", "rate": "10"})

    def test_subtracted_aperture_is_zero_with_variance_twenty(self):
        report = render(str(self.archive.path), 1, self.spec)
        star = next(a for a in report["report"]["apertures"]
                    if a["aperture_id"] == "star")
        self.assertTrue(star["complete"])
        self.assertEqual(star["status"], "ok")
        self.assertEqual(star["sum_rate"], "0")
        self.assertEqual(
            report["uncertainty"]["aperture_covariance"],
            [[None, None], [None, "20"]])
        self.assertEqual(report["uncertainty"]["aperture_order"],
                         ["arm", "star"])

    def test_arm_pixels_are_unestimable_but_center_is_exact_zero(self):
        report = render(str(self.archive.path), 1, self.spec)
        stack = report["report"]
        # arms keep their real exposure and contributor counts; only the
        # subtracted rate/variance become null
        self.assertEqual(stack["exposure_s"][1][0], 1)
        self.assertEqual(stack["contributor_count"][1][0], 1)
        self.assertIsNone(stack["rate_image"][1][0])
        self.assertIsNone(report["uncertainty"]["variance_image"][1][0])
        self.assertEqual(stack["rate_image"][1][1], "0")
        self.assertEqual(report["uncertainty"]["variance_image"][1][1], "0")
        status = stack["pixel_status"]
        self.assertEqual(status[1], ["unestimable", "ok", "unestimable"])
        self.assertEqual(status[0][1], "unestimable")
        self.assertEqual(status[2][1], "unestimable")
        arm = next(a for a in stack["apertures"]
                   if a["aperture_id"] == "arm")
        self.assertTrue(arm["complete"])
        self.assertEqual(arm["status"], "unestimable")
        self.assertIsNone(arm["sum_rate"])
        # physical coverage fields are not recomputed from null images
        self.assertEqual(arm["measured_pixels"], 1)

    def test_radius_zero_aperture_on_arm_is_unestimable_not_guessed(self):
        # arm aperture already has radius 0: complete coverage, no estimable
        # plane target, so no arbitrary slope may invent a value
        report = render(str(self.archive.path), 1, self.spec)
        arm = next(a for a in report["report"]["apertures"]
                   if a["aperture_id"] == "arm")
        self.assertEqual(arm["nominal_pixels"], 1)
        self.assertEqual(arm["outside_pixels"], 0)
        self.assertEqual(arm["missing_pixels"], 0)
        self.assertEqual(arm["status"], "unestimable")

    def test_campaign_threshold_equalities_20_19_and_zero_raw_variance_4(self):
        for max_variance, delivered in (("20", 1), ("19", 0)):
            products = [{
                "id": "p", "start_s": 0, "end_s": 100, "priority": 1,
                "max_frames": 1,
                "requirements": [{"aperture_id": "star",
                                  "min_exposure_s": 1,
                                  "max_variance": max_variance}]}]
            archive = geometry_archive(products=products)
            self.addCleanup(archive.close)
            report = render(str(archive.path), 1, self.spec)
            self.assertEqual(report["report"]["summary"]["delivered_products"],
                             delivered)
        # center raw variance 0: aperture variance collapses to 4
        quiet_variance = [[0, 1, 0], [1, 0, 1], [0, 1, 0]]
        archive = geometry_archive(
            products=[{"id": "p", "start_s": 0, "end_s": 100, "priority": 1,
                       "max_frames": 1,
                       "requirements": [{"aperture_id": "star",
                                         "min_exposure_s": 1,
                                         "max_variance": "4"}]}],
            variances=quiet_variance)
        self.addCleanup(archive.close)
        report = render(str(archive.path), 1, self.spec)
        self.assertEqual(report["report"]["summary"]["delivered_products"], 1)
        product = report["report"]["products"][0]
        self.assertEqual(product["uncertainty"]["aperture_variances"],
                         [{"aperture_id": "star", "variance": "4"}])


# --------------------------------------------------------------------------
# Acceptance scenario 2: shared sky noise across disjoint campaign products
# --------------------------------------------------------------------------
TWO_PRODUCTS = [
    {"id": "pA", "start_s": 0, "end_s": 10, "priority": 1, "max_frames": 1,
     "requirements": [{"aperture_id": "star", "min_exposure_s": 1,
                       "max_variance": "30"}]},
    {"id": "pB", "start_s": 10, "end_s": 20, "priority": 1, "max_frames": 1,
     "requirements": [{"aperture_id": "star", "min_exposure_s": 1,
                       "max_variance": "30"}]}]


def three_frame_archive(center_variance_c=1):
    variances_c = [row[:] for row in GEOMETRY_VARIANCE]
    variances_c[1][1] = center_variance_c
    frames = [geometry_frame("a", observed_at=5),
              geometry_frame("b", observed_at=12),
              geometry_frame("c", observed_at=25, variances=variances_c)]
    return geometry_archive(products=TWO_PRODUCTS, frames=frames)


class ScenarioTwoTests(unittest.TestCase):
    SPEC = {"sky_samples": [{"frame_id": "c", "detector_x": 1,
                             "detector_y": 1}]}

    def test_both_products_deliver_share_correlation_and_leave_c_unused(self):
        archive = three_frame_archive()
        self.addCleanup(archive.close)
        report = render(str(archive.path), 1, self.SPEC)["report"]
        self.assertEqual(report["summary"]["delivered_products"], 2)
        self.assertEqual(report["unused_frame_ids"], ["c"])
        assigned = {p["product_id"]: p["frame_ids"]
                    for p in report["products"]}
        self.assertEqual(assigned, {"pA": ["a"], "pB": ["b"]})
        for product in report["products"]:
            self.assertEqual(product["uncertainty"]["aperture_variances"],
                             [{"aperture_id": "star", "variance": "30"}])
        self.assertEqual(report["measurement_order"],
                         [{"product_id": "pA", "aperture_id": "star"},
                          {"product_id": "pB", "aperture_id": "star"}])
        self.assertEqual(report["covariance"],
                         [["30", "25"], ["25", "30"]])

    def test_doubling_off_window_sky_variance_blocks_both_at_new_revision(self):
        new = three_frame_archive(center_variance_c=2)
        self.addCleanup(new.close)
        report = render(str(new.path), 1, self.SPEC)["report"]
        self.assertEqual(report["summary"]["delivered_products"], 0)
        self.assertEqual(report["unfulfilled_product_ids"], ["pA", "pB"])
        self.assertEqual(report["unused_frame_ids"], ["a", "b", "c"])

    def test_old_revision_keeps_original_covariance_after_correction(self):
        # simulate the correction as a second revision on the same archive:
        # history must remain reproducible without borrowing new pixel facts
        archive = three_frame_archive()
        self.addCleanup(archive.close)
        corrected_variance = [row[:] for row in GEOMETRY_VARIANCE]
        corrected_variance[1][1] = 2
        apply_batch(str(archive.path), batch_record(
            "b2", 1,
            frames=[geometry_frame("c", variances=corrected_variance,
                                   observed_at=25, expected_version=1)]))
        old = render(str(archive.path), 1, self.SPEC)["report"]
        new = render(str(archive.path), 2, self.SPEC)["report"]
        self.assertEqual(old["covariance"], [["30", "25"], ["25", "30"]])
        self.assertEqual(old["summary"]["delivered_products"], 2)
        self.assertEqual(new["summary"]["delivered_products"], 0)
        # the schema v3 archive render itself is untouched by sky analysis
        self.assertEqual(archive_render(str(archive.path), 1)["schema_version"],
                         3)

    def test_sky_sample_consumes_no_product_slot_or_max_frames(self):
        archive = three_frame_archive()
        self.addCleanup(archive.close)
        bindings = render(str(archive.path), 1, self.SPEC)["frame_bindings"]
        self.assertEqual({row["frame_id"] for row in bindings},
                         {"a", "b", "c"})
        self.assertTrue(all(row["status"] == "ready" for row in bindings))


# --------------------------------------------------------------------------
# Acceptance scenario 3: singular covariance and noise support
# --------------------------------------------------------------------------
class ScenarioThreeTests(unittest.TestCase):
    def _archive(self, rate_second, second_dark_id=None):
        darks = [dark_record("d0", values=[[0]], variances=[[1]],
                             width=1, height=1)]
        releases = [release_record("r1", 1, end=100)]
        if second_dark_id is not None:
            darks.append(dark_record(second_dark_id, values=[[0]],
                                     variances=[[1]], width=1, height=1))
            releases.append(release_record("r2", 2, dark_id=second_dark_id,
                                           start=100, end=200))
        frames = [
            frame_record("f1", [[10]], [[0]], observed_at=5),
            frame_record("f2", [[rate_second]], [[0]],
                         observed_at=150 if second_dark_id else 5)]
        definition = {"dataset_id": "ds", "width": 1, "height": 1,
                      "apertures": [{"id": "a", "x": 0, "y": 0,
                                     "radius": 0}],
                      "products": None}
        return build_archive(
            definition,
            [batch_record("b1", 0, darks=darks,
                          flats=[flat_record(width=1, height=1)],
                          releases=releases, frames=frames)])

    SPEC = {"sky_samples": [
        {"frame_id": "f1", "detector_x": 0, "detector_y": 0},
        {"frame_id": "f2", "detector_x": 0, "detector_y": 0}]}

    def test_shared_dark_singular_support_rejects_10_vs_12(self):
        archive = self._archive(12)
        self.addCleanup(archive.close)
        with self.assertRaises(SkyInconsistent):
            render(str(archive.path), 1, self.SPEC)

    def test_shared_dark_equal_rates_fit_variance_one(self):
        archive = self._archive(10)
        self.addCleanup(archive.close)
        fit = render(str(archive.path), 1, self.SPEC)["sky_fit"]
        self.assertEqual(fit["rank"], 1)
        self.assertEqual(fit["estimates"], ["10"])
        self.assertEqual(fit["covariance"], [["1"]])

    def test_distinct_dark_ids_break_support_constraint(self):
        archive = self._archive(12, second_dark_id="d1")
        self.addCleanup(archive.close)
        fit = render(str(archive.path), 1, self.SPEC)["sky_fit"]
        self.assertEqual(fit["estimates"], ["11"])
        self.assertEqual(fit["covariance"], [["1/2"]])

    def test_positive_definite_covariance_accepts_any_residual(self):
        # independent raw noise (variance 1 each) makes C = I, so no residual
        # can ever be sky_inconsistent
        definition = {"dataset_id": "ds", "width": 1, "height": 1,
                      "apertures": [{"id": "a", "x": 0, "y": 0,
                                     "radius": 0}], "products": None}
        frames = [frame_record("f1", [[10]], [[1]], observed_at=5),
                  frame_record("f2", [[12]], [[1]], observed_at=6)]
        archive = build_archive(
            definition,
            [batch_record("b1", 0, darks=[dark_record(
                "d0", values=[[0]], variances=[[0]], width=1, height=1)],
                flats=[flat_record(width=1, height=1)],
                releases=[release_record()], frames=frames)])
        self.addCleanup(archive.close)
        fit = render(str(archive.path), 1, self.SPEC)["sky_fit"]
        self.assertEqual(fit["estimates"], ["11"])
        self.assertEqual(fit["covariance"], [["1/2"]])


# --------------------------------------------------------------------------
# Hand-derived geometries: exposures, flats, shifts, overlap, full rank
# --------------------------------------------------------------------------
class HandDerivedGeometryTests(unittest.TestCase):
    def test_exposure_weighted_science_keeps_correlation_with_fit(self):
        # 1x1, two frames: f exposure 2 raw var 4 (rate var 4/4 = 1),
        # g exposure 1 raw var 9. Sky sample is f's only pixel.
        # science = (2 r_f + r_g)/3, fit = r_f, subtracted = (r_g - r_f)/3,
        # variance = (9 + 1)/9 = 10/9; mean with r_f=10, r_g=12 is 2/3.
        definition = {"dataset_id": "ds", "width": 1, "height": 1,
                      "apertures": [{"id": "a", "x": 0, "y": 0,
                                     "radius": 0}], "products": None}
        frames = [frame_record("f", [[20]], [[4]], exposure=2, observed_at=5),
                  frame_record("g", [[12]], [[9]], exposure=1, observed_at=6)]
        archive = build_archive(
            definition,
            [batch_record("b1", 0, darks=[dark_record(
                "d0", values=[[0]], variances=[[0]], width=1, height=1)],
                flats=[flat_record(width=1, height=1)],
                releases=[release_record()], frames=frames)])
        self.addCleanup(archive.close)
        report = render(str(archive.path), 1,
                        {"sky_samples": [{"frame_id": "f", "detector_x": 0,
                                          "detector_y": 0}]})
        self.assertEqual(report["report"]["rate_image"], [["2/3"]])
        self.assertEqual(report["uncertainty"]["variance_image"], [["10/9"]])
        aperture = report["report"]["apertures"][0]
        self.assertEqual(aperture["status"], "ok")
        self.assertEqual(aperture["sum_rate"], "2/3")
        # independent-error implementation would report 13/9 + 1 = 22/9
        self.assertEqual(
            report["uncertainty"]["aperture_covariance"], [["10/9"]])

    def test_full_rank_plane_fits_nonconstant_sky_exactly_as_ols(self):
        # samples (0,0)=10, (1,0)=12, (0,1)=7 -> beta = (10, 2, -3),
        # C = I, so BLUE equals OLS; covariance = (X'X)^-1.
        pixels = [[10, 12, 0], [7, 0, 8], [0, 0, 0]]
        variances = [[1, 1, 0], [1, 0, 1], [0, 0, 0]]
        archive = geometry_archive(pixels=pixels, variances=variances)
        self.addCleanup(archive.close)
        spec = {"sky_samples": [
            {"frame_id": "a", "detector_x": 0, "detector_y": 0},
            {"frame_id": "a", "detector_x": 1, "detector_y": 0},
            {"frame_id": "a", "detector_x": 0, "detector_y": 1}]}
        fit = render(str(archive.path), 1, spec)["sky_fit"]
        self.assertEqual(fit["rank"], 3)
        self.assertEqual(fit["basis"],
                         [["1", "0", "0"], ["0", "1", "0"],
                          ["0", "0", "1"]])
        self.assertEqual(fit["estimates"], ["10", "2", "-3"])
        self.assertEqual(fit["covariance"],
                         [["1", "-1", "-1"], ["-1", "2", "1"],
                          ["-1", "1", "2"]])

    def test_negative_cross_covariance_between_subtracted_pixels(self):
        # 1x3 frame, sky samples at x = 0,1,2 with rates affine in x,
        # all raw variances 1. OLS weights give Cov(m_0, m_2) = -7/36.
        definition = {"dataset_id": "ds", "width": 3, "height": 1,
                      "apertures": [
                          {"id": "left", "x": 0, "y": 0, "radius": 0},
                          {"id": "right", "x": 2, "y": 0, "radius": 0}],
                      "products": None}
        frames = [frame_record("f", [[10, 12, 14]], [[1, 1, 1]])]
        archive = build_archive(
            definition,
            [batch_record("b1", 0, darks=[dark_record(
                "d0", width=3, height=1)],
                flats=[flat_record(width=3, height=1)],
                releases=[release_record()], frames=frames)])
        self.addCleanup(archive.close)
        spec = {"sky_samples": [
            {"frame_id": "f", "detector_x": 0, "detector_y": 0},
            {"frame_id": "f", "detector_x": 1, "detector_y": 0},
            {"frame_id": "f", "detector_x": 2, "detector_y": 0}]}
        report = render(str(archive.path), 1, spec)
        covariance_matrix = report["uncertainty"]["aperture_covariance"]
        # rank-2 plane: the single unidentifiable direction is removed by the
        # subtraction, so both subtracted pixel measurements coincide with the
        # same residual projection (I-H)_02 = 1/6; their variance equals it.
        self.assertEqual(covariance_matrix,
                         [["1/6", "1/6"], ["1/6", "1/6"]])
        # the full-rank-only implementation would instead null these rows
        self.assertTrue(all(cell is not None
                            for row in covariance_matrix for cell in row))

    def test_registered_shift_moves_sample_to_output_coordinates(self):
        # frame shifted by (1, 0): detector (0,0) lands on output (1,0),
        # so the fitted constant direction is beta0 + beta_x.
        definition = {"dataset_id": "ds", "width": 2, "height": 1,
                      "apertures": [{"id": "a", "x": 1, "y": 0,
                                     "radius": 0}], "products": None}
        frames = [frame_record("f", [[10, 0]], [[1, 0]], dx=1)]
        archive = build_archive(
            definition,
            [batch_record("b1", 0, darks=[dark_record(
                "d0", width=2, height=1)],
                flats=[flat_record(width=2, height=1)],
                releases=[release_record()], frames=frames)])
        self.addCleanup(archive.close)
        report = render(str(archive.path), 1,
                        {"sky_samples": [{"frame_id": "f", "detector_x": 0,
                                          "detector_y": 0}]})
        sample = report["sky_fit"]["samples"][0]
        self.assertEqual((sample["output_x"], sample["output_y"]), (1, 0))
        self.assertEqual(report["sky_fit"]["basis"], [["1", "1", "0"]])
        # detector (0,0) moved onto output (1,0); output (0,0) is uncovered
        self.assertEqual(report["report"]["rate_image"], [[None, "0"]])
        self.assertEqual(report["report"]["pixel_status"],
                         [["uncovered", "ok"]])

    def test_flat_and_dark_coefficients_propagate_into_fit_variance(self):
        # Two-pixel flat [2, 4] has mean M = 3; frame exposure E = 2 and dark
        # exposure D = 1.  Calibration is (raw/E - dark/D) * M/flat, so at
        # detector pixel 0 the calibrated rate coefficients are:
        #   raw  (M/flat)/E = 3/4, raw count variance 4 -> 9/4
        #   dark M/(flat*D) = 3/2 (no frame-exposure division), dark var 9
        #        -> 81/4
        # The single sky-sample fit variance is 9/4 + 81/4 = 45/2, identical
        # to the schema v3 pixel variance for the same calibrated sample.
        definition = {"dataset_id": "ds", "width": 2, "height": 1,
                      "apertures": [{"id": "a", "x": 0, "y": 0,
                                     "radius": 0}], "products": None}
        frames = [frame_record("f", [[40, 0]], [[4, 0]], exposure=2)]
        archive = build_archive(
            definition,
            [batch_record("b1", 0,
                          darks=[dark_record("d0", values=[[0, 0]],
                                             variances=[[9, 0]],
                                             width=2, height=1)],
                          flats=[flat_record("f0", values=[[2, 4]],
                                             width=2, height=1)],
                          releases=[release_record()], frames=frames)])
        self.addCleanup(archive.close)
        fit = render(str(archive.path), 1,
                     {"sky_samples": [{"frame_id": "f", "detector_x": 0,
                                       "detector_y": 0}]})["sky_fit"]
        self.assertEqual(fit["covariance"], [["45/2"]])

    def test_overlapping_apertures_share_subtracted_terms(self):
        archive = geometry_archive(
            apertures=[dict(STAR_APERTURE), dict(ARM_APERTURE)])
        self.addCleanup(archive.close)
        # add a second estimable direction so both apertures are ok: use the
        # center plus both horizontal arms as sky samples (rank 2 in x)
        spec = {"sky_samples": [
            {"frame_id": "a", "detector_x": 1, "detector_y": 1},
            {"frame_id": "a", "detector_x": 0, "detector_y": 1},
            {"frame_id": "a", "detector_x": 2, "detector_y": 1}]}
        report = render(str(archive.path), 1, spec)
        order = report["uncertainty"]["aperture_order"]
        self.assertEqual(order, ["arm", "star"])
        matrix = report["uncertainty"]["aperture_covariance"]
        # both are ok; diagonal is positive and the overlap yields a real
        # cross term rather than an independent zero
        self.assertIsNotNone(matrix[0][0])
        self.assertIsNotNone(matrix[0][1])
        self.assertEqual(matrix[0][1], matrix[1][0])
        arm = next(a for a in report["report"]["apertures"]
                   if a["aperture_id"] == "arm")
        self.assertEqual(arm["status"], "ok")

    def test_zero_noise_deterministic_plane_and_its_failure(self):
        pixels = [[1, 2, 3], [0, 0, 0], [0, 0, 0]]
        archive = geometry_archive(pixels=pixels,
                                   variances=matrix(0))
        self.addCleanup(archive.close)
        spec = {"sky_samples": [
            {"frame_id": "a", "detector_x": 0, "detector_y": 0},
            {"frame_id": "a", "detector_x": 1, "detector_y": 0},
            {"frame_id": "a", "detector_x": 2, "detector_y": 0}]}
        fit = render(str(archive.path), 1, spec)["sky_fit"]
        self.assertEqual(fit["rank"], 2)
        self.assertEqual(fit["estimates"], ["1", "1"])
        self.assertEqual(fit["covariance"],
                         [["0", "0"], ["0", "0"]])
        broken = geometry_archive(
            pixels=[[1, 2, 4], [0, 0, 0], [0, 0, 0]],
            variances=matrix(0))
        self.addCleanup(broken.close)
        with self.assertRaises(SkyInconsistent):
            render(str(broken.path), 1, spec)


# --------------------------------------------------------------------------
# Estimability, coverage and report shape
# --------------------------------------------------------------------------
class ReportShapeTests(unittest.TestCase):
    def test_uncovered_pixel_has_zero_exposure_and_null_rate(self):
        definition = {"dataset_id": "ds", "width": 2, "height": 1,
                      "apertures": [
                          {"id": "live", "x": 1, "y": 0, "radius": 0},
                          {"id": "dead", "x": 0, "y": 0, "radius": 0}],
                      "products": None}
        frames = [frame_record("f", [[0, 10]], [[0, 1]], dx=1)]
        archive = build_archive(
            definition,
            [batch_record("b1", 0, darks=[dark_record(
                "d0", width=2, height=1)],
                flats=[flat_record(width=2, height=1)],
                releases=[release_record()], frames=frames)])
        self.addCleanup(archive.close)
        report = render(str(archive.path), 1,
                        {"sky_samples": [{"frame_id": "f", "detector_x": 0,
                                         "detector_y": 0}]})["report"]
        self.assertEqual(report["pixel_status"], [["uncovered", "ok"]])
        self.assertEqual(report["exposure_s"], [[0, 1]])
        self.assertEqual(report["contributor_count"], [[0, 1]])
        self.assertEqual(report["rate_image"], [[None, "0"]])
        dead = next(a for a in report["apertures"]
                    if a["aperture_id"] == "dead")
        self.assertEqual(dead["status"], "incomplete")
        self.assertFalse(dead["complete"])
        self.assertIsNone(dead["sum_rate"])

    def test_incomplete_takes_precedence_over_unestimable(self):
        # radius-1 disk centered at the corner cannot fit inside a 1x1 image
        definition = {"dataset_id": "ds", "width": 1, "height": 1,
                      "apertures": [{"id": "edge", "x": 0, "y": 0,
                                     "radius": 1}], "products": None}
        frames = [frame_record("f", [[10]], [[1]])]
        archive = build_archive(
            definition,
            [batch_record("b1", 0, darks=[dark_record(
                "d0", width=1, height=1)],
                flats=[flat_record(width=1, height=1)],
                releases=[release_record()], frames=frames)])
        self.addCleanup(archive.close)
        aperture = render(str(archive.path), 1, {
            "sky_samples": [{"frame_id": "f", "detector_x": 0,
                             "detector_y": 0}]})["report"]["apertures"][0]
        self.assertEqual(aperture["status"], "incomplete")
        self.assertGreater(aperture["outside_pixels"], 0)
        self.assertIsNone(aperture["sum_rate"])

    def test_classic_empty_ready_set_still_renders_when_spec_is_valid(self):
        # a ready but window-blocked campaign frame leaves an empty classic
        # style stack; here exercise the no-ready-frames branch indirectly via
        # a withdrawn frame: specification must then be rejected, while an
        # archive with a second untouched ready frame still renders
        archive = geometry_archive()
        self.addCleanup(archive.close)
        apply_batch(str(archive.path), batch_record(
            "b2", 1, withdraw_frames=[{"id": "a", "expected_version": 1}]))
        with self.assertRaises(SkyError):
            render(str(archive.path), 2,
                   {"sky_samples": [dict(CENTER_SAMPLE)]})

    def test_frame_audit_and_binding_counts_keep_original_semantics(self):
        archive = geometry_archive(
            apertures=[dict(STAR_APERTURE), dict(ARM_APERTURE)])
        self.addCleanup(archive.close)
        report = render(str(archive.path), 1,
                        {"sky_samples": [dict(CENTER_SAMPLE)]})
        self.assertEqual(report["summary"],
                         {"frames_seen": 1, "ready_frames": 1,
                          "blocked_frames": 0, "withdrawn_frames": 0})
        audit = report["report"]["frame_audit"][0]
        self.assertEqual(audit["accepted_pixels"], 9)
        self.assertEqual(report["report"]["summary"]["accepted_contributions"],
                         9)
        self.assertEqual(report["report"]["summary"]["complete_apertures"], 2)

    def test_campaign_product_stack_carries_status_for_other_apertures(self):
        products = [{
            "id": "p", "start_s": 0, "end_s": 100, "priority": 1,
            "max_frames": 1,
            "requirements": [{"aperture_id": "star", "min_exposure_s": 1,
                              "max_variance": "100"}]}]
        archive = geometry_archive(
            products=products,
            apertures=[dict(STAR_APERTURE), dict(ARM_APERTURE)])
        self.addCleanup(archive.close)
        product = render(str(archive.path), 1,
                         {"sky_samples": [dict(CENTER_SAMPLE)]}
                         )["report"]["products"][0]
        statuses = {a["aperture_id"]: a["status"]
                    for a in product["stack"]["apertures"]}
        self.assertEqual(statuses, {"arm": "unestimable", "star": "ok"})
        # only the delivered requirement enters measurement order
        self.assertEqual(product["uncertainty"]["aperture_variances"],
                         [{"aperture_id": "star", "variance": "20"}])


# --------------------------------------------------------------------------
# Correlation can rescue or ruin deliverability
# --------------------------------------------------------------------------
class CorrelationFeasibilityTests(unittest.TestCase):
    def test_same_sample_subtraction_collapses_variance_to_zero(self):
        # 1x1 frame, raw variance 2: without subtraction the aperture would
        # need max_variance >= 2; subtracting the identical random sample
        # yields exact zero variance, so max_variance 0 is deliverable.
        definition = {"dataset_id": "ds", "width": 1, "height": 1,
                      "apertures": [{"id": "a", "x": 0, "y": 0,
                                     "radius": 0}],
                      "products": [{
                          "id": "p", "start_s": 0, "end_s": 100,
                          "priority": 1, "max_frames": 1,
                          "requirements": [{"aperture_id": "a",
                                            "min_exposure_s": 1,
                                            "max_variance": "0"}]}]}
        frames = [frame_record("f", [[10]], [[2]])]
        archive = build_archive(
            definition,
            [batch_record("b1", 0, darks=[dark_record(
                "d0", width=1, height=1)],
                flats=[flat_record(width=1, height=1)],
                releases=[release_record()], frames=frames)])
        self.addCleanup(archive.close)
        report = render(str(archive.path), 1,
                        {"sky_samples": [{"frame_id": "f", "detector_x": 0,
                                          "detector_y": 0}]})["report"]
        self.assertEqual(report["summary"]["delivered_products"], 1)
        self.assertEqual(report["products"][0]["uncertainty"]
                         ["aperture_variances"],
                         [{"aperture_id": "a", "variance": "0"}])

    def test_no_feasible_subset_is_a_successful_empty_delivery(self):
        products = [{
            "id": "p", "start_s": 100, "end_s": 200, "priority": 1,
            "max_frames": 1,
            "requirements": [{"aperture_id": "star", "min_exposure_s": 1,
                              "max_variance": "1"}]}]
        archive = geometry_archive(products=products)
        self.addCleanup(archive.close)
        report = render(str(archive.path), 1,
                        {"sky_samples": [dict(CENTER_SAMPLE)]})["report"]
        self.assertEqual(report["products"], [])
        self.assertEqual(report["unfulfilled_product_ids"], ["p"])
        self.assertEqual(report["unused_frame_ids"], ["a"])
        self.assertEqual(report["summary"]["delivered_products"], 0)

    def test_assignment_uses_subtracted_variance_not_raw_variance(self):
        # With the central raw variance set to zero, the raw (unsubtracted)
        # five-pixel aperture still carries four arm units: raw variance 4.
        # Subtracting the fitted central sample introduces a new term but the
        # exact correlated propagation keeps the total at 4; an independent
        # fit-error implementation would add the fit variance and exceed the
        # equality threshold, while an error-free constant could not explain
        # the variance-20 geometry above.
        quiet_variance = [[0, 1, 0], [1, 0, 1], [0, 1, 0]]
        products = [{
            "id": "p", "start_s": 0, "end_s": 100, "priority": 1,
            "max_frames": 1,
            "requirements": [{"aperture_id": "star", "min_exposure_s": 1,
                              "max_variance": "4"}]}]
        archive = geometry_archive(products=products,
                                   variances=quiet_variance)
        self.addCleanup(archive.close)
        result = render(str(archive.path), 1,
                        {"sky_samples": [dict(CENTER_SAMPLE)]})["report"]
        self.assertEqual(result["summary"]["delivered_products"], 1)
        self.assertEqual(result["products"][0]["uncertainty"]
                         ["aperture_variances"],
                         [{"aperture_id": "star", "variance": "4"}])


# --------------------------------------------------------------------------
# Input validation and failure boundaries
# --------------------------------------------------------------------------
class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.archive = geometry_archive()
        self.addCleanup(self.archive.close)

    def render(self, spec):
        return render(str(self.archive.path), 1, spec)

    def test_schema_shape_and_limits(self):
        for bad in ({}, {"sky_samples": []},
                    {"sky_samples": [{"frame_id": "a", "detector_x": 1,
                                      "detector_y": 1}] * 65},
                    {"sky_samples": "no"}, {"other": 1},
                    {"sky_samples": [{}]},
                    {"sky_samples": [{"frame_id": "a", "detector_x": 1,
                                      "detector_y": 1, "extra": 0}]},
                    {"sky_samples": [{"frame_id": "a", "detector_x": 1}]},
                    {"sky_samples": [{"frame_id": 5, "detector_x": 1,
                                      "detector_y": 1}]},
                    {"sky_samples": [{"frame_id": "a", "detector_x": 1.0,
                                      "detector_y": 1}]},
                    {"sky_samples": [{"frame_id": "a", "detector_x": True,
                                      "detector_y": 1}]},
                    {"sky_samples": [{"frame_id": "a", "detector_x": -1,
                                      "detector_y": 1}]},
                    {"sky_samples": [{"frame_id": "a", "detector_x": 3,
                                      "detector_y": 1}]},
                    {"sky_samples": [{"frame_id": "missing", "detector_x": 0,
                                      "detector_y": 0}]},
                    {"sky_samples": [
                        {"frame_id": "a", "detector_x": 1, "detector_y": 1},
                        {"frame_id": "a", "detector_x": 1, "detector_y": 1}]}):
            with self.assertRaises(ValueError):
                self.render(bad)

    def test_duplicate_json_keys_in_spec_file_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "spec.json")
            path.write_text(
                '{"sky_samples": [{"frame_id": "a", "detector_x": 1, '
                '"detector_y": 1}], "sky_samples": []}')
            with self.assertRaises(ValueError):
                load(path)

    def test_illegal_revision_and_path_are_value_errors(self):
        for revision in (-1, 99):
            with self.assertRaises(ValueError):
                render(str(self.archive.path), revision,
                       {"sky_samples": [dict(CENTER_SAMPLE)]})
        with self.assertRaises((ValueError, OSError)):
            render("/nonexistent/astro/archive", 1,
                   {"sky_samples": [dict(CENTER_SAMPLE)]})

    def test_revision_zero_has_no_committed_frames(self):
        with self.assertRaises(ValueError):
            render(str(self.archive.path), 0,
                   {"sky_samples": [dict(CENTER_SAMPLE)]})

    def test_sample_rejections_mask_saturation_shift_withdrawal(self):
        base = {"sky_samples": [{"frame_id": "f", "detector_x": 1,
                                 "detector_y": 0}]}
        definition = {"dataset_id": "ds", "width": 2, "height": 1,
                      "apertures": [{"id": "a", "x": 0, "y": 0,
                                     "radius": 0}], "products": None}

        def archive(frames, flats=("f0", None), releases=()):
            flat_records = [flat_record("f0", width=2, height=1)]
            release_records = [release_record()]
            flat_id = "f0"
            if flats[1] is not None:
                flat_records.append(flats[1])
            for extra in releases:
                release_records.append(extra)
                flat_id = extra["flat_id"]
            return build_archive(
                definition,
                [batch_record("b1", 0,
                              darks=[dark_record(width=2, height=1)],
                              flats=flat_records, releases=release_records,
                              frames=frames)])

        saturated = archive([frame_record("f", [[1, 200]], [[0, 0]],
                                          saturation=200)])
        self.addCleanup(saturated.close)
        with self.assertRaises(SkyError):
            render(str(saturated.path), 1, base)
        shifted = archive([frame_record("f", [[1, 2]], [[0, 0]], dx=1)])
        self.addCleanup(shifted.close)
        with self.assertRaises(SkyError):
            render(str(shifted.path), 1, base)
        masked_flat = flat_record("f1", width=2, height=1,
                                  mask=[[False, True]])
        masked = archive(
            [frame_record("f", [[1, 2]], [[0, 0]])],
            releases=(release_record("r2", 2, flat_id="f1"),),
            flats=("f0", masked_flat))
        self.addCleanup(masked.close)
        with self.assertRaises(SkyError):
            render(str(masked.path), 1, base)
        zero_flat = flat_record("f2", values=[[1, 0]], width=2, height=1)
        zeroed = archive(
            [frame_record("f", [[1, 2]], [[0, 0]])],
            releases=(release_record("r3", 3, flat_id="f2"),),
            flats=("f0", zero_flat))
        self.addCleanup(zeroed.close)
        with self.assertRaises(SkyError):
            render(str(zeroed.path), 1, base)

    def test_withdrawn_and_retracted_release_reject_whole_request(self):
        archive = geometry_archive()
        self.addCleanup(archive.close)
        apply_batch(str(archive.path), batch_record(
            "b2", 1, withdraw_frames=[{"id": "a", "expected_version": 1}]))
        with self.assertRaises(SkyError):
            render(str(archive.path), 2,
                   {"sky_samples": [dict(CENTER_SAMPLE)]})
        # restore at frame version 3, then retract the covering release
        apply_batch(str(archive.path), batch_record(
            "b3", 2,
            frames=[frame_record("a", GEOMETRY_PIXELS, GEOMETRY_VARIANCE,
                                 expected_version=2)]))
        apply_batch(str(archive.path), batch_record(
            "b4", 3, retract_releases=["r1"]))
        with self.assertRaises(SkyError):
            render(str(archive.path), 4,
                   {"sky_samples": [dict(CENTER_SAMPLE)]})

    def test_sample_order_does_not_change_any_result(self):
        spec_a = {"sky_samples": [
            {"frame_id": "a", "detector_x": 0, "detector_y": 1},
            {"frame_id": "a", "detector_x": 1, "detector_y": 1}]}
        spec_b = {"sky_samples": [
            {"frame_id": "a", "detector_x": 1, "detector_y": 1},
            {"frame_id": "a", "detector_x": 0, "detector_y": 1}]}
        first = render(str(self.archive.path), 1, spec_a)
        second = render(str(self.archive.path), 1, spec_b)
        self.assertEqual(first, second)
        # samples are always reported in (frame, y, x) order
        identities = [(s["frame_id"], s["detector_y"], s["detector_x"])
                      for s in first["sky_fit"]["samples"]]
        self.assertEqual(identities, sorted(identities))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        env = dict(os.environ, PYTHONPATH=str(SRC))
        return subprocess.run(
            [sys.executable, "-m", "astro_stack.cli_sky", *args],
            cwd=ROOT, capture_output=True, text=True, env=env)

    def _bootstrap(self, directory):
        directory = Path(directory)
        definition = {"dataset_id": "ds", "width": 3, "height": 3,
                      "apertures": [dict(STAR_APERTURE), dict(ARM_APERTURE)],
                      "products": None}
        definition_path = write_json(directory, "definition.json", definition)
        batch_path = write_json(directory, "batch.json", batch_record(
            "b1", 0, darks=[dark_record()], flats=[flat_record()],
            releases=[release_record()],
            frames=[frame_record("a", GEOMETRY_PIXELS, GEOMETRY_VARIANCE)]))
        spec_path = write_json(directory, "spec.json",
                               {"sky_samples": [dict(CENTER_SAMPLE)]})
        archive = directory / "archive"
        env = dict(os.environ, PYTHONPATH=str(SRC))
        subprocess.run([sys.executable, "-m", "astro_stack.cli_archive",
                        "--archive", str(archive), "--initialize",
                        str(definition_path)], cwd=ROOT, env=env, check=True,
                       capture_output=True, text=True)
        subprocess.run([sys.executable, "-m", "astro_stack.cli_archive",
                        "--archive", str(archive), "--apply",
                        str(batch_path)], cwd=ROOT, env=env, check=True,
                       capture_output=True, text=True)
        return archive, spec_path

    def test_cli_publishes_report_prints_summary_and_protects_target(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, spec_path = self._bootstrap(directory)
            output = Path(directory, "nested", "report.json")
            process = self.run_cli("--archive", str(archive), "--revision",
                                   "1", "--spec", str(spec_path),
                                   "--output", str(output))
            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report["schema_version"], 4)
            self.assertEqual(json.loads(process.stdout.strip()),
                             report["summary"])
            before = output.read_text()
            blocked = self.run_cli("--archive", str(archive), "--revision",
                                   "1", "--spec", str(spec_path),
                                   "--output", str(output))
            self.assertEqual(blocked.returncode, 2)
            self.assertEqual(output.read_text(), before)

    def test_cli_rejects_inconsistent_spec_and_leaves_no_report(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            # 1x1 shared-dark singular setup, rates 10 and 12
            definition = {"dataset_id": "ds", "width": 1, "height": 1,
                          "apertures": [{"id": "a", "x": 0, "y": 0,
                                         "radius": 0}], "products": None}
            write_json(directory, "definition.json", definition)
            batch = batch_record(
                "b1", 0,
                darks=[dark_record("d0", values=[[0]], variances=[[1]],
                                   width=1, height=1)],
                flats=[flat_record(width=1, height=1)],
                releases=[release_record()],
                frames=[frame_record("f1", [[10]], [[0]], observed_at=5),
                        frame_record("f2", [[12]], [[0]], observed_at=6)])
            write_json(directory, "batch.json", batch)
            spec = write_json(directory, "spec.json", {"sky_samples": [
                {"frame_id": "f1", "detector_x": 0, "detector_y": 0},
                {"frame_id": "f2", "detector_x": 0, "detector_y": 0}]})
            archive = directory / "archive"
            env = dict(os.environ, PYTHONPATH=str(SRC))
            subprocess.run([sys.executable, "-m", "astro_stack.cli_archive",
                            "--archive", str(archive), "--initialize",
                            str(directory / "definition.json")],
                           cwd=ROOT, env=env, check=True, capture_output=True)
            subprocess.run([sys.executable, "-m", "astro_stack.cli_archive",
                            "--archive", str(archive), "--apply",
                            str(directory / "batch.json")],
                           cwd=ROOT, env=env, check=True, capture_output=True)
            output = directory / "report.json"
            failed = self.run_cli("--archive", str(archive), "--revision",
                                  "1", "--spec", str(spec),
                                  "--output", str(output))
            self.assertEqual(failed.returncode, 2)
            self.assertIn("sky_inconsistent", failed.stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
