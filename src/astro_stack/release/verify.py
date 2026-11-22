"""Offline package verification: digest gate plus full evidence recomputation.

The verifier never opens the source archive or any license.  It first checks
the caller-supplied expected SHA-256, then rebuilds coordinate mapping,
calibration, exposure weighting and exact rational error propagation from the
package evidence alone, and rejects any internal contradiction or redundant
evidence.
"""
from fractions import Fraction

from .package import FORMAT, PackageError, parse_fraction

TOP_FIELDS = {"format", "request", "frame", "cells", "region", "frames",
              "releases", "darks", "flats", "contributions"}
REQUEST_FIELDS = {"request_id", "dataset_id", "revision", "region"}
REGION_FIELDS = {"x0", "y0", "x1", "y1"}
CELL_BASE = {"x", "y", "status"}
CELL_PUBLISHED = CELL_BASE | {"rate", "variance", "exposure_s",
                              "contributor_count", "contributions"}
FRAME_FIELDS = {"frame_id", "frame_version", "exposure_s", "dx", "dy",
                "saturation", "observed_at_s", "samples"}
FRAME_SAMPLE_FIELDS = {"x", "y", "count", "variance"}
RELEASE_FIELDS = {"id", "version", "start_s", "end_s", "dark_id", "flat_id"}
DARK_FIELDS = {"id", "dark_exposure_s", "samples"}
DARK_SAMPLE_FIELDS = {"x", "y", "dark", "dark_variance"}
FLAT_FIELDS = {"id", "mean_flat", "samples"}
FLAT_SAMPLE_FIELDS = {"x", "y", "flat", "masked"}
CONTRIBUTION_FIELDS = {"frame", "sample", "release", "dark", "dark_sample",
                       "flat", "flat_sample", "output_x", "output_y"}
STATUSES = {"published", "withheld", "uncovered"}


def _require(condition, message):
    if not condition:
        raise PackageError(message)


def _exact_fields(value, fields, name):
    _require(isinstance(value, dict), f"{name} must be an object")
    _require(set(value) == fields,
             f"{name} has invalid fields: missing={sorted(fields - set(value))}, "
             f"unknown={sorted(set(value) - fields)}")


def _nonneg_int(value, name):
    _require(type(value) is int and value >= 0, f"{name} must be a non-negative integer")
    return value


def _positive_int(value, name):
    _require(type(value) is int and value > 0, f"{name} must be a positive integer")
    return value


def verify_document(document):
    """Recompute every published cell and the region totals from the evidence.

    Raises PackageError on any inconsistency; returns a JSON-ready result on
    success.  The caller is responsible for checking the package digest before
    invoking this function.
    """
    _exact_fields(document, TOP_FIELDS, "package")
    _require(document["format"] == FORMAT, "unknown package format")
    request = document["request"]
    _exact_fields(request, REQUEST_FIELDS, "request")
    _require(isinstance(request["request_id"], str)
             and request["request_id"].strip()
             and len(request["request_id"]) <= 200, "bad request_id")
    _require(isinstance(request["dataset_id"], str)
             and request["dataset_id"].strip()
             and len(request["dataset_id"]) <= 200, "bad dataset_id")
    revision = request["revision"]
    _nonneg_int(revision, "revision")
    _require(revision <= 10**12, "revision outside the archive integer range")
    region = request["region"]
    _exact_fields(region, REGION_FIELDS, "region")
    for name in REGION_FIELDS:
        _nonneg_int(region[name], f"region.{name}")
    _require(region["x0"] < region["x1"] and region["y0"] < region["y1"],
             "region must be non-empty")
    frame_meta = document["frame"]
    _exact_fields(frame_meta, {"width", "height"}, "frame")
    width = _positive_int(frame_meta["width"], "frame.width")
    height = _positive_int(frame_meta["height"], "frame.height")
    _require(0 <= region["x0"] and region["x1"] <= width
             and 0 <= region["y0"] and region["y1"] <= height,
             "region outside the archive frame")

    frames = _parse_frames(document["frames"], width, height)
    releases, release_by_id = _parse_releases(document["releases"])
    darks, dark_by_id = _parse_darks(document["darks"], width, height)
    flats, flat_by_id = _parse_flats(document["flats"], width, height)
    contributions = _parse_contributions(
        document["contributions"], frames, releases, darks, flats,
        release_by_id, dark_by_id, flat_by_id, region)

    cells = document["cells"]
    _require(isinstance(cells, list) and cells, "cells must be a non-empty array")
    expected_positions = [(x, y)
                          for y in range(region["y0"], region["y1"])
                          for x in range(region["x0"], region["x1"])]
    _require([(cell["x"], cell["y"]) for cell in cells] == expected_positions,
             "cells must be row-major absolute coordinates covering the region")
    used_contributions = set()
    per_cell = {}
    for cell in cells:
        _require(isinstance(cell, dict), "cell must be an object")
        _require(cell.get("status") in STATUSES, "unknown cell status")
        status = cell["status"]
        if status == "published":
            _exact_fields(cell, CELL_PUBLISHED, "published cell")
        else:
            _exact_fields(cell, CELL_BASE, f"{status} cell")
            _require(not any(key in cell for key in
                             ("rate", "variance", "exposure_s",
                              "contributor_count", "contributions")),
                     f"{status} cell must not carry pixel evidence")
            continue
        _nonneg_int(cell["exposure_s"], "cell exposure_s")
        _require(cell["exposure_s"] > 0, "published cell needs positive exposure")
        _positive_int(cell["contributor_count"], "cell contributor_count")
        indexes = cell["contributions"]
        _require(isinstance(indexes, list) and indexes
                 and len(indexes) == len(set(indexes)),
                 "cell contributions must be a non-empty list of unique indexes")
        for index in indexes:
            _require(type(index) is int and 0 <= index < len(contributions),
                    "contribution index out of range")
            _require(index not in used_contributions,
                     "contribution reused across cells")
            contribution = contributions[index]
            _require(contribution["output_x"] == cell["x"]
                     and contribution["output_y"] == cell["y"],
                     "contribution output coordinate does not match its cell")
            used_contributions.add(index)
        _require(len(indexes) == cell["contributor_count"],
                 "contributor_count does not match the evidence")
        per_cell[(cell["x"], cell["y"])] = indexes
    _require(used_contributions == set(range(len(contributions))),
             "every contribution must be bound to a published cell")
    _check_table_reachability(document, contributions)

    computed_cells, region_rate, region_variance, counts = _recompute(
        cells, contributions, frames, darks, flats, release_by_id,
        dark_by_id, flat_by_id, per_cell)
    for cell, computed in zip(cells, computed_cells):
        if cell["status"] != "published":
            continue
        _require(cell["rate"] == str(computed["rate"]),
                 f"rate mismatch at ({cell['x']},{cell['y']})")
        _require(cell["variance"] == str(computed["variance"]),
                 f"variance mismatch at ({cell['x']},{cell['y']})")
        _require(cell["exposure_s"] == computed["exposure_s"],
                 f"exposure mismatch at ({cell['x']},{cell['y']})")
        _require(cell["contributor_count"] == computed["contributor_count"],
                 f"contributor count mismatch at ({cell['x']},{cell['y']})")
    region_block = document["region"]
    _exact_fields(region_block, {"rate_sum", "variance", "counts"}, "region")
    _require(set(region_block["counts"]) == {"published", "withheld", "uncovered"},
             "region counts have invalid keys")
    _require(region_block["counts"] == counts, "region status counts mismatch")
    if counts["published"] == 0:
        _require(region_block["rate_sum"] is None
                 and region_block["variance"] is None,
                 "region totals must be null without published cells")
    else:
        _require(region_block["rate_sum"] == str(region_rate),
                 "region rate_sum mismatch")
        _require(region_block["variance"] == str(region_variance),
                 "region variance mismatch")
    return {
        "format": FORMAT, "request_id": request["request_id"],
        "dataset_id": request["dataset_id"], "revision": request["revision"],
        "region": dict(region), "frame": {"width": width, "height": height},
        "counts": counts,
        "rate_sum": str(region_rate) if counts["published"] else None,
        "variance": str(region_variance) if counts["published"] else None,
        "published_cells": [
            {"x": cell["x"], "y": cell["y"], "rate": cell["rate"],
             "variance": cell["variance"], "exposure_s": cell["exposure_s"],
             "contributor_count": cell["contributor_count"]}
            for cell in cells if cell["status"] == "published"]}


def _parse_frames(value, width, height):
    _require(isinstance(value, list), "frames must be an array")
    frames = []
    seen = set()
    for record in value:
        _exact_fields(record, FRAME_FIELDS, "frame")
        frame_id = record["frame_id"]
        _require(isinstance(frame_id, str) and frame_id.strip()
                 and len(frame_id) <= 200, "bad frame_id")
        _require(frame_id not in seen, "duplicate frame table entry")
        seen.add(frame_id)
        _positive_int(record["frame_version"], "frame_version")
        _require(1 <= record["exposure_s"] <= 86400,
                 "frame exposure_s outside the archive range")
        _require(type(record["dx"]) is int and -64 <= record["dx"] <= 64,
                 "frame dx must be an integer in [-64,64]")
        _require(type(record["dy"]) is int and -64 <= record["dy"] <= 64,
                 "frame dy must be an integer in [-64,64]")
        _require(1 <= record["saturation"] <= 10**9,
                 "frame saturation outside the archive range")
        _nonneg_int(record["observed_at_s"], "frame observed_at_s")
        _require(record["observed_at_s"] <= 10**12,
                 "frame observed_at_s outside the archive range")
        samples = record["samples"]
        _require(isinstance(samples, list) and samples,
                 "frame samples must be a non-empty array")
        positions = set()
        for sample in samples:
            _exact_fields(sample, FRAME_SAMPLE_FIELDS, "frame sample")
            x, y = _nonneg_int(sample["x"], "frame sample x"), \
                _nonneg_int(sample["y"], "frame sample y")
            _require(0 <= x < width and 0 <= y < height,
                     "frame sample outside the detector")
            _require((x, y) not in positions, "duplicate frame sample position")
            positions.add((x, y))
            _nonneg_int(sample["count"], "frame sample count")
            _require(sample["count"] <= 10**9,
                     "frame sample count outside the archive range")
            _nonneg_int(sample["variance"], "frame sample variance")
            _require(sample["variance"] <= 10**9,
                     "frame sample variance outside the archive range")
            _require(sample["count"] < record["saturation"],
                     "packed frame sample is saturated and cannot contribute")
            out_x, out_y = x + record["dx"], y + record["dy"]
            _require(0 <= out_x < width and 0 <= out_y < height,
                     "packed frame sample maps outside the frame")
        frames.append(record)
    return frames


def _parse_releases(value):
    _require(isinstance(value, list), "releases must be an array")
    releases = []
    by_id = {}
    seen = set()
    for record in value:
        _exact_fields(record, RELEASE_FIELDS, "release")
        release_id = record["id"]
        _require(isinstance(release_id, str) and release_id.strip()
                 and len(release_id) <= 200, "bad release id")
        _require(release_id not in seen, "duplicate release table entry")
        seen.add(release_id)
        _positive_int(record["version"], "release version")
        _require(record["version"] <= 10**12,
                 "release version outside the archive range")
        _nonneg_int(record["start_s"], "release start_s")
        _nonneg_int(record["end_s"], "release end_s")
        _require(record["start_s"] <= 10**12 and record["end_s"] <= 10**12,
                 "release window outside the archive range")
        _require(record["start_s"] < record["end_s"],
                 "release requires start_s < end_s")
        for name in ("dark_id", "flat_id"):
            _require(isinstance(record[name], str) and record[name].strip()
                     and len(record[name]) <= 200, f"bad release {name}")
        releases.append(record)
        by_id[release_id] = record
    return releases, by_id


def _parse_darks(value, width, height):
    _require(isinstance(value, list), "darks must be an array")
    darks = []
    by_id = {}
    seen = set()
    for record in value:
        _exact_fields(record, DARK_FIELDS, "dark")
        dark_id = record["id"]
        _require(isinstance(dark_id, str) and dark_id.strip()
                 and len(dark_id) <= 200, "bad dark id")
        _require(dark_id not in seen, "duplicate dark table entry")
        seen.add(dark_id)
        _positive_int(record["dark_exposure_s"], "dark_exposure_s")
        _require(1 <= record["dark_exposure_s"] <= 86400,
                 "dark_exposure_s outside the archive range")
        samples = record["samples"]
        _require(isinstance(samples, list) and samples,
                 "dark samples must be a non-empty array")
        positions = set()
        parsed_samples = {}
        for sample in samples:
            _exact_fields(sample, DARK_SAMPLE_FIELDS, "dark sample")
            x, y = _nonneg_int(sample["x"], "dark sample x"), \
                _nonneg_int(sample["y"], "dark sample y")
            _require(0 <= x < width and 0 <= y < height,
                     "dark sample outside the detector")
            _require((x, y) not in positions, "duplicate dark sample position")
            positions.add((x, y))
            _nonneg_int(sample["dark"], "dark sample count")
            _require(sample["dark"] <= 10**9,
                     "dark sample count outside the archive range")
            _nonneg_int(sample["dark_variance"], "dark sample variance")
            _require(sample["dark_variance"] <= 10**9,
                     "dark sample variance outside the archive range")
            parsed_samples[(x, y)] = sample
        darks.append(record)
        by_id[dark_id] = parsed_samples
    return darks, by_id


def _parse_flats(value, width, height):
    _require(isinstance(value, list), "flats must be an array")
    flats = []
    by_id = {}
    seen = set()
    for record in value:
        _exact_fields(record, FLAT_FIELDS, "flat")
        flat_id = record["id"]
        _require(isinstance(flat_id, str) and flat_id.strip()
                 and len(flat_id) <= 200, "bad flat id")
        _require(flat_id not in seen, "duplicate flat table entry")
        seen.add(flat_id)
        mean_flat = parse_fraction(record["mean_flat"], "flat mean_flat")
        _require(mean_flat > 0, "flat mean_flat must be positive")
        samples = record["samples"]
        _require(isinstance(samples, list) and samples,
                 "flat samples must be a non-empty array")
        positions = set()
        parsed_samples = {}
        for sample in samples:
            _exact_fields(sample, FLAT_SAMPLE_FIELDS, "flat sample")
            x, y = _nonneg_int(sample["x"], "flat sample x"), \
                _nonneg_int(sample["y"], "flat sample y")
            _require(0 <= x < width and 0 <= y < height,
                     "flat sample outside the detector")
            _require((x, y) not in positions, "duplicate flat sample position")
            positions.add((x, y))
            _require(type(sample["masked"]) is bool, "flat mask must be boolean")
            _require(sample["masked"] is False,
                     "masked flat samples cannot back published cells")
            _positive_int(sample["flat"], "flat normalization factor")
            _require(sample["flat"] <= 10**9,
                     "flat factor outside the archive range")
            parsed_samples[(x, y)] = sample
        flats.append(record)
        by_id[flat_id] = parsed_samples
    return flats, by_id


def _parse_contributions(value, frames, releases, darks, flats,
                         release_by_id, dark_by_id, flat_by_id, region):
    _require(isinstance(value, list), "contributions must be an array")
    frame_facts = {}
    release_facts = {}
    dark_facts = {}
    contributions = []
    seen_identity = set()
    for index, entry in enumerate(value):
        _exact_fields(entry, CONTRIBUTION_FIELDS, "contribution")
        for name in ("frame", "release", "dark", "flat"):
            _require(type(entry[name]) is int, f"contribution {name} index")
        for name in ("sample", "dark_sample", "flat_sample"):
            _nonneg_int(entry[name], f"contribution {name}")
        _require(0 <= entry["frame"] < len(frames), "frame index out of range")
        _require(0 <= entry["release"] < len(releases),
                 "release index out of range")
        _require(0 <= entry["dark"] < len(darks), "dark index out of range")
        _require(0 <= entry["flat"] < len(flats), "flat index out of range")
        frame = frames[entry["frame"]]
        _require(entry["sample"] < len(frame["samples"]),
                 "frame sample index out of range")
        frame_sample = frame["samples"][entry["sample"]]
        release = releases[entry["release"]]
        dark = darks[entry["dark"]]
        flat = flats[entry["flat"]]
        _require(entry["dark_sample"] < len(dark["samples"]),
                 "dark sample index out of range")
        _require(entry["flat_sample"] < len(flat["samples"]),
                 "flat sample index out of range")
        dark_sample = dark["samples"][entry["dark_sample"]]
        flat_sample = flat["samples"][entry["flat_sample"]]
        # every reference resolves through the release binding chain
        _require(release["dark_id"] == dark["id"],
                 "contribution dark does not match its bound release")
        _require(release["flat_id"] == flat["id"],
                 "contribution flat does not match its bound release")
        _require(dark_sample["x"] == frame_sample["x"]
                 and dark_sample["y"] == frame_sample["y"],
                 "dark sample detector coordinate does not match the frame sample")
        _require(flat_sample["x"] == frame_sample["x"]
                 and flat_sample["y"] == frame_sample["y"],
                 "flat sample detector coordinate does not match the frame sample")
        out_x = _nonneg_int(entry["output_x"], "contribution output_x")
        out_y = _nonneg_int(entry["output_y"], "contribution output_y")
        _require(region["x0"] <= out_x < region["x1"]
                 and region["y0"] <= out_y < region["y1"],
                 "contribution output lies outside the requested region")
        _require(out_x == frame_sample["x"] + frame["dx"]
                 and out_y == frame_sample["y"] + frame["dy"],
                 "contribution output disagrees with the integer registration")
        _require(release["start_s"] <= frame["observed_at_s"]
                 < release["end_s"],
                 "frame observed_at_s is outside its bound release window")
        identity = (frame["frame_id"], frame["frame_version"],
                    frame_sample["x"], frame_sample["y"])
        _require(identity not in seen_identity,
                 "duplicate contribution for the same frame version sample")
        seen_identity.add(identity)
        # identical frame-version facts must not disagree across cells
        fact_key = (frame["frame_id"], frame["frame_version"])
        frame_fact = {key: frame[key] for key in
                      ("exposure_s", "dx", "dy", "saturation", "observed_at_s")}
        if fact_key in frame_facts:
            _require(frame_facts[fact_key] == frame_fact,
                     "contradictory frame facts across contributions")
        else:
            frame_facts[fact_key] = frame_fact
        release_key = release["id"]
        release_fact = {key: release[key] for key in RELEASE_FIELDS}
        if release_key in release_facts:
            _require(release_facts[release_key] == release_fact,
                     "contradictory release facts across contributions")
        else:
            release_facts[release_key] = release_fact
        dark_key = (dark["id"], dark_sample["x"], dark_sample["y"])
        if dark_key in dark_facts:
            _require(dark_facts[dark_key] == (dark_sample["dark"],
                                              dark_sample["dark_variance"],
                                              dark["dark_exposure_s"]),
                     "contradictory dark sample facts across contributions")
        else:
            dark_facts[dark_key] = (dark_sample["dark"],
                                    dark_sample["dark_variance"],
                                    dark["dark_exposure_s"])
        contributions.append({
            "indexes": {"frame": entry["frame"], "sample": entry["sample"],
                        "release": entry["release"], "dark": entry["dark"],
                        "dark_sample": entry["dark_sample"],
                        "flat": entry["flat"],
                        "flat_sample": entry["flat_sample"]},
            "frame": frame, "frame_sample": frame_sample,
            "release": release, "dark": dark, "dark_sample": dark_sample,
            "flat": flat, "flat_sample": flat_sample,
            "output_x": out_x, "output_y": out_y})
    return contributions


def _check_table_reachability(document, contributions):
    """Every shared-table record must be referenced by a bound contribution."""
    if not contributions:
        for name in ("frames", "releases", "darks", "flats"):
            _require(document[name] == [],
                     f"unreferenced {name} record without published evidence")
        return
    for field, table in (("frame", document["frames"]),
                         ("release", document["releases"]),
                         ("dark", document["darks"]),
                         ("flat", document["flats"])):
        referenced = {entry["indexes"][field] for entry in contributions}
        _require(referenced == set(range(len(table))),
                 f"every {field} record must be bound to a published cell")
    for field, table, sample_field in (
            ("frame", document["frames"], "sample"),
            ("dark", document["darks"], "dark_sample"),
            ("flat", document["flats"], "flat_sample")):
        referenced = {(entry["indexes"][field], entry["indexes"][sample_field])
                      for entry in contributions}
        for index, record in enumerate(table):
            positions = {i for i, _ in enumerate(record["samples"])}
            used = {sample for table_index, sample in referenced
                    if table_index == index}
            _require(used == positions,
                     f"every {field} sample must back a published cell")


def _recompute(cells, contributions, frames, darks, flats, release_by_id,
               dark_by_id, flat_by_id, per_cell):
    computed = []
    rate_sum = Fraction(0)
    region_variance = Fraction(0)
    region_dark_g = {}
    counts = {"published": 0, "withheld": 0, "uncovered": 0}
    for cell in cells:
        if cell["status"] != "published":
            counts[cell["status"]] += 1
            computed.append(None)
            continue
        counts["published"] += 1
        total_exposure = 0
        weighted = Fraction(0)
        raw_accum = Fraction(0)
        dark_accum = {}
        for index in per_cell[(cell["x"], cell["y"])]:
            entry = contributions[index]
            frame = entry["frame"]
            frame_sample = entry["frame_sample"]
            dark = entry["dark"]
            dark_sample = entry["dark_sample"]
            flat = entry["flat"]
            flat_sample = entry["flat_sample"]
            mean_flat = parse_fraction(flat["mean_flat"], "flat mean_flat")
            exposure = frame["exposure_s"]
            dark_exposure = dark["dark_exposure_s"]
            flat_value = flat_sample["flat"]
            dark_rate = Fraction(dark_sample["dark"], dark_exposure)
            cal_rate = (Fraction(frame_sample["count"], exposure) - dark_rate) \
                * mean_flat / flat_value
            flat_factor = mean_flat / flat_value
            qf_term = Fraction(frame_sample["variance"]) * flat_factor ** 2
            dark_h = mean_flat / (flat_value * dark_exposure)
            total_exposure += exposure
            weighted += cal_rate * exposure
            raw_accum += qf_term
            source = (dark["id"], dark_sample["x"], dark_sample["y"])
            term = dark_h * exposure
            dark_accum[source] = term if source not in dark_accum \
                else dark_accum[source] + term
        total = Fraction(total_exposure)
        rate = weighted / total
        variance = raw_accum / total ** 2
        for (dark_id, det_x, det_y), numerator in dark_accum.items():
            g = numerator / total
            source_variance = dark_by_id[dark_id][(det_x, det_y)][
                "dark_variance"]
            variance += Fraction(source_variance) * g ** 2
            key = (dark_id, det_x, det_y)
            region_dark_g[key] = region_dark_g.get(key, Fraction(0)) + g
        region_variance += raw_accum / total ** 2
        rate_sum += rate
        computed.append({"rate": rate, "variance": variance,
                         "exposure_s": total_exposure,
                         "contributor_count": len(
                             per_cell[(cell["x"], cell["y"])])})
    for (dark_id, det_x, det_y), g in region_dark_g.items():
        source_variance = dark_by_id[dark_id][(det_x, det_y)][
            "dark_variance"]
        region_variance += Fraction(source_variance) * g ** 2
    return computed, rate_sum, region_variance, counts
