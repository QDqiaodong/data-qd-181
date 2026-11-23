"""Tests for the schema_version=1 sub-pixel survey mosaic and its CLI.

Every expected number is derived by hand from the contract: the local rate at
a point is the exposure-weighted mean of the calibrated rates covering it,
qualified area needs ``min_visits`` distinct visits, and raw/dark noise
sources keep their archive identities prefixed with the dataset id.  The
geometries deliberately distinguish the correct per-point local denominator
from tempting wrong implementations (cell-level single division, counting
one visit's frames as several visits, sharing dark noise across archives,
clipping to the old integer canvas, or dense empty-sky scans).
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from astro_stack.archive import apply_batch, initialize
from astro_stack.archive import render as archive_render
from astro_stack.mosaic import MosaicError, render

from archive_helpers import TempArchive, write_json

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


# --------------------------------------------------------------------------
# Archive construction helpers
# --------------------------------------------------------------------------
def dark_record(dark_id="d0", values=None, variances=None, exposure=1,
                width=1, height=1):
    if values is None:
        values = [[0 for _ in range(width)] for _ in range(height)]
    if variances is None:
        variances = [[4 for _ in range(width)] for _ in range(height)]
    return {"id": dark_id, "dark_exposure_s": exposure,
            "dark": values, "dark_variance": variances}


def flat_record(flat_id="f0", values=None, mask=None, width=1, height=1):
    if values is None:
        values = [[1 for _ in range(width)] for _ in range(height)]
    if mask is None:
        mask = [[False for _ in range(width)] for _ in range(height)]
    return {"id": flat_id, "flat": values, "bad_mask": mask}


def release_record(release_id="r1", version=1, dark_id="d0", flat_id="f0",
                   start=0, end=100):
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


def classic_definition(dataset_id, width=1, height=1):
    return {"dataset_id": dataset_id, "width": width, "height": height,
            "apertures": [], "products": None}


def one_pixel_archive(dataset_id, count, *, raw_variance=1, dark_variance=4,
                      exposure=1, dx=0, dy=0, frame_id="f", releases=None,
                      observed_at=5):
    """Classic 1x1 archive at revision 1 with one ready frame."""
    return build_archive(
        classic_definition(dataset_id),
        [batch_record("b1", 0,
                      darks=[dark_record(variances=[[dark_variance]])],
                      flats=[flat_record()],
                      releases=releases or [release_record()],
                      frames=[frame_record(
                          frame_id, [[count]], [[raw_variance]],
                          exposure=exposure, dx=dx, dy=dy,
                          observed_at=observed_at)])])


def source(archive, dataset_id, revision=1, origin_x="0", origin_y="0",
           offsets=None):
    return {"archive": str(archive.path), "dataset_id": dataset_id,
            "revision": revision, "origin_x": origin_x, "origin_y": origin_y,
            "offsets": offsets or []}


def offset(frame_id, frame_version, dx, dy):
    return {"frame_id": frame_id, "frame_version": frame_version,
            "dx": dx, "dy": dy}


def region(region_id, x0, y0, x1, y1):
    return {"id": region_id, "x0": x0, "y0": y0, "x1": x1, "y1": y1}


def cell(report, row, column):
    return report["cells"][row][column]


def region_entry(report, region_id):
    return next(entry for entry in report["regions"]
                if entry["id"] == region_id)


# --------------------------------------------------------------------------
# Acceptance scenario 1: two one-pixel archives half a cell apart
# --------------------------------------------------------------------------
class AcceptanceOneTests(unittest.TestCase):
    """A: count 10 at origin (0,0); B: count 30 at origin (1/2,0).

    Both exposures are 1s with raw variance 1, unit flat, zero dark count and
    dark variance 4 under the same dark_id "d0" -- but the two archives never
    share noise: a shared-dark implementation would report 37/8 where the
    correct left-cell variance is 25/8.
    """

    def setUp(self):
        self.a = one_pixel_archive("A", 10)
        self.b = one_pixel_archive("B", 30)
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)
        self.sources = [source(self.a, "A"), source(self.b, "B",
                                                    origin_x="1/2")]
        self.regions = [region("L", "0", "0", "1", "1"),
                        region("W", "0", "0", "3/2", "1"),
                        region("C", "1/4", "0", "5/4", "1")]

    def spec(self, min_visits=1, regions=None, sources=None):
        return {"sources": sources if sources is not None else self.sources,
                "grid": {"x0": "0", "y0": "0", "width": 2, "height": 1},
                "regions": regions if regions is not None else self.regions,
                "min_visits": min_visits}

    def test_min_visits_one_cells_regions_and_covariance(self):
        report = render(self.spec())
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["min_visits"], 1)
        self.assertEqual(report["sources"],
                         [{"dataset_id": "A", "revision": 1},
                          {"dataset_id": "B", "revision": 1}])
        self.assertEqual(report["grid"],
                         {"x0": "0", "y0": "0", "width": 2, "height": 1})
        # left cell: [0,1/2) only A (rate 10), [1/2,1) A+B (rate 20)
        self.assertEqual(cell(report, 0, 0),
                         {"qualified_area": "1", "status": "complete",
                          "integral": "15", "variance": "25/8"})
        # right cell: only B over [1,3/2)
        self.assertEqual(cell(report, 0, 1),
                         {"qualified_area": "1/2", "status": "partial",
                          "integral": "15", "variance": "5/4"})
        # regions are reported sorted by id: C, L, W
        self.assertEqual([entry["id"] for entry in report["regions"]],
                         ["C", "L", "W"])
        self.assertEqual(region_entry(report, "C"),
                         {"id": "C", "area": "1", "qualified_area": "1",
                          "status": "complete", "integral": "20",
                          "variance": "5/2"})
        self.assertEqual(region_entry(report, "L"),
                         {"id": "L", "area": "1", "qualified_area": "1",
                          "status": "complete", "integral": "15",
                          "variance": "25/8"})
        self.assertEqual(region_entry(report, "W"),
                         {"id": "W", "area": "3/2", "qualified_area": "3/2",
                          "status": "complete", "integral": "30",
                          "variance": "45/8"})
        # hand-derived shared-noise covariances (order C, L, W)
        self.assertEqual(report["region_covariance"],
                         [["5/2", "5/2", "15/4"],
                          ["5/2", "25/8", "15/4"],
                          ["15/4", "15/4", "45/8"]])
        for i, entry in enumerate(report["regions"]):
            self.assertEqual(report["region_covariance"][i][i],
                             entry["variance"])
        self.assertEqual(report["summary"],
                         {"sources": 2, "ready_frames": 2,
                          "accepted_samples": 2, "covered_cells": 2,
                          "complete_cells": 1, "complete_regions": 3})

    def test_min_visits_two_partial_cells_and_overlap_region(self):
        overlap = region("O", "1/2", "0", "1", "1")
        report = render(self.spec(min_visits=2,
                                  regions=self.regions + [overlap]))
        # only the A overlap B strip [1/2,1) reaches two visits
        self.assertEqual(cell(report, 0, 0),
                         {"qualified_area": "1/2", "status": "partial",
                          "integral": "10", "variance": "5/8"})
        self.assertEqual(cell(report, 0, 1),
                         {"qualified_area": "0", "status": "uncovered",
                          "integral": None, "variance": None})
        # L, W, C keep their true qualified area but deliver no integral
        for region_id in ("L", "W", "C"):
            entry = region_entry(report, region_id)
            self.assertEqual(entry["qualified_area"], "1/2")
            self.assertEqual(entry["status"], "partial")
            self.assertIsNone(entry["integral"])
            self.assertIsNone(entry["variance"])
        # the exact-overlap region is fully qualified and delivers
        self.assertEqual(region_entry(report, "O"),
                         {"id": "O", "area": "1/2", "qualified_area": "1/2",
                          "status": "complete", "integral": "10",
                          "variance": "5/8"})
        self.assertEqual([entry["id"] for entry in report["regions"]],
                         ["C", "L", "O", "W"])
        covariance = report["region_covariance"]
        self.assertEqual(len(covariance), 4)
        self.assertEqual(covariance[2][2], "5/8")
        for i in range(4):
            for j in range(4):
                if (i, j) != (2, 2):
                    self.assertIsNone(covariance[i][j])

    def test_same_visit_frames_share_dark_and_count_as_one_visit(self):
        # both exposures live in one archive S; f2 carries the extra half
        # cell offset; D is a far-away source that contributes nothing here
        s = build_archive(
            classic_definition("S"),
            [batch_record("b1", 0, darks=[dark_record()],
                          flats=[flat_record()],
                          releases=[release_record()],
                          frames=[frame_record("f1", [[10]], [[1]]),
                                  frame_record("f2", [[30]], [[1]])])])
        d = one_pixel_archive("D", 7)
        self.addCleanup(s.close)
        self.addCleanup(d.close)
        sources = [source(s, "S", offsets=[offset("f2", 1, "1/2", "0")]),
                   source(d, "D", origin_x="100", origin_y="100")]
        spec = self.spec(min_visits=2, regions=[region("W", "0", "0",
                                                       "3/2", "1")],
                         sources=sources)
        report = render(spec)
        # two frames of one visit never count as two visits
        self.assertEqual(region_entry(report, "W")["status"], "uncovered")
        self.assertEqual(region_entry(report, "W")["qualified_area"], "0")
        self.assertTrue(all(c["status"] == "uncovered"
                            for row in report["cells"] for c in row))
        # min_visits=1: same geometry delivers, with the dark random source
        # shared between the two frames of S (variance 81/8, not 45/8)
        report = render(self.spec(min_visits=1,
                                  regions=[region("W", "0", "0", "3/2", "1")],
                                  sources=sources))
        self.assertEqual(region_entry(report, "W"),
                         {"id": "W", "area": "3/2", "qualified_area": "3/2",
                          "status": "complete", "integral": "30",
                          "variance": "81/8"})
        self.assertEqual(cell(report, 0, 0)["integral"], "15")
        self.assertEqual(cell(report, 0, 0)["variance"], "37/8")
        self.assertEqual(cell(report, 0, 1)["variance"], "5/4")

    def test_shared_dark_across_releases_stays_one_random_source(self):
        # two releases, two windows, one dark_id: the dark random variable is
        # still shared, so W keeps variance 81/8
        releases = [release_record("r1", 1, start=0, end=100),
                    release_record("r2", 2, start=100, end=200)]
        m = build_archive(
            classic_definition("M"),
            [batch_record("b1", 0, darks=[dark_record()],
                          flats=[flat_record()], releases=releases,
                          frames=[frame_record("f1", [[10]], [[1]],
                                               observed_at=5),
                                  frame_record("f2", [[30]], [[1]],
                                               observed_at=150)])])
        self.addCleanup(m.close)
        sources = [source(m, "M", offsets=[offset("f2", 1, "1/2", "0")])]
        spec = {"sources": sources,
                "grid": {"x0": "0", "y0": "0", "width": 2, "height": 1},
                "regions": [region("W", "0", "0", "3/2", "1")],
                "min_visits": 1}
        report = render(spec)
        self.assertEqual(region_entry(report, "W")["integral"], "30")
        self.assertEqual(region_entry(report, "W")["variance"], "81/8")

    def test_source_and_region_order_do_not_change_results(self):
        first = render(self.spec())
        reordered = self.spec(sources=list(reversed(self.sources)),
                              regions=list(reversed(self.regions)))
        second = render(reordered)
        self.assertEqual(first, second)


# --------------------------------------------------------------------------
# Acceptance scenario 2: 2D geometry, escaped pixel, edge contact, revisions
# --------------------------------------------------------------------------
class AcceptanceTwoTests(unittest.TestCase):
    """A: rate 10 over [0,1)^2; B: rate 30, exposure 3, projected to
    [1/2,3/2)^2 although the old single-archive canvas would clip it;
    C: 2x1 archive whose valid right pixel only touches the origin cell
    along an edge (its huge count must never leak into the cell)."""

    def setUp(self):
        self.a = one_pixel_archive("A", 10)
        self.b = one_pixel_archive("B", 90, raw_variance=3, exposure=3, dx=1)
        self.c = build_archive(
            classic_definition("C", width=2, height=1),
            [batch_record("b1", 0,
                          darks=[dark_record(width=2, height=1)],
                          flats=[flat_record(values=[[1, 1]],
                                             mask=[[True, False]])],
                          releases=[release_record()],
                          frames=[frame_record("f", [[7, 200]], [[1, 1]],
                                               saturation=1000)])])
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)
        self.addCleanup(self.c.close)
        self.sources = [source(self.a, "A"),
                        source(self.b, "B", origin_x="-1/2", origin_y="1/2"),
                        source(self.c, "C")]

    def spec(self, min_visits):
        return {"sources": self.sources,
                "grid": {"x0": "0", "y0": "0", "width": 1, "height": 1},
                "regions": [region("box", "0", "0", "1", "1"),
                            region("ov", "1/2", "1/2", "1", "1")],
                "min_visits": min_visits}

    def test_min_visits_one_full_cell(self):
        report = render(self.spec(1))
        # 10 over 3/4 of the cell, (10*1+30*3)/4 = 25 over the overlap quarter
        self.assertEqual(cell(report, 0, 0),
                         {"qualified_area": "1", "status": "complete",
                          "integral": "55/4", "variance": "221/64"})
        self.assertEqual(region_entry(report, "box")["integral"], "55/4")
        self.assertEqual(region_entry(report, "box")["variance"], "221/64")
        self.assertEqual(region_entry(report, "ov"),
                         {"id": "ov", "area": "1/4", "qualified_area": "1/4",
                          "status": "complete", "integral": "25/4",
                          "variance": "11/64"})
        self.assertEqual(report["region_covariance"],
                         [["221/64", "13/32"], ["13/32", "11/64"]])

    def test_min_visits_two_keeps_only_the_overlap_quarter(self):
        report = render(self.spec(2))
        self.assertEqual(cell(report, 0, 0),
                         {"qualified_area": "1/4", "status": "partial",
                          "integral": "25/4", "variance": "11/64"})
        box = region_entry(report, "box")
        self.assertEqual(box["qualified_area"], "1/4")
        self.assertEqual(box["status"], "partial")
        self.assertIsNone(box["integral"])
        self.assertEqual(region_entry(report, "ov")["status"], "complete")
        self.assertEqual(region_entry(report, "ov")["integral"], "25/4")
        self.assertEqual(report["region_covariance"],
                         [[None, None], [None, "11/64"]])

    def test_old_archive_canvas_would_clip_but_mosaic_projects(self):
        # B's only pixel lands outside its own 1x1 integer canvas: the
        # classic archive report sees nothing, the mosaic still projects it
        old = archive_render(str(self.b.path), 1)
        self.assertEqual(old["report"]["rate_image"], [[None]])
        self.assertEqual(old["report"]["exposure_s"], [[0]])
        report = render(self.spec(1))
        self.assertEqual(cell(report, 0, 0)["integral"], "55/4")

    def test_revision_boundaries_offsets_never_move_across_versions(self):
        d = build_archive(
            classic_definition("D"),
            [batch_record("b1", 0, darks=[dark_record()],
                          flats=[flat_record()], releases=[release_record()],
                          frames=[frame_record("f", [[10]], [[1]])])])
        self.addCleanup(d.close)
        spec_v1 = {"sources": [source(d, "D", revision=1,
                                      offsets=[offset("f", 1, "1/2", "0")])],
                   "grid": {"x0": "0", "y0": "0", "width": 1, "height": 1},
                   "regions": [], "min_visits": 1}
        # revision 1: the half-cell offset projects the pixel to [1/2,3/2)
        first = render(spec_v1)
        self.assertEqual(cell(first, 0, 0),
                         {"qualified_area": "1/2", "status": "partial",
                          "integral": "5", "variance": "5/4"})
        # replace the frame at revision 2 (version 2, count 20)
        apply_batch(str(d.path), batch_record(
            "b2", 1, frames=[frame_record("f", [[20]], [[1]],
                                          expected_version=1)]))
        # the old correction names version 1 and must not move to version 2
        with self.assertRaises(MosaicError):
            render({"sources": [source(d, "D", revision=2,
                                       offsets=[offset("f", 1, "1/2", "0")])],
                    "grid": {"x0": "0", "y0": "0", "width": 1, "height": 1},
                    "regions": [], "min_visits": 1})
        # an explicit version-2 correction applies the same shift
        shifted = render({"sources": [source(d, "D", revision=2,
                                             offsets=[offset("f", 2, "1/2",
                                                             "0")])],
                          "grid": {"x0": "0", "y0": "0", "width": 1,
                                   "height": 1},
                          "regions": [], "min_visits": 1})
        self.assertEqual(cell(shifted, 0, 0)["integral"], "10")
        self.assertEqual(cell(shifted, 0, 0)["variance"], "5/4")
        # without offsets the new version projects unshifted
        plain = render({"sources": [source(d, "D", revision=2)],
                        "grid": {"x0": "0", "y0": "0", "width": 1,
                                 "height": 1},
                        "regions": [], "min_visits": 1})
        self.assertEqual(cell(plain, 0, 0),
                         {"qualified_area": "1", "status": "complete",
                          "integral": "20", "variance": "5"})
        # the old revision still reproduces the original result
        again = render(spec_v1)
        self.assertEqual(again, first)

    def test_blocked_and_withdrawn_frames_match_but_never_contribute(self):
        e = build_archive(
            classic_definition("E"),
            [batch_record("b1", 0, darks=[dark_record()],
                          flats=[flat_record()], releases=[release_record()],
                          frames=[frame_record("f", [[10]], [[1]])])])
        self.addCleanup(e.close)
        offsets = [offset("f", 1, "1/2", "0")]

        def spec(revision, frame_offsets):
            return {"sources": [source(e, "E", revision=revision,
                                       offsets=frame_offsets)],
                    "grid": {"x0": "0", "y0": "0", "width": 1, "height": 1},
                    "regions": [], "min_visits": 1}

        self.assertEqual(cell(render(spec(1, offsets)), 0, 0)["integral"],
                         "5")
        # retracting the release blocks the frame at revision 2; the
        # correction still matches the visible version but contributes nothing
        apply_batch(str(e.path), batch_record("b2", 1, retract_releases=["r1"]))
        blocked = render(spec(2, offsets))
        self.assertEqual(cell(blocked, 0, 0),
                         {"qualified_area": "0", "status": "uncovered",
                          "integral": None, "variance": None})
        # restore readiness with a fresh release and a replacement frame
        # (version 2), then withdraw: the withdrawn record is version 3, so
        # a correction must name version 3 to be legal
        apply_batch(str(e.path), batch_record(
            "b3", 2, releases=[release_record("r2", 2)],
            frames=[frame_record("f", [[10]], [[1]], expected_version=1)]))
        apply_batch(str(e.path), batch_record(
            "b4", 3, withdraw_frames=[{"id": "f", "expected_version": 2}]))
        withdrawn = render(spec(4, [offset("f", 3, "1/2", "0")]))
        self.assertEqual(cell(withdrawn, 0, 0)["status"], "uncovered")
        with self.assertRaises(MosaicError):
            render(spec(4, [offset("f", 2, "1/2", "0")]))
        # history is unchanged: revision 1 still renders the original shift
        self.assertEqual(cell(render(spec(1, offsets)), 0, 0)["integral"],
                         "5")

    def test_mosaic_never_modifies_source_archives(self):
        before = archive_render(str(self.a.path), 1)
        render(self.spec(1))
        self.assertEqual(archive_render(str(self.a.path), 1), before)


# --------------------------------------------------------------------------
# Acceptance scenario 3: far-apart coordinates stay local
# --------------------------------------------------------------------------
class AcceptanceThreeTests(unittest.TestCase):
    FAR = "1000000000"

    def setUp(self):
        self.a = one_pixel_archive("A", 10)
        self.b = one_pixel_archive("B", 30)
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)
        self.spec = {
            "sources": [source(self.a, "A"),
                        source(self.b, "B", origin_x=self.FAR,
                               origin_y=self.FAR)],
            "grid": {"x0": "0", "y0": "0", "width": 1, "height": 1},
            "regions": [region("far", self.FAR, self.FAR,
                               "1000000001", "1000000001"),
                        region("home", "0", "0", "1", "1")],
            "min_visits": 1}

    def test_far_apart_values(self):
        report = render(self.spec)
        self.assertEqual(cell(report, 0, 0),
                         {"qualified_area": "1", "status": "complete",
                          "integral": "10", "variance": "5"})
        self.assertEqual(region_entry(report, "far"),
                         {"id": "far", "area": "1", "qualified_area": "1",
                          "status": "complete", "integral": "30",
                          "variance": "5"})
        self.assertEqual(region_entry(report, "home")["integral"], "10")
        self.assertEqual(report["region_covariance"],
                         [["5", "0"], ["0", "5"]])

    def test_cli_far_apart_under_256_mib_and_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            spec_path = write_json(Path(directory), "spec.json", self.spec)
            output = Path(directory, "report.json")
            env = dict(os.environ, PYTHONPATH=str(SRC))

            def limit_address_space():
                import resource
                limit = 256 * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

            process = subprocess.run(
                [sys.executable, "-m", "astro_stack.cli_mosaic",
                 "--spec", str(spec_path), "--output", str(output)],
                capture_output=True, text=True, env=env,
                preexec_fn=limit_address_space, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(cell(report, 0, 0)["integral"], "10")
            self.assertEqual(region_entry(report, "far")["integral"], "30")
            self.assertEqual(report["region_covariance"],
                             [["5", "0"], ["0", "5"]])


# --------------------------------------------------------------------------
# Rational region edges are native, never snapped to a sampling grid
# --------------------------------------------------------------------------
class RationalEdgeTests(unittest.TestCase):
    def setUp(self):
        self.a = one_pixel_archive("A", 10)
        self.b = one_pixel_archive("B", 30)
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)

    def test_third_cut_region_uses_exact_edges(self):
        spec = {"sources": [source(self.a, "A")],
                "grid": {"x0": "0", "y0": "0", "width": 1, "height": 1},
                "regions": [region("third", "1/3", "1/6", "2/3", "5/6")],
                "min_visits": 1}
        report = render(spec)
        # area (1/3)*(2/3) = 2/9 fully covered by the rate-10 pixel
        self.assertEqual(region_entry(report, "third"),
                         {"id": "third", "area": "2/9",
                          "qualified_area": "2/9", "status": "complete",
                          "integral": "20/9", "variance": "20/81"})
        self.assertEqual(cell(report, 0, 0)["integral"], "10")

    def test_third_offset_visit_changes_local_denominators(self):
        spec = {"sources": [source(self.a, "A"),
                            source(self.b, "B", origin_x="1/3")],
                "grid": {"x0": "0", "y0": "0", "width": 1, "height": 1},
                "regions": [region("left", "0", "0", "1/3", "1"),
                            region("mid", "1/3", "0", "2/3", "1")],
                "min_visits": 1}
        report = render(spec)
        # [0,1/3) only A; [1/3,1) A and B -> cell integral 10/3 + 20*(2/3)
        self.assertEqual(cell(report, 0, 0),
                         {"qualified_area": "1", "status": "complete",
                          "integral": "50/3", "variance": "25/9"})
        self.assertEqual(region_entry(report, "left")["integral"], "10/3")
        self.assertEqual(region_entry(report, "mid"),
                         {"id": "mid", "area": "1/3", "qualified_area": "1/3",
                          "status": "complete", "integral": "20/3",
                          "variance": "5/18"})
        # left and mid share A's raw and dark noise: (1/3)(1/6)(1+4) = 5/18
        self.assertEqual(report["region_covariance"],
                         [["5/9", "5/18"], ["5/18", "5/18"]])


# --------------------------------------------------------------------------
# Strict input validation
# --------------------------------------------------------------------------
class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.a = one_pixel_archive("A", 10)
        self.addCleanup(self.a.close)
        self.base = {"sources": [source(self.a, "A")],
                     "grid": {"x0": "0", "y0": "0", "width": 1, "height": 1},
                     "regions": [], "min_visits": 1}

    def spec(self, **changes):
        value = dict(self.base)
        value.update(changes)
        return value

    def test_root_shape_and_required_fields(self):
        for bad in (None, [], "x", 5,
                    self.spec(min_visits=None),
                    {k: v for k, v in self.base.items() if k != "grid"},
                    dict(self.base, extra=1)):
            with self.assertRaises(ValueError, msg=repr(bad)):
                render(bad)

    def test_sources_count_and_fields(self):
        with self.assertRaises(ValueError):
            render(self.spec(sources=[]))
        with self.assertRaises(ValueError):
            render(self.spec(sources=[source(self.a, "A")] * 9))
        for bad_source in (
                {"dataset_id": "A", "revision": 1, "origin_x": "0",
                 "origin_y": "0", "offsets": []},
                dict(source(self.a, "A"), archive=""),
                dict(source(self.a, "A"), archive=5),
                dict(source(self.a, "A"), revision=-1),
                dict(source(self.a, "A"), revision=True),
                dict(source(self.a, "A"), revision="1"),
                dict(source(self.a, "A"), revision=10**12 + 1),
                dict(source(self.a, "A"), unknown=0)):
            with self.assertRaises(ValueError, msg=repr(bad_source)):
                render(self.spec(sources=[bad_source]))

    def test_dataset_id_must_be_unique_and_match_the_archive(self):
        with self.assertRaises(ValueError):
            render(self.spec(sources=[source(self.a, "A"),
                                      source(self.a, "A")]))
        with self.assertRaises(MosaicError):
            render(self.spec(sources=[source(self.a, "wrong")]))

    def test_rational_string_rules(self):
        good = {"0": 0, "1": 1, "-1": -1, "1/2": 0.5, "-1/2": -0.5,
                "2/4": 0.5, "10/5": 2, "1000000000000": 10**12,
                "1/1000000000000": 1e-12}
        for text in good:
            report = render(self.spec(
                sources=[source(self.a, "A", origin_x=text)]))
            self.assertEqual(report["schema_version"], 1)
        for bad in ("+1", "01", "-01", "-0", "-0/3", "1.0", "0.5", " 1",
                    "1 ", "", "1/0", "1/02", "1/", "/2", "1/2/3", "x",
                    "1000000000001", "1/1000000000001", "1e3", "0x1"):
            with self.assertRaises(MosaicError, msg=repr(bad)):
                render(self.spec(sources=[source(self.a, "A",
                                                 origin_x=bad)]))

    def test_offsets_validation(self):
        cases = (
            [offset("f", 1, "2", "0")],                # dx outside [-1,1]
            [offset("f", 1, "0", "-3/2")],             # dy outside [-1,1]
            [offset("f", 0, "0", "0")],                # version not positive
            [offset("f", True, "0", "0")],             # bool version
            [offset("f", 1, "0", "0"), offset("f", 1, "0", "0")],  # duplicate
            [offset("g", 1, "0", "0")],                # unknown frame
            [offset("f", 2, "0", "0")],                # version mismatch
            [dict(offset("f", 1, "0", "0"), extra=0)],  # unknown field
        )
        for offsets in cases:
            with self.assertRaises(ValueError, msg=repr(offsets)):
                render(self.spec(sources=[source(self.a, "A",
                                                 offsets=offsets)]))
        # 51 entries exceed the per-source limit
        too_many = [offset("f", 1, "0", "0")] + [
            {"frame_id": f"x{i}", "frame_version": 1, "dx": "0", "dy": "0"}
            for i in range(50)]
        with self.assertRaises(ValueError):
            render(self.spec(sources=[source(self.a, "A",
                                             offsets=too_many)]))
        # legal corrections at the boundary of [-1,1]
        report = render(self.spec(sources=[source(
            self.a, "A", offsets=[offset("f", 1, "-1", "1")])]))
        self.assertEqual(report["schema_version"], 1)

    def test_grid_validation(self):
        for bad_grid in (
                {"x0": "0", "y0": "0", "width": 0, "height": 1},
                {"x0": "0", "y0": "0", "width": 65, "height": 1},
                {"x0": "0", "y0": "0", "width": True, "height": 1},
                {"x0": "0", "y0": "0", "width": 1},
                {"x0": "0", "y0": "0", "width": 1, "height": 1, "z": 0},
                {"x0": "0.5", "y0": "0", "width": 1, "height": 1}):
            with self.assertRaises(ValueError, msg=repr(bad_grid)):
                render(self.spec(grid=bad_grid))

    def test_regions_validation(self):
        for bad_regions in (
                [region("r", "0", "0", "0", "1")],          # empty in x
                [region("r", "1", "0", "0", "1")],          # inverted x
                [region("r", "0", "0", "1", "0")],          # empty in y
                [region("r", "0", "0", "1", "1"),
                 region("r", "0", "0", "2", "2")],          # duplicate id
                [{"id": "r", "x0": "0", "y0": "0", "x1": "1", "y1": "1",
                  "extra": 0}],
                [{"id": "r", "x0": "0", "y0": "0", "x1": "1"}],
                [region("r", "0", "0", "1", "1")] * 17,     # too many
        ):
            with self.assertRaises(ValueError, msg=repr(bad_regions)):
                render(self.spec(regions=bad_regions))

    def test_min_visits_bounds(self):
        for bad in (0, 2, -1, True, "1", 1.0, None):
            with self.assertRaises((ValueError, TypeError), msg=repr(bad)):
                render(self.spec(min_visits=bad))

    def test_archive_state_rejections(self):
        # campaign archives are not supported
        campaign = TempArchive()
        initialize(str(campaign.path), {
            "dataset_id": "P", "width": 1, "height": 1,
            "apertures": [{"id": "a", "x": 0, "y": 0, "radius": 0}],
            "products": [{"id": "p", "start_s": 0, "end_s": 100,
                          "priority": 1, "max_frames": 1,
                          "requirements": [{"aperture_id": "a",
                                            "min_exposure_s": 1,
                                            "max_variance": "2"}]}]})
        self.addCleanup(campaign.close)
        with self.assertRaises(MosaicError):
            render(self.spec(sources=[source(campaign, "P", revision=0)]))
        # revision beyond the committed history
        with self.assertRaises(ValueError):
            render(self.spec(sources=[source(self.a, "A", revision=2)]))
        # unreadable / missing archive paths
        for path in ("/nonexistent/astro-mosaic/archive",
                     str(Path(self.a.path, "state.json"))):
            broken = dict(source(self.a, "A"), archive=path)
            with self.assertRaises(ValueError, msg=path):
                render(self.spec(sources=[broken]))

    def test_zero_contribution_source_is_a_success(self):
        empty = TempArchive()
        initialize(str(empty.path), classic_definition("E"))
        self.addCleanup(empty.close)
        sources = [source(self.a, "A"), source(empty, "E", revision=0)]
        report = render(self.spec(sources=sources))
        self.assertEqual(cell(report, 0, 0),
                         {"qualified_area": "1", "status": "complete",
                          "integral": "10", "variance": "5"})
        # min_visits=2 can never be reached with an empty visit
        report = render(self.spec(sources=sources, min_visits=2))
        self.assertEqual(cell(report, 0, 0)["status"], "uncovered")
        self.assertEqual(report["summary"]["ready_frames"], 1)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
class CliTests(unittest.TestCase):
    def setUp(self):
        self.a = one_pixel_archive("A", 10)
        self.b = one_pixel_archive("B", 30)
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)
        self.spec = {
            "sources": [source(self.a, "A"),
                        source(self.b, "B", origin_x="1/2")],
            "grid": {"x0": "0", "y0": "0", "width": 2, "height": 1},
            "regions": [region("L", "0", "0", "1", "1"),
                        region("W", "0", "0", "3/2", "1"),
                        region("C", "1/4", "0", "5/4", "1")],
            "min_visits": 1}

    def run_cli(self, *args):
        env = dict(os.environ, PYTHONPATH=str(SRC))
        return subprocess.run(
            [sys.executable, "-m", "astro_stack.cli_mosaic", *args],
            capture_output=True, text=True, env=env)

    def test_cli_publishes_report_prints_summary_and_protects_target(self):
        with tempfile.TemporaryDirectory() as directory:
            spec_path = write_json(Path(directory), "spec.json", self.spec)
            output = Path(directory, "nested", "report.json")
            process = self.run_cli("--spec", str(spec_path),
                                   "--output", str(output))
            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(json.loads(process.stdout.strip()),
                             report["summary"])
            self.assertEqual(cell(report, 0, 0)["integral"], "15")
            before = output.read_text()
            blocked = self.run_cli("--spec", str(spec_path),
                                   "--output", str(output))
            self.assertEqual(blocked.returncode, 2)
            self.assertEqual(output.read_text(), before)

    def test_cli_failure_leaves_no_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "report.json")
            bad_spec = dict(self.spec, min_visits=5)
            spec_path = write_json(Path(directory), "spec.json", bad_spec)
            process = self.run_cli("--spec", str(spec_path),
                                   "--output", str(output))
            self.assertEqual(process.returncode, 2)
            self.assertFalse(output.exists())
            # unknown archive path fails the same way
            bad_source = dict(self.spec["sources"][0],
                              archive="/nonexistent/astro-mosaic/archive")
            spec_path = write_json(Path(directory), "spec2.json",
                                   dict(self.spec, sources=[bad_source]))
            process = self.run_cli("--spec", str(spec_path),
                                   "--output", str(output))
            self.assertEqual(process.returncode, 2)
            self.assertFalse(output.exists())

    def test_cli_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            spec_path = Path(directory, "spec.json")
            spec_path.write_text(
                '{"sources": [], "sources": [], "grid": {}, "regions": [], '
                '"min_visits": 1}')
            output = Path(directory, "report.json")
            process = self.run_cli("--spec", str(spec_path),
                                   "--output", str(output))
            self.assertEqual(process.returncode, 2)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
