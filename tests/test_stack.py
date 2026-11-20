import copy
import json
import unittest
from pathlib import Path
from astro_stack.service import run


def fixture():
    return json.loads(Path("fixtures/input.json").read_text())


class StackTests(unittest.TestCase):
    def test_calibration_shift_exposure_and_aperture(self):
        result = run(fixture())
        self.assertEqual(result["rate_image"], [["10", "40/3", "70/3"], ["40", "130/3", "60"]])
        self.assertEqual(result["exposure_s"], [[1, 3, 3], [1, 3, 1]])
        self.assertEqual(result["summary"], {"frames": 2, "covered_pixels": 6,
                                           "accepted_contributions": 9, "complete_apertures": 1})
        star = next(a for a in result["apertures"] if a["aperture_id"] == "star")
        self.assertEqual(star["sum_rate"], "40/3")
        edge = result["apertures"][0]
        self.assertFalse(edge["complete"])
        self.assertEqual(edge["outside_pixels"], 2)

    def test_zero_flat_and_mask_remove_detector_not_output_coordinates(self):
        data = fixture()
        data["flat"][0][0] = 0
        data["bad_mask"][1][0] = True
        result = run(data)
        self.assertIsNone(result["rate_image"][0][0])
        self.assertIsNone(result["rate_image"][1][0])
        self.assertEqual(result["frame_audit"][0]["zero_flat_pixels"], 1)
        self.assertEqual(result["frame_audit"][1]["masked_pixels"], 1)

    def test_flat_normalization_and_negative_signal(self):
        data = fixture()
        data["frames"] = [data["frames"][0]]
        data["flat"] = [[5, 10, 15], [5, 10, 15]]
        data["frames"][0]["pixels"][0][0] = 1
        result = run(data)
        self.assertEqual(result["rate_image"][0][0], "-2")
        self.assertEqual(result["rate_image"][0][2], "20")

    def test_all_saturated_has_null_photometry(self):
        data = fixture()
        for frame in data["frames"]:
            frame["saturation"] = 1
        result = run(data)
        self.assertEqual(result["summary"]["covered_pixels"], 0)
        self.assertIsNone(result["apertures"][1]["sum_rate"])
        self.assertEqual(result["apertures"][1]["missing_pixels"], 1)

    def test_malformed_shape_and_duplicate_frames_reject(self):
        data = fixture()
        data["frames"][0]["pixels"][0].pop()
        with self.assertRaises(ValueError):
            run(data)
        data = fixture()
        data["frames"].append(copy.deepcopy(data["frames"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            run(data)
