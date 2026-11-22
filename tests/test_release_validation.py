"""Strict request/license validation and offline verifier rejection cases."""
import json
import unittest
from pathlib import Path

from astro_stack.release import export, issue, verify
from astro_stack.release.package import (PackageError, canonical_bytes,
                                         sha256_hex)
from astro_stack.release.validation import (MAX_DENIED, validate_license,
                                            validate_request)

from release_helpers import (TempPaths, build_two_by_one_archive,
                             full_request, license_with)


class RequestValidationTests(unittest.TestCase):

    def base(self):
        return full_request("r1")

    def test_accepts_canonical_request(self):
        parsed = validate_request(self.base())
        self.assertEqual(parsed["region"],
                         {"x0": 0, "y0": 0, "x1": 2, "y1": 1})

    def test_rejects_unknown_and_missing_fields(self):
        for name, value in self.base().items():
            broken = dict(self.base())
            del broken[name]
            with self.assertRaises(ValueError):
                validate_request(broken)
        broken = dict(self.base(), extra=1)
        with self.assertRaises(ValueError):
            validate_request(broken)
        broken = dict(self.base(), region=dict(self.base()["region"], z=0))
        with self.assertRaises(ValueError):
            validate_request(broken)

    def test_rejects_empty_rectangle_and_bad_bounds(self):
        for region in (
                {"x0": 1, "y0": 0, "x1": 1, "y1": 1},
                {"x0": 0, "y0": 1, "x1": 2, "y1": 1},
                {"x0": -1, "y0": 0, "x1": 2, "y1": 1},
                {"x0": 1, "y0": 0, "x1": 0, "y1": 1}):
            with self.assertRaises(ValueError):
                validate_request(dict(self.base(), region=region))

    def test_rejects_booleans_floats_and_oversized_integers(self):
        broken = dict(self.base(), revision=True)
        with self.assertRaises(ValueError):
            validate_request(broken)
        broken = dict(self.base(), revision=1.0)
        with self.assertRaises(ValueError):
            validate_request(broken)
        broken = dict(self.base(), revision=10**12 + 1)
        with self.assertRaises(ValueError):
            validate_request(broken)
        broken = dict(self.base(), region=dict(self.base()["region"], x0=-1))
        with self.assertRaises(ValueError):
            validate_request(broken)

    def test_revision_zero_is_allowed_by_validation(self):
        self.assertEqual(validate_request(
            dict(self.base(), revision=0))["revision"], 0)

    def test_rejects_blank_or_oversized_ids(self):
        for request_id in ("", "  ", "x" * 201):
            with self.assertRaises(ValueError):
                validate_request(dict(self.base(), request_id=request_id))


class LicenseValidationTests(unittest.TestCase):

    def test_accepts_empty_and_populated_licenses(self):
        self.assertEqual(validate_license(
            {"dataset_id": "ds", "denied": []})["denied"], set())
        parsed = validate_license(license_with([("G", 1, 0, 0)]))
        self.assertEqual(parsed["denied"], {("G", 1, 0, 0)})

    def test_rejects_duplicate_identity_and_bad_fields(self):
        value = license_with([("G", 1, 0, 0), ("G", 1, 0, 0)])
        with self.assertRaises(ValueError):
            validate_license(value)
        broken = dict(value, extra=1)
        with self.assertRaises(ValueError):
            validate_license(broken)
        broken = license_with([("G", 0, 0, 0)])
        with self.assertRaises(ValueError):
            validate_license(broken)
        broken = license_with([("G", 1, True, 0)])
        with self.assertRaises(ValueError):
            validate_license(broken)

    def test_rejects_more_than_50_000_denied_rows(self):
        value = {"dataset_id": "ds",
                 "denied": [{"frame_id": "F", "frame_version": 1,
                             "x": i % 2, "y": 0}
                            for i in range(MAX_DENIED + 1)]}
        with self.assertRaises(ValueError):
            validate_license(value)

    def test_identities_match_exact_frame_version_only(self):
        # version 2 denial must not match version 1 samples
        parsed = validate_license(license_with([("G", 2, 0, 0)]))
        self.assertNotIn(("G", 1, 0, 0), parsed["denied"])


class StrictJsonFileTests(unittest.TestCase):

    def setUp(self):
        self.paths = TempPaths()
        self.addCleanup(self.paths.close)
        build_two_by_one_archive(self.paths.archive)

    def _write_issue(self, name, text):
        path = self.paths.root / name
        path.write_text(text)
        return path

    def test_duplicate_json_keys_rejected(self):
        path = self._write_issue(
            "dup.json",
            '{"request_id": "r", "request_id": "s", "dataset_id": "ds-2x1", '
            '"revision": 1, "region": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}}')
        with self.assertRaises(ValueError):
            issue(str(self.paths.library), str(self.paths.archive), path,
                  license_with([]))

    def test_non_finite_constants_rejected(self):
        path = self._write_issue(
            "nan.json",
            '{"request_id": "r", "dataset_id": "ds-2x1", "revision": 1, '
            '"region": {"x0": 0, "y0": 0, "x1": NaN, "y1": 1}}')
        with self.assertRaises(ValueError):
            issue(str(self.paths.library), str(self.paths.archive), path,
                  license_with([]))

    def test_inputs_over_8mb_rejected(self):
        big = {"dataset_id": "ds",
               "denied": [{"frame_id": "x" * 100, "frame_version": 1,
                           "x": 0, "y": 0}]}
        path = self.paths.root / "big.json"
        path.write_text(json.dumps(big) + " " * (8_000_001))
        with self.assertRaises(ValueError):
            issue(str(self.paths.library), str(self.paths.archive),
                  full_request("big"), path)


class ServiceValidationTests(unittest.TestCase):

    def setUp(self):
        self.paths = TempPaths()
        self.addCleanup(self.paths.close)
        build_two_by_one_archive(self.paths.archive)

    def test_dataset_mismatch_and_unknown_revision_rejected(self):
        with self.assertRaises(ValueError):
            issue(str(self.paths.library), str(self.paths.archive),
                  full_request("r", dataset_id="other"), license_with([]))
        with self.assertRaises(ValueError):
            issue(str(self.paths.library), str(self.paths.archive),
                  full_request("r", revision=99), license_with([]))

    def test_denied_coordinates_outside_detector_rejected(self):
        with self.assertRaises(ValueError):
            issue(str(self.paths.library), str(self.paths.archive),
                  full_request("r"), license_with([("F", 1, 2, 0)]))
        with self.assertRaises(ValueError):
            issue(str(self.paths.library), str(self.paths.archive),
                  full_request("r"), license_with([("F", 1, 0, 7)]))

    def test_region_outside_frame_rejected_at_issue(self):
        with self.assertRaises(ValueError):
            issue(str(self.paths.library), str(self.paths.archive),
                  full_request("r",
                               region={"x0": 0, "y0": 0, "x1": 2, "y1": 2}),
                  license_with([]))

    def test_campaign_archives_are_rejected(self):
        from archive_helpers import (batch as make_batch, campaign_definition,
                                     dark, flat, frame, release)
        from astro_stack.archive import apply_batch, initialize
        archive = self.paths.root / "campaign"
        definition = campaign_definition(dataset_id="ds-2x1")
        initialize(str(archive), definition)
        apply_batch(str(archive), make_batch(
            "b1", 0, add_darks=[dark("d0")], add_flats=[flat()],
            add_releases=[release("r1", 1)], put_frames=[frame("fr", 0)]))
        with self.assertRaisesRegex(ValueError, "classic archives only"):
            issue(str(self.paths.root / "lib-campaign"), str(archive),
                  full_request("r"), license_with([]))

    def test_license_dataset_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            issue(str(self.paths.library), str(self.paths.archive),
                  full_request("r"), license_with([], "other"))

    def test_license_replacement_only_affects_new_requests(self):
        first = issue(str(self.paths.library), str(self.paths.archive),
                      full_request("same-license-request"), license_with([]))
        second = issue(
            str(self.paths.library), str(self.paths.archive),
            full_request("same-license-request"),
            license_with([("G", 1, 0, 0)]))
        self.assertEqual(first, second)

    def test_stale_and_future_frame_versions_never_match(self):
        license_value = license_with([
            ("H", 1, 0, 0), ("G", 99, 0, 0), ("F", 7, 1, 0)])
        receipt = issue(str(self.paths.root / "lib-stale"),
                        str(self.paths.archive),
                        full_request("stale"), license_value)
        output = self.paths.root / "stale.json"
        export(str(self.paths.root / "lib-stale"), "stale", output)
        document = json.loads(output.read_text())
        self.assertEqual(
            [cell["status"] for cell in document["cells"]],
            ["published", "published"])
        self.assertEqual(document["region"]["rate_sum"], "35")
        verify(str(output), receipt["sha256"])


class VerifierRejectionTests(unittest.TestCase):

    def setUp(self):
        self.paths = TempPaths()
        self.addCleanup(self.paths.close)
        build_two_by_one_archive(self.paths.archive)
        receipt = issue(str(self.paths.library), str(self.paths.archive),
                        full_request("v"), license_with([]))
        self.output = self.paths.root / "v.json"
        export(str(self.paths.library), "v", self.output)
        self.digest = receipt["sha256"]

    def _repackage(self, mutate):
        document = json.loads(self.output.read_text())
        mutate(document)
        path = self.paths.root / f"mut-{id(document)}.json"
        path.write_bytes(canonical_bytes(document))
        return path

    def test_wrong_digest_shape_rejected_before_opening_package(self):
        for bad in ("abc", "F" * 64, "0" * 63, "g" * 64):
            with self.assertRaises((ValueError, PackageError)):
                verify(str(self.output), bad)

    def test_duplicate_package_json_keys_rejected(self):
        raw = self.output.read_bytes()
        text = raw.decode("utf-8")
        # duplicate a top-level key with the same scalar value
        text = text.replace('"format":', '"format": "x", "format":', 1)
        path = self.paths.root / "dup-package.json"
        path.write_text(text)
        with self.assertRaises(ValueError):
            verify(str(path), sha256_hex(path.read_bytes()))

    def test_non_finite_package_constants_rejected(self):
        text = self.output.read_text()
        for constant in ("NaN", "Infinity", "-Infinity"):
            mutated = text.replace('"rate":"10"', f'"rate":{constant}', 1)
            path = self.paths.root / f"const-{constant}.json"
            path.write_text(mutated)
            with self.assertRaises(PackageError):
                verify(str(path), sha256_hex(path.read_bytes()))

    def test_non_object_package_root_rejected(self):
        path = self.paths.root / "array-package.json"
        path.write_bytes(b"[1,2,3]")
        with self.assertRaises(PackageError):
            verify(str(path), sha256_hex(path.read_bytes()))

    def test_registration_mismatch_rejected(self):
        def mutate(document):
            # G really shifts by +1; claim 0 so the stored output coordinate
            # no longer matches the integer registration
            g = next(frame for frame in document["frames"]
                     if frame["frame_id"] == "G")
            g["dx"] = 0
        path = self._repackage(mutate)
        with self.assertRaisesRegex(PackageError, "registration"):
            verify(str(path), sha256_hex(path.read_bytes()))

    def test_coordinate_out_of_bounds_rejected(self):
        def mutate(document):
            document["frames"][0]["samples"][0]["x"] = 99
        path = self._repackage(mutate)
        with self.assertRaisesRegex(PackageError, "outside the detector"):
            verify(str(path), sha256_hex(path.read_bytes()))

    def test_dark_identity_contradiction_rejected(self):
        def mutate(document):
            document["darks"][0]["samples"][0]["dark_variance"] = 99
        path = self._repackage(mutate)
        with self.assertRaises(PackageError):
            verify(str(path), sha256_hex(path.read_bytes()))

    def test_unreferenced_table_entry_rejected(self):
        def mutate(document):
            # a second dark with no contribution pointing at it
            document["darks"].append(dict(document["darks"][0], id="extra"))
        path = self._repackage(mutate)
        with self.assertRaisesRegex(PackageError, "bound to a published cell"):
            verify(str(path), sha256_hex(path.read_bytes()))

    def test_cell_ordering_must_be_row_major_absolute(self):
        def mutate(document):
            document["cells"].reverse()
        path = self._repackage(mutate)
        with self.assertRaisesRegex(PackageError, "row-major"):
            verify(str(path), sha256_hex(path.read_bytes()))

    def test_rate_mismatch_rejected(self):
        def mutate(document):
            document["cells"][0]["rate"] = "11"
        path = self._repackage(mutate)
        with self.assertRaisesRegex(PackageError, "rate mismatch"):
            verify(str(path), sha256_hex(path.read_bytes()))

    def test_status_counts_mismatch_rejected(self):
        def mutate(document):
            document["region"]["counts"]["published"] = 9
        path = self._repackage(mutate)
        with self.assertRaisesRegex(PackageError, "status counts"):
            verify(str(path), sha256_hex(path.read_bytes()))

    def test_unknown_format_rejected(self):
        def mutate(document):
            document["format"] = "astro-release-package/v2"
        path = self._repackage(mutate)
        with self.assertRaisesRegex(PackageError, "unknown package format"):
            verify(str(path), sha256_hex(path.read_bytes()))

    def test_unreduced_fraction_string_rejected(self):
        def mutate(document):
            document["cells"][0]["rate"] = "20/2"
        path = self._repackage(mutate)
        with self.assertRaises(PackageError):
            verify(str(path), sha256_hex(path.read_bytes()))


if __name__ == "__main__":
    unittest.main()
