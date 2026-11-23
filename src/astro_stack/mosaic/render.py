"""Exact sub-pixel mosaic engine.

Every ready detector sample of every requested source revision is projected
onto the common plane as a unit square whose top-left corner is
``(x + frame.dx + origin_x + extra_dx, y + frame.dy + origin_y + extra_dy)``;
the old single-archive integer canvas no longer clips anything.  Targets
(grid cells and photometry rectangles) are integrated exactly over the
rectilinear partition induced by the sample edges, so no dense canvas is ever
allocated for empty sky between far-apart visits and rational region edges
are native.

At a point the local rate is the exposure-weighted mean of the calibrated
rates of the samples covering that point (its denominator is the sum of the
exposures actually covering it); a point is qualified when at least
``min_visits`` distinct visits cover it.  Cells deliver the integral over
their qualified part; regions deliver only full nominal-rectangle integrals.

Noise identities follow the archive contract, prefixed with the dataset id so
no random source is ever shared across archives: raw counts are
``("raw", dataset_id, frame_id, frame_version, x, y)`` and dark counts are
``("dark", dataset_id, dark_id, x, y)``.  Flats, variances, quality
rejections, offsets and exposures are deterministic.
"""
import math
from fractions import Fraction

from ..archive.science import select_bindings, _ready_frames
from ..archive.store import Store, replay_snapshot
from .model import MosaicError, validate_specification


class _Sample:
    """One accepted detector sample projected onto the common plane."""

    __slots__ = ("visit", "x0", "y0", "exposure", "rate",
                 "raw_key", "raw_coeff", "dark_key", "dark_coeff")

    def __init__(self, visit, x0, y0, exposure, rate, raw_key, raw_coeff,
                 dark_key, dark_coeff):
        self.visit = visit
        self.x0 = x0
        self.y0 = y0
        self.exposure = exposure
        self.rate = rate
        self.raw_key = raw_key
        self.raw_coeff = raw_coeff
        self.dark_key = dark_key
        self.dark_coeff = dark_coeff


def _capture(path, revision):
    with Store(path) as store:
        snapshot = replay_snapshot(store.state, store.root, revision)
    return snapshot


def _check_offsets(source, snapshot):
    """Bind each requested correction to the frame version visible at the revision.

    Withdrawn or blocked frames may carry a correction but never contribute;
    an unknown frame or a version mismatch fails the whole request rather
    than silently moving a correction to another version.
    """
    offsets = {}
    for frame_id, (frame_version, dx, dy) in source["offsets"].items():
        frame = snapshot.frames.get(frame_id)
        if frame is None:
            raise MosaicError(
                f"offsets reference unknown frame {frame_id!r} at revision "
                f"{source['revision']} of {source['dataset_id']!r}")
        if frame["frame_version"] != frame_version:
            raise MosaicError(
                f"offsets frame_version mismatch for {frame_id!r}: revision "
                f"{source['revision']} of {source['dataset_id']!r} shows "
                f"{frame['frame_version']}, requested {frame_version}")
        offsets[frame_id] = (dx, dy)
    return offsets


def _source_visit(visit, source, var_of):
    """Capture the requested revision and project its ready samples.

    Returns (frames, ready_count, accepted_count); each frame entry is
    (samples, origin_x, origin_y, width, height) with samples keyed by
    detector coordinate.
    """
    snapshot = _capture(source["archive"], source["revision"])
    dataset_id = source["dataset_id"]
    if snapshot.dataset_id != dataset_id:
        raise MosaicError(
            f"archive dataset_id mismatch: request says {dataset_id!r}, "
            f"archive holds {snapshot.dataset_id!r}")
    if snapshot.products is not None:
        raise MosaicError(
            f"mosaic supports classic archives only: {dataset_id!r} "
            "declares products")
    offsets = _check_offsets(source, snapshot)
    bindings = select_bindings(snapshot)
    ready = _ready_frames(snapshot, bindings)
    width, height = snapshot.width, snapshot.height
    frames = []
    accepted = 0
    for frame_id in sorted(ready):
        frame = ready[frame_id]
        binding = bindings[frame_id]
        dark = snapshot.darks[binding["dark_id"]]
        flat_record = snapshot.flats[binding["flat_id"]]
        mean_flat = flat_record["mean_flat"]
        dark_exposure = dark["dark_exposure_s"]
        dark_id = dark["id"]
        extra_dx, extra_dy = offsets.get(frame_id,
                                         (Fraction(0), Fraction(0)))
        ox = source["origin_x"] + frame["dx"] + extra_dx
        oy = source["origin_y"] + frame["dy"] + extra_dy
        exposure = frame["exposure_s"]
        frame_version = frame["frame_version"]
        samples = {}
        for y in range(height):
            for x in range(width):
                if flat_record["bad_mask"][y][x]:
                    continue
                flat = flat_record["flat"][y][x]
                if flat == 0:
                    continue
                pixel = frame["pixels"][y][x]
                if pixel >= frame["saturation"]:
                    continue
                dark_rate = Fraction(dark["dark"][y][x], dark_exposure)
                cal_rate = (Fraction(pixel, exposure) - dark_rate) \
                    * mean_flat / flat
                flat_factor = mean_flat / flat
                raw_key = ("raw", dataset_id, frame_id, frame_version, x, y)
                dark_key = ("dark", dataset_id, dark_id, x, y)
                var_of[raw_key] = Fraction(frame["variance"][y][x])
                var_of[dark_key] = Fraction(dark["dark_variance"][y][x])
                samples[(x, y)] = _Sample(
                    visit, ox + x, oy + y, exposure, cal_rate, raw_key,
                    flat_factor / exposure, dark_key,
                    -flat_factor / dark_exposure)
                accepted += 1
        if samples:
            frames.append((samples, ox, oy, width, height))
    return frames, len(ready), accepted


def _clip_samples(rect, visits):
    """Positive-area intersections of the projected samples with the target.

    The intersecting detector range of each frame is computed arithmetically
    from its projection origin, so empty sky between far-apart visits is
    never enumerated.
    """
    tx0, ty0, tx1, ty1 = rect
    clipped = []
    for frames in visits:
        for samples, ox, oy, width, height in frames:
            xlo = math.floor(tx0 - ox - 1) + 1
            xhi = math.ceil(tx1 - ox) - 1
            if xlo < 0:
                xlo = 0
            if xhi >= width:
                xhi = width - 1
            if xlo > xhi:
                continue
            ylo = math.floor(ty0 - oy - 1) + 1
            yhi = math.ceil(ty1 - oy) - 1
            if ylo < 0:
                ylo = 0
            if yhi >= height:
                yhi = height - 1
            if ylo > yhi:
                continue
            for x in range(xlo, xhi + 1):
                for y in range(ylo, yhi + 1):
                    sample = samples.get((x, y))
                    if sample is None:
                        continue
                    cx0 = sample.x0 if sample.x0 > tx0 else tx0
                    cx1 = sample.x0 + 1
                    if cx1 > tx1:
                        cx1 = tx1
                    cy0 = sample.y0 if sample.y0 > ty0 else ty0
                    cy1 = sample.y0 + 1
                    if cy1 > ty1:
                        cy1 = ty1
                    clipped.append((cx0, cy0, cx1, cy1, sample))
    return clipped


def _analyze(rect, visits, min_visits):
    """Exact (qualified_area, integral mean, noise coefficients) of one target.

    The target is swept along the x edges of the clipped samples; inside each
    x slab a y sweep keeps the active sample set, the distinct-visit count
    and the local exposure denominator.  Only qualified elementary
    rectangles accumulate area and per-sample weights ``area * e_s / E``.
    """
    clipped = _clip_samples(rect, visits)
    qualified = Fraction(0)
    if not clipped:
        return qualified, Fraction(0), {}
    xs = sorted({entry[0] for entry in clipped}
                | {entry[2] for entry in clipped})
    weights = {}
    for position in range(len(xs) - 1):
        xa, xb = xs[position], xs[position + 1]
        xact = [entry for entry in clipped
                if entry[0] <= xa and entry[2] >= xb]
        if not xact:
            continue
        ys = sorted({entry[1] for entry in xact}
                    | {entry[3] for entry in xact})
        add_at = {}
        remove_at = {}
        for index, entry in enumerate(xact):
            add_at.setdefault(entry[1], []).append(index)
            remove_at.setdefault(entry[3], []).append(index)
        active = set()
        visit_refs = {}
        exposure_total = 0
        for row in range(len(ys) - 1):
            ya, yb = ys[row], ys[row + 1]
            for index in remove_at.get(ya, ()):
                sample = xact[index][4]
                active.discard(index)
                exposure_total -= sample.exposure
                remaining = visit_refs[sample.visit] - 1
                if remaining:
                    visit_refs[sample.visit] = remaining
                else:
                    del visit_refs[sample.visit]
            for index in add_at.get(ya, ()):
                sample = xact[index][4]
                active.add(index)
                exposure_total += sample.exposure
                visit_refs[sample.visit] = \
                    visit_refs.get(sample.visit, 0) + 1
            if not active or len(visit_refs) < min_visits:
                continue
            area = (xb - xa) * (yb - ya)
            qualified += area
            share = area / exposure_total
            for index in active:
                sample = xact[index][4]
                key = sample.raw_key
                weights[key] = weights.get(key, Fraction(0)) \
                    + share * sample.exposure
    by_key = {entry[4].raw_key: entry[4] for entry in clipped}
    mean = Fraction(0)
    coeffs = {}
    for key, weight in weights.items():
        sample = by_key[key]
        mean += weight * sample.rate
        coeffs[key] = coeffs.get(key, Fraction(0)) + weight * sample.raw_coeff
        dark_key = sample.dark_key
        coeffs[dark_key] = coeffs.get(dark_key, Fraction(0)) \
            + weight * sample.dark_coeff
    return qualified, mean, coeffs


def _variance(coeffs, var_of):
    total = Fraction(0)
    for key, coefficient in coeffs.items():
        total += var_of[key] * coefficient * coefficient
    return total


def _covariance(left, right, var_of):
    """Exact covariance of two target integrals over every shared source."""
    if len(left) > len(right):
        left, right = right, left
    total = Fraction(0)
    for key, coefficient in left.items():
        other = right.get(key)
        if other is not None:
            total += var_of[key] * coefficient * other
    return total


def _cells(grid, visits, min_visits, var_of):
    rows = []
    covered = 0
    complete = 0
    one = Fraction(1)
    for row in range(grid["height"]):
        cell_row = []
        y0 = grid["y0"] + row
        for column in range(grid["width"]):
            x0 = grid["x0"] + column
            qualified, mean, coeffs = _analyze(
                (x0, y0, x0 + 1, y0 + 1), visits, min_visits)
            if not qualified:
                cell_row.append({"qualified_area": "0",
                                 "status": "uncovered",
                                 "integral": None, "variance": None})
                continue
            covered += 1
            status = "partial"
            if qualified == one:
                status = "complete"
                complete += 1
            cell_row.append({"qualified_area": str(qualified),
                             "status": status,
                             "integral": str(mean),
                             "variance": str(_variance(coeffs, var_of))})
        rows.append(cell_row)
    return rows, covered, complete


def _regions(regions, visits, min_visits, var_of):
    forms = {}
    areas = {}
    qualified_of = {}
    for region in regions:
        rect = (region["x0"], region["y0"], region["x1"], region["y1"])
        qualified, mean, coeffs = _analyze(rect, visits, min_visits)
        forms[region["id"]] = (mean, coeffs)
        areas[region["id"]] = (region["x1"] - region["x0"]) \
            * (region["y1"] - region["y0"])
        qualified_of[region["id"]] = qualified
    order = sorted(forms)
    entries = []
    complete = 0
    for region_id in order:
        area = areas[region_id]
        qualified = qualified_of[region_id]
        mean, coeffs = forms[region_id]
        if not qualified:
            status = "uncovered"
        elif qualified == area:
            status = "complete"
            complete += 1
        else:
            status = "partial"
        delivered = status == "complete"
        entries.append({
            "id": region_id, "area": str(area),
            "qualified_area": str(qualified), "status": status,
            "integral": str(mean) if delivered else None,
            "variance": str(_variance(coeffs, var_of))
            if delivered else None})
    size = len(order)
    covariance = [[None for _ in range(size)] for _ in range(size)]
    for i, left_id in enumerate(order):
        if qualified_of[left_id] != areas[left_id]:
            continue
        for j, right_id in enumerate(order):
            if qualified_of[right_id] != areas[right_id]:
                continue
            covariance[i][j] = str(_covariance(
                forms[left_id][1], forms[right_id][1], var_of))
    return entries, covariance, complete


def render(specification):
    """Render the schema_version=1 sub-pixel mosaic report.

    The specification is validated as a whole before any archive is opened;
    archives are only read, never modified.
    """
    spec = validate_specification(specification)
    min_visits = spec["min_visits"]
    var_of = {}
    visits = []
    ready_frames = 0
    accepted_samples = 0
    for visit, source in enumerate(spec["sources"]):
        frames, ready, accepted = _source_visit(visit, source, var_of)
        visits.append(frames)
        ready_frames += ready
        accepted_samples += accepted
    cells, covered_cells, complete_cells = _cells(
        spec["grid"], visits, min_visits, var_of)
    region_entries, covariance, complete_regions = _regions(
        spec["regions"], visits, min_visits, var_of)
    sources = [{"dataset_id": source["dataset_id"],
                "revision": source["revision"]}
               for source in sorted(spec["sources"],
                                    key=lambda item: item["dataset_id"])]
    grid = spec["grid"]
    return {
        "schema_version": 1,
        "min_visits": min_visits,
        "sources": sources,
        "grid": {"x0": str(grid["x0"]), "y0": str(grid["y0"]),
                 "width": grid["width"], "height": grid["height"]},
        "cells": cells,
        "regions": region_entries,
        "region_covariance": covariance,
        "summary": {"sources": len(spec["sources"]),
                    "ready_frames": ready_frames,
                    "accepted_samples": accepted_samples,
                    "covered_cells": covered_cells,
                    "complete_cells": complete_cells,
                    "complete_regions": complete_regions}}
