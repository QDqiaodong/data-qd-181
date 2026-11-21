import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from astro_stack.service import run

ROOT = Path(__file__).resolve().parents[1]


def campaign_1x1(frames, products, dark_variance=((1,),), dark=((0,),),
                 flat=((1,),), mask=((False,),), apertures=None,
                 dark_exposure_s=1):
    if apertures is None:
        apertures = [{"id": "a", "x": 0, "y": 0, "radius": 0}]
    return {
        "width": 1, "height": 1, "dark_exposure_s": dark_exposure_s,
        "dark": [list(row) for row in dark],
        "flat": [list(row) for row in flat],
        "bad_mask": [list(row) for row in mask],
        "frames": copy.deepcopy(frames),
        "apertures": apertures,
        "campaign": {"dark_variance": [list(row) for row in dark_variance],
                     "products": copy.deepcopy(products)}}


def frame(frame_id, value=10, variance=1, observed_at=0, exposure=1,
          dx=0, dy=0, saturation=100):
    return {"id": frame_id, "exposure_s": exposure, "dx": dx, "dy": dy,
            "saturation": saturation, "pixels": [[value]],
            "observed_at_s": observed_at, "variance": [[variance]]}


def product(product_id, start=0, end=10, priority=1, max_frames=1,
            requirements=None):
    if requirements is None:
        requirements = [{"aperture_id": "a", "min_exposure_s": 1,
                         "max_variance": "100"}]
    return {"id": product_id, "start_s": start, "end_s": end,
            "priority": priority, "max_frames": max_frames,
            "requirements": requirements}


def requirement(aperture_id="a", min_exposure_s=1, max_variance="100"):
    return {"aperture_id": aperture_id, "min_exposure_s": min_exposure_s,
            "max_variance": max_variance}


class SharedDarkUncertaintyTests(unittest.TestCase):
    def test_single_and_stacked_rate_variance_with_shared_dark(self):
        data = campaign_1x1(
            [frame("f1", observed_at=0), frame("f2", observed_at=1)],
            [product("p", max_frames=2,
                     requirements=[requirement(max_variance="1")])],
            dark_variance=((1,),))
        result = run(data)
        self.assertEqual(result["unfulfilled_product_ids"], ["p"])
        self.assertEqual(result["products"], [])

        # threshold exactly 3/2 admits only the two-frame stack
        data["campaign"]["products"][0]["requirements"][0]["max_variance"] = "3/2"
        result = run(data)
        delivered = result["products"][0]
        self.assertEqual(delivered["frame_ids"], ["f1", "f2"])
        self.assertEqual(delivered["uncertainty"]["variance_image"], [["3/2"]])
        self.assertEqual(delivered["uncertainty"]["aperture_variances"],
                         [{"aperture_id": "a", "variance": "3/2"}])
        self.assertEqual(result["covariance"], [["3/2"]])

    def test_dark_variance_zero_allows_cheapest_single_frame(self):
        data = campaign_1x1(
            [frame("f1", observed_at=0), frame("f2", observed_at=1)],
            [product("p", max_frames=2,
                     requirements=[requirement(max_variance="1")])],
            dark_variance=((0,),))
        result = run(data)
        delivered = result["products"][0]
        self.assertEqual(delivered["frame_ids"], ["f1"])
        self.assertEqual(delivered["uncertainty"]["aperture_variances"],
                         [{"aperture_id": "a", "variance": "1"}])
        self.assertEqual(delivered["stack"]["rate_image"], [["10"]])
        self.assertEqual(result["unused_frame_ids"], ["f2"])
        self.assertEqual(result["summary"],
                         {"requested_products": 1, "delivered_products": 1,
                          "total_priority": 1, "used_exposure_s": 1,
                          "used_frames": 1})

    def test_dark_error_is_shared_not_independently_averaged(self):
        # With raw variance removed, the whole 3/2 must come from the shared dark term.
        data = campaign_1x1(
            [frame("f1", variance=0, observed_at=0),
             frame("f2", variance=0, observed_at=1)],
            [product("p", max_frames=2,
                     requirements=[requirement(max_variance="100")])],
            dark_variance=((1,),))
        result = run(data)
        delivered = result["products"][0]
        # A single frame gives 1; two correlated dark draws still give 1, never 1/2.
        self.assertEqual(delivered["frame_ids"], ["f1"])
        self.assertEqual(delivered["uncertainty"]["variance_image"], [["1"]])


class CrossMeasurementCovarianceTests(unittest.TestCase):
    def _shifted_two_pixel_data(self):
        return {
            "width": 2, "height": 1, "dark_exposure_s": 1,
            "dark": [[0, 0]], "flat": [[1, 1]], "bad_mask": [[False, False]],
            "frames": [
                {"id": "f0", "exposure_s": 1, "dx": 0, "dy": 0, "saturation": 100,
                 "pixels": [[0, 0]], "observed_at_s": 0, "variance": [[0, 0]]},
                {"id": "f1", "exposure_s": 1, "dx": 1, "dy": 0, "saturation": 100,
                 "pixels": [[0, 0]], "observed_at_s": 10, "variance": [[0, 0]]}],
            "apertures": [{"id": "a0", "x": 0, "y": 0, "radius": 0},
                          {"id": "a1", "x": 1, "y": 0, "radius": 0}],
            "campaign": {
                "dark_variance": [[1, 4]],
                "products": [
                    product("p0", start=0, end=5,
                            requirements=[requirement("a0", max_variance="100")]),
                    product("p1", start=5, end=15,
                            requirements=[requirement("a1", max_variance="100")])]}}

    def test_shift_preserves_detector_dark_identity_across_products(self):
        result = run(self._shifted_two_pixel_data())
        by_product = {p["product_id"]: p for p in result["products"]}
        self.assertEqual(by_product["p0"]["frame_ids"], ["f0"])
        self.assertEqual(by_product["p1"]["frame_ids"], ["f1"])
        self.assertEqual(by_product["p0"]["uncertainty"]["aperture_variances"],
                         [{"aperture_id": "a0", "variance": "1"}])
        self.assertEqual(by_product["p1"]["uncertainty"]["aperture_variances"],
                         [{"aperture_id": "a1", "variance": "1"}])
        self.assertEqual(result["measurement_order"],
                         [{"product_id": "p0", "aperture_id": "a0"},
                          {"product_id": "p1", "aperture_id": "a1"}])
        self.assertEqual(result["covariance"], [["1", "1"], ["1", "1"]])

    def test_distinct_sources_have_zero_covariance_within_product(self):
        data = {
            "width": 2, "height": 1, "dark_exposure_s": 1,
            "dark": [[0, 0]], "flat": [[1, 1]], "bad_mask": [[False, False]],
            "frames": [{"id": "f", "exposure_s": 1, "dx": 0, "dy": 0,
                        "saturation": 100, "pixels": [[0, 0]],
                        "observed_at_s": 0, "variance": [[2, 3]]}],
            "apertures": [{"id": "a0", "x": 0, "y": 0, "radius": 0},
                          {"id": "a1", "x": 1, "y": 0, "radius": 0}],
            "campaign": {"dark_variance": [[1, 4]],
                         "products": [product(
                             "p", max_frames=1,
                             requirements=[requirement("a0"), requirement("a1")])]}}
        result = run(data)
        self.assertEqual(result["measurement_order"],
                         [{"product_id": "p", "aperture_id": "a0"},
                          {"product_id": "p", "aperture_id": "a1"}])
        self.assertEqual(result["covariance"], [["3", "0"], ["0", "7"]])

    def test_overlapping_apertures_share_raw_and_dark_pixels(self):
        data = {
            "width": 3, "height": 3, "dark_exposure_s": 1,
            "dark": [[0] * 3 for _ in range(3)],
            "flat": [[1] * 3 for _ in range(3)],
            "bad_mask": [[False] * 3 for _ in range(3)],
            "frames": [{"id": "f", "exposure_s": 1, "dx": 0, "dy": 0,
                        "saturation": 100, "pixels": [[0] * 3 for _ in range(3)],
                        "observed_at_s": 0,
                        "variance": [[2, 3, 5], [7, 11, 13], [17, 19, 23]]}],
            "apertures": [{"id": "small", "x": 0, "y": 1, "radius": 0},
                          {"id": "big", "x": 1, "y": 1, "radius": 1}],
            "campaign": {
                "dark_variance": [[1, 4, 7], [2, 8, 14], [3, 12, 21]],
                "products": [product(
                    "p", requirements=[requirement("small", max_variance="10000"),
                                       requirement("big", max_variance="10000")])]}}
        result = run(data)
        self.assertEqual(result["measurement_order"],
                         [{"product_id": "p", "aperture_id": "big"},
                          {"product_id": "p", "aperture_id": "small"}])
        self.assertEqual(result["covariance"], [["93", "9"], ["9", "9"]])

    def test_covariance_matrix_is_symmetric_with_matching_diagonal(self):
        result = run(self._shifted_two_pixel_data())
        size = len(result["measurement_order"])
        for i in range(size):
            for j in range(size):
                self.assertEqual(Fraction(result["covariance"][i][j]),
                                 Fraction(result["covariance"][j][i]))
            entry = result["measurement_order"][i]
            product = next(p for p in result["products"]
                           if p["product_id"] == entry["product_id"])
            variance = next(v["variance"] for v in product["uncertainty"]["aperture_variances"]
                            if v["aperture_id"] == entry["aperture_id"])
            self.assertEqual(result["covariance"][i][i], variance)


class AllocationTests(unittest.TestCase):
    def _overlapping_windows(self):
        return campaign_1x1(
            [frame("a", value=10, variance=0, observed_at=1),
             frame("b", value=20, variance=0, observed_at=2)],
            [product("wide", start=0, end=3,
                     requirements=[requirement(max_variance="0")]),
             product("narrow", start=0, end=2,
                     requirements=[requirement(max_variance="0")])],
            dark_variance=((0,),))

    def test_overlapping_windows_deliver_both_without_frame_reuse(self):
        result = run(self._overlapping_windows())
        assigned = {p["product_id"]: p["frame_ids"] for p in result["products"]}
        self.assertEqual(assigned, {"narrow": ["a"], "wide": ["b"]})
        rates = {p["product_id"]: p["stack"]["rate_image"][0][0]
                 for p in result["products"]}
        self.assertEqual(rates, {"narrow": "10", "wide": "20"})
        self.assertEqual(result["summary"]["delivered_products"], 2)
        self.assertEqual(result["summary"]["total_priority"], 2)
        self.assertEqual(result["unused_frame_ids"], [])

    def test_assignment_stable_under_input_permutations(self):
        baseline = run(self._overlapping_windows())
        expected = {p["product_id"]: p["frame_ids"] for p in baseline["products"]}
        for frame_order in (["a", "b"], ["b", "a"]):
            for product_order in (["wide", "narrow"], ["narrow", "wide"]):
                data = self._overlapping_windows()
                frames_by_id = {f["id"]: f for f in data["frames"]}
                data["frames"] = [frames_by_id[fid] for fid in frame_order]
                products_by_id = {p["id"]: p for p in data["campaign"]["products"]}
                data["campaign"]["products"] = [products_by_id[pid]
                                                 for pid in product_order]
                result = run(data)
                assigned = {p["product_id"]: p["frame_ids"] for p in result["products"]}
                self.assertEqual(assigned, expected)

    def test_priority_outranks_delivered_count(self):
        data = self._overlapping_windows()
        data["frames"] = [data["frames"][0]]  # only frame a fits both windows
        for product in data["campaign"]["products"]:
            product["priority"] = 9 if product["id"] == "narrow" else 1
        result = run(data)
        self.assertEqual([p["product_id"] for p in result["products"]], ["narrow"])
        self.assertEqual(result["summary"]["total_priority"], 9)
        self.assertEqual(result["summary"]["delivered_products"], 1)
        self.assertEqual(result["unfulfilled_product_ids"], ["wide"])

    def test_exposure_cost_outranks_signature(self):
        data = campaign_1x1(
            [frame("a", variance=0, observed_at=1, exposure=2),
             frame("b", variance=0, observed_at=2, exposure=1)],
            [product("p", end=3,
                     requirements=[requirement(max_variance="0")])],
            dark_variance=((0,),))
        result = run(data)
        # b is the lexicographically larger id but the cheaper frame.
        self.assertEqual(result["products"][0]["frame_ids"], ["b"])
        self.assertEqual(result["summary"]["used_exposure_s"], 1)

    def test_signature_breaks_final_tie(self):
        data = campaign_1x1(
            [frame("a", variance=0, observed_at=1),
             frame("b", variance=0, observed_at=2)],
            [product("narrow", end=3, requirements=[requirement(max_variance="0")]),
             product("wide", end=3, requirements=[requirement(max_variance="0")])],
            dark_variance=((0,),))
        result = run(data)
        assigned = {p["product_id"]: p["frame_ids"] for p in result["products"]}
        # First signature slot (narrow) takes the smallest frame id.
        self.assertEqual(assigned, {"narrow": ["a"], "wide": ["b"]})

    def test_empty_assignment_when_nothing_deliverable(self):
        data = campaign_1x1([frame("a", observed_at=99)],
                            [product("p", start=0, end=5)])
        result = run(data)
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["products"], [])
        self.assertEqual(result["measurement_order"], [])
        self.assertEqual(result["covariance"], [])
        self.assertEqual(result["unfulfilled_product_ids"], ["p"])
        self.assertEqual(result["unused_frame_ids"], ["a"])
        self.assertEqual(result["summary"],
                         {"requested_products": 1, "delivered_products": 0,
                          "total_priority": 0, "used_exposure_s": 0,
                          "used_frames": 0})


class DeliverabilityAndStackTests(unittest.TestCase):
    def test_exact_variance_threshold_is_accepted(self):
        data = campaign_1x1(
            [frame("a", variance=1)],
            [product("p", requirements=[requirement(max_variance="2")])],
            dark_variance=((1,),))
        self.assertEqual(run(data)["summary"]["delivered_products"], 1)
        data["campaign"]["products"][0]["requirements"][0]["max_variance"] = "4/2"
        self.assertEqual(run(data)["summary"]["delivered_products"], 1)
        data["campaign"]["products"][0]["requirements"][0]["max_variance"] = "1"
        self.assertEqual(run(data)["summary"]["delivered_products"], 0)

    def test_negative_signal_is_preserved(self):
        data = campaign_1x1(
            [frame("a", value=0, variance=0)],
            [product("p", requirements=[requirement(max_variance="0")])],
            dark=((5,),), dark_variance=((0,),))
        result = run(data)
        self.assertEqual(result["products"][0]["stack"]["rate_image"], [["-5"]])

    def test_saturation_blocks_delivery_signal_and_noise(self):
        data = campaign_1x1([frame("a", saturation=1)], [product("p")])
        result = run(data)
        self.assertEqual(result["products"], [])
        self.assertEqual(result["unused_frame_ids"], ["a"])

    def test_shift_outside_frame_blocks_delivery(self):
        data = campaign_1x1([frame("a", dx=1)], [product("p")])
        self.assertEqual(run(data)["summary"]["delivered_products"], 0)

    def test_local_exposure_shortfall_blocks_delivery(self):
        data = campaign_1x1(
            [frame("a", exposure=1)],
            [product("p", requirements=[requirement(min_exposure_s=2)])])
        self.assertEqual(run(data)["summary"]["delivered_products"], 0)
        # a second frame reaching the same output pixel satisfies the per-pixel lower bound
        data["frames"].append(frame("b", observed_at=1))
        data["campaign"]["products"][0]["max_frames"] = 2
        self.assertEqual(run(data)["summary"]["delivered_products"], 1)

    def test_aperture_crossing_frame_boundary_is_undeliverable(self):
        data = campaign_1x1(
            [frame("a")], [product("p")],
            apertures=[{"id": "a", "x": 0, "y": 0, "radius": 1}])
        result = run(data)
        self.assertEqual(result["unfulfilled_product_ids"], ["p"])

    def test_mask_and_zero_flat_pixel_keeps_null_and_excludes_noise(self):
        data = {
            "width": 2, "height": 1, "dark_exposure_s": 1,
            "dark": [[0, 0]], "flat": [[0, 1]], "bad_mask": [[True, False]],
            "frames": [{"id": "f", "exposure_s": 1, "dx": 0, "dy": 0,
                        "saturation": 100, "pixels": [[10, 20]],
                        "observed_at_s": 0, "variance": [[1, 1]]}],
            "apertures": [{"id": "dead", "x": 0, "y": 0, "radius": 0},
                          {"id": "live", "x": 1, "y": 0, "radius": 0}],
            "campaign": {"dark_variance": [[1, 1]],
                         "products": [
                             product("p", requirements=[requirement("dead")]),
                             product("q", requirements=[requirement("live")])]}}
        result = run(data)
        self.assertEqual(result["unfulfilled_product_ids"], ["p"])
        delivered = next(p for p in result["products"] if p["product_id"] == "q")
        self.assertEqual(delivered["stack"]["rate_image"], [[None, "20"]])
        self.assertEqual(delivered["uncertainty"]["variance_image"], [[None, "2"]])
        audit = delivered["stack"]["frame_audit"][0]
        self.assertEqual(audit["masked_pixels"], 1)
        self.assertEqual(audit["zero_flat_pixels"], 0)

    def test_high_exposure_frame_can_worsen_aperture_variance(self):
        data = campaign_1x1(
            [frame("f", variance=1, observed_at=0),
             frame("g", variance=1_000_000, observed_at=1, exposure=10)],
            [product("p", max_frames=2, requirements=[requirement(max_variance="100")])],
            dark_variance=((1,),))
        result = run(data)
        self.assertEqual(result["products"][0]["frame_ids"], ["f"])

    def test_non_required_aperture_remains_measured_even_when_incomplete(self):
        data = campaign_1x1(
            [frame("a")], [product("p")],
            apertures=[{"id": "a", "x": 0, "y": 0, "radius": 0},
                       {"id": "edge", "x": 0, "y": 0, "radius": 1}])
        result = run(data)
        delivered = result["products"][0]
        ids = [a["aperture_id"] for a in delivered["stack"]["apertures"]]
        self.assertEqual(ids, ["a", "edge"])
        edge = next(a for a in delivered["stack"]["apertures"]
                    if a["aperture_id"] == "edge")
        self.assertFalse(edge["complete"])
        self.assertEqual(edge["outside_pixels"], 4)
        self.assertEqual(edge["sum_rate"], "10")
        self.assertEqual(
            [v["aperture_id"] for v in delivered["uncertainty"]["aperture_variances"]],
            ["a"])

    def test_non_required_aperture_without_contribution_reports_null(self):
        data = {
            "width": 2, "height": 1, "dark_exposure_s": 1,
            "dark": [[0, 0]], "flat": [[1, 1]], "bad_mask": [[False, True]],
            "frames": [{"id": "f", "exposure_s": 1, "dx": 0, "dy": 0,
                        "saturation": 100, "pixels": [[10, 20]],
                        "observed_at_s": 0, "variance": [[1, 1]]}],
            "apertures": [{"id": "live", "x": 0, "y": 0, "radius": 0},
                          {"id": "null", "x": 1, "y": 0, "radius": 0}],
            "campaign": {"dark_variance": [[1, 1]],
                         "products": [product(
                             "p", requirements=[requirement("live")])]}}
        result = run(data)
        delivered = result["products"][0]
        null_aperture = next(a for a in delivered["stack"]["apertures"]
                             if a["aperture_id"] == "null")
        self.assertIsNone(null_aperture["sum_rate"])
        self.assertEqual(null_aperture["missing_pixels"], 1)
        self.assertEqual(delivered["stack"]["rate_image"], [["10", None]])
        self.assertEqual(delivered["uncertainty"]["variance_image"], [["2", None]])

    def test_stack_summary_reflects_only_assigned_frames(self):
        data = campaign_1x1(
            [frame("a", observed_at=0), frame("b", observed_at=1)],
            [product("p", max_frames=2, requirements=[requirement(max_variance="100")])],
            dark_variance=((1,),))
        result = run(data)
        delivered = result["products"][0]
        self.assertEqual(delivered["frame_ids"], ["a"])
        self.assertEqual(delivered["stack"]["summary"]["frames"], 1)
        self.assertEqual([audit["frame_id"] for audit in delivered["stack"]["frame_audit"]],
                         ["a"])


class ValidationTests(unittest.TestCase):
    def _valid(self):
        return campaign_1x1([frame("a")], [product("p")])

    def assert_rejects(self, data):
        with self.assertRaises(ValueError):
            run(data)

    def test_extra_frame_fields_rejected_without_campaign(self):
        legacy = json.loads(Path(ROOT, "fixtures", "input.json").read_text())
        legacy["frames"][0]["observed_at_s"] = 0
        self.assert_rejects(legacy)
        legacy2 = json.loads(Path(ROOT, "fixtures", "input.json").read_text())
        legacy2["frames"][0]["variance"] = [[0]]
        self.assert_rejects(legacy2)

    def test_campaign_field_shape_is_strict(self):
        data = self._valid()
        data["campaign"]["extra"] = 1
        self.assert_rejects(data)
        data = self._valid()
        del data["campaign"]["products"]
        self.assert_rejects(data)
        data = self._valid()
        data["campaign"]["products"][0]["extra"] = 1
        self.assert_rejects(data)
        data = self._valid()
        del data["campaign"]["products"][0]["priority"]
        self.assert_rejects(data)
        data = self._valid()
        data["campaign"]["products"][0]["requirements"][0]["extra"] = 1
        self.assert_rejects(data)

    def test_frame_limit_and_required_fields(self):
        data = self._valid()
        data["frames"] = [frame(f"f{i}", observed_at=i) for i in range(15)]
        self.assert_rejects(data)
        data = self._valid()
        del data["frames"][0]["variance"]
        self.assert_rejects(data)
        data = self._valid()
        del data["frames"][0]["observed_at_s"]
        self.assert_rejects(data)

    def test_value_ranges_and_booleans(self):
        data = self._valid()
        data["frames"][0]["observed_at_s"] = -1
        self.assert_rejects(data)
        data = self._valid()
        data["frames"][0]["variance"] = [[True]]
        self.assert_rejects(data)
        data = self._valid()
        data["campaign"]["dark_variance"] = [[-1]]
        self.assert_rejects(data)
        data = self._valid()
        data["campaign"]["products"][0]["start_s"] = 5
        data["campaign"]["products"][0]["end_s"] = 5
        self.assert_rejects(data)
        data = self._valid()
        data["campaign"]["products"][0]["priority"] = 0
        self.assert_rejects(data)
        data = self._valid()
        data["campaign"]["products"][0]["max_frames"] = 5
        self.assert_rejects(data)

    def test_requirement_references_and_duplicates(self):
        data = self._valid()
        data["campaign"]["products"][0]["requirements"][0]["aperture_id"] = "missing"
        self.assert_rejects(data)
        data = self._valid()
        req = data["campaign"]["products"][0]["requirements"][0]
        data["campaign"]["products"][0]["requirements"].append(copy.deepcopy(req))
        self.assert_rejects(data)

    def test_rational_strings(self):
        for bad in ("", "1.5", "-1", "1/ 2", "01", "1/0", "+1", "10000000000000",
                    "0/1 ", "1/-2", "1.0", " 1", "1/"):
            data = self._valid()
            data["campaign"]["products"][0]["requirements"][0]["max_variance"] = bad
            self.assert_rejects(data)
        for good in ("0", "1000000000000", "1/1000000000000", "4/2", "12/8",
                     "2/1000000000000"):
            data = self._valid()
            data["campaign"]["products"][0]["requirements"][0]["max_variance"] = good
            run(data)

    def test_duplicate_product_ids_and_json_keys_rejected(self):
        data = self._valid()
        data["campaign"]["products"].append(product("p"))
        self.assert_rejects(data)
        text = json.dumps(self._valid()).replace(
            '"variance"', '"variance": 1, "variance"', 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "bad.json")
            path.write_text(text)
            from astro_stack.io import load
            with self.assertRaises(ValueError):
                run(load(path))

    def test_unknown_campaign_combination_is_success_not_error(self):
        data = self._valid()
        data["frames"][0]["observed_at_s"] = 99  # outside the window
        result = run(data)
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["summary"]["delivered_products"], 0)


class FixtureTests(unittest.TestCase):
    def test_campaign_fixture_is_reproducible(self):
        source = Path(ROOT, "fixtures", "campaign.json")
        result = run(json.loads(source.read_text()))
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["summary"],
                         {"requested_products": 2, "delivered_products": 2,
                          "total_priority": 6, "used_exposure_s": 6,
                          "used_frames": 2})
        assigned = {p["product_id"]: p["frame_ids"] for p in result["products"]}
        self.assertEqual(assigned, {"deep": ["exp-late"],
                                    "snapshot": ["exp-early"]})
        # shared dark source produces a nonzero cross-product covariance
        self.assertEqual(result["covariance"],
                         [["5/2", "2"], ["2", "3"]])
        # the non-required, partially out-of-frame aperture is still measured
        snapshot = next(p for p in result["products"]
                        if p["product_id"] == "snapshot")
        field = next(a for a in snapshot["stack"]["apertures"]
                     if a["aperture_id"] == "field")
        self.assertFalse(field["complete"])
        self.assertEqual(field["outside_pixels"], 2)


class ReportShapeTests(unittest.TestCase):
    def test_report_top_level_and_ordering_contract(self):
        data = campaign_1x1(
            [frame("a", observed_at=0), frame("b", observed_at=1)],
            [product("zeta", end=3, max_frames=2,
                     requirements=[requirement(max_variance="100")]),
             product("alpha", end=3,
                     requirements=[requirement(max_variance="100")])],
            dark_variance=((1,),))
        result = run(data)
        self.assertEqual(set(result),
                         {"schema_version", "products", "measurement_order",
                          "covariance", "unfulfilled_product_ids",
                          "unused_frame_ids", "summary"})
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual([p["product_id"] for p in result["products"]],
                         ["alpha", "zeta"])
        for entry in result["products"]:
            self.assertEqual(set(entry),
                             {"product_id", "frame_ids", "stack", "uncertainty"})
            self.assertEqual(set(entry["uncertainty"]),
                             {"variance_image", "aperture_variances"})
        ordered = [(e["product_id"], e["aperture_id"])
                   for e in result["measurement_order"]]
        self.assertEqual(ordered, sorted(ordered))
        self.assertEqual(len(result["covariance"]), len(ordered))
        self.assertTrue(all(len(row) == len(ordered) for row in result["covariance"]))


class CliTests(unittest.TestCase):
    def run_cli(self, source, output):
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory, "astro_stack_launcher.py")
            launcher.write_text(
                "import sys\n"
                f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
                "from astro_stack.cli import main\n"
                "main()\n")
            return subprocess.run(
                [sys.executable, "-I", str(launcher),
                 "--source", str(source), "--output", str(output)],
                cwd=ROOT, capture_output=True, text=True)

    def test_cli_publishes_campaign_report_and_prints_summary(self):
        data = campaign_1x1(
            [frame("a", variance=1, observed_at=0), frame("b", observed_at=1)],
            [product("p", max_frames=2, requirements=[requirement(max_variance="1")])],
            dark_variance=((0,),))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "input.json")
            output = Path(directory, "nested", "report.json")
            source.write_text(json.dumps(data))
            process = self.run_cli(source, output)
            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report["schema_version"], 2)
            self.assertEqual(json.loads(process.stdout.strip()), report["summary"])

    def test_cli_legacy_mode_unchanged_and_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(ROOT, "fixtures", "input.json")
            output = Path(directory, "report.json")
            process = self.run_cli(source, output)
            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report["schema_version"], 1)
            before = output.read_text()
            bad = Path(directory, "bad.json")
            bad.write_text('{"width": 1}')
            failed = self.run_cli(bad, output)
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(output.read_text(), before)
            # an existing target blocks even a valid rerun
            blocked = self.run_cli(source, output)
            self.assertEqual(blocked.returncode, 2)
            self.assertEqual(output.read_text(), before)


if __name__ == "__main__":
    unittest.main()
