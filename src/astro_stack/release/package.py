"""Deterministic, versioned release package format: packing and offline verify.

The package is one canonical JSON document (sorted object keys, compact
separators, no wall-clock time, no source paths).  Its exact bytes are hashed
for the receipt and re-derived by the offline verifier.  Every shared table
entry is referenced by at least one published cell; withheld and uncovered
cells carry no pixel evidence.
"""
import hashlib
import json
import re
from fractions import Fraction

from ..archive.science import select_bindings

FORMAT = "astro-release-package/v1"
_RATIONAL = re.compile(r"(0|[1-9][0-9]*)(?:/([1-9][0-9]*))?")
_MAX_RATIONAL_DIGITS = 200


class PackageError(ValueError):
    """Raised when a package cannot be parsed or fails offline recomputation."""


def canonical_bytes(document):
    """The canonical on-disk bytes of a package document."""
    return json.dumps(document, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def parse_fraction(text, name):
    """Parse an exact, reduced non-negative rational string without the archive's
    10^12 input ceiling (package values are derived quantities)."""
    if not isinstance(text, str) or len(text) > _MAX_RATIONAL_DIGITS:
        raise PackageError(f"{name} requires a rational string")
    match = _RATIONAL.fullmatch(text)
    if not match:
        raise PackageError(f"{name} requires a rational string n or n/d")
    value = Fraction(int(match.group(1)),
                     int(match.group(2)) if match.group(2) else 1)
    if str(value) != text:
        raise PackageError(f"{name} must be a reduced fraction string")
    return value


# -------------------------------------------------------------------------- pack
def collect_region(snapshot, request, denied):
    """Region-local per-frame contributions following the classic stack rules.

    Returns (accepted, blocked): accepted maps an output coordinate to the
    accepted contribution facts, blocked marks outputs hit by a denied sample.
    Masked, zero-flat, saturated and out-of-frame samples (including denied
    ones) never influence any output.
    """
    if snapshot.products is not None:
        raise ValueError("release supports classic archives only; campaign "
                         "definitions are not accepted")
    width, height = snapshot.width, snapshot.height
    region = request["region"]
    if not (0 <= region["x0"] < region["x1"] <= width
            and 0 <= region["y0"] < region["y1"] <= height):
        raise ValueError("region must lie fully inside the archive frame")
    bindings = select_bindings(snapshot)
    ready = {frame_id: snapshot.frames[frame_id]
             for frame_id, release in bindings.items()
             if release is not None and not release["retracted"]}
    accepted = {}
    blocked = set()
    for frame_id in sorted(ready):
        frame = ready[frame_id]
        release = bindings[frame_id]
        dark = snapshot.darks[release["dark_id"]]
        flat_record = snapshot.flats[release["flat_id"]]
        mean_flat = flat_record["mean_flat"]
        dark_exposure = dark["dark_exposure_s"]
        exposure = frame["exposure_s"]
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
                out_x, out_y = x + frame["dx"], y + frame["dy"]
                if not (region["x0"] <= out_x < region["x1"]
                        and region["y0"] <= out_y < region["y1"]):
                    continue
                if (frame_id, frame["frame_version"], x, y) in denied:
                    blocked.add((out_x, out_y))
                    continue
                dark_rate = Fraction(dark["dark"][y][x], dark_exposure)
                cal_rate = (Fraction(pixel, exposure) - dark_rate) \
                    * mean_flat / flat
                flat_factor = mean_flat / flat
                qf_term = Fraction(frame["variance"][y][x]) * flat_factor ** 2
                dark_h = mean_flat / (flat * dark_exposure)
                accepted.setdefault((out_x, out_y), []).append({
                    "frame_id": frame_id, "frame": frame, "release": release,
                    "dark": dark, "flat_record": flat_record,
                    "mean_flat": mean_flat, "x": x, "y": y,
                    "out_x": out_x, "out_y": out_y,
                    "pixel": pixel, "raw_variance": frame["variance"][y][x],
                    "exposure": exposure, "cal_rate": cal_rate,
                    "qf_term": qf_term, "dark_h": dark_h,
                    "dark_variance": dark["dark_variance"][y][x]})
    return accepted, blocked


def build_package(snapshot, request, denied):
    """Pack the deliverable region plus exactly the evidence published cells need."""
    accepted, blocked = collect_region(snapshot, request, denied)
    region = request["region"]
    frame_samples = {}
    frame_table = {}
    frame_records = {}
    dark_samples = {}
    dark_table = {}
    dark_records = {}
    flat_samples = {}
    flat_table = {}
    flat_mean = {}
    release_records = {}
    raw_contributions = []

    def intern_frame(c):
        frame = c["frame"]
        frame_id = frame["id"]
        if frame_id not in frame_table:
            frame_table[frame_id] = {
                "frame_id": frame_id,
                "frame_version": frame["frame_version"],
                "exposure_s": frame["exposure_s"],
                "dx": frame["dx"], "dy": frame["dy"],
                "saturation": frame["saturation"],
                "observed_at_s": frame["observed_at_s"], "samples": []}
            frame_records[frame_id] = frame
            frame_samples[frame_id] = {}
        position = (c["x"], c["y"])
        if position not in frame_samples[frame_id]:
            frame_samples[frame_id][position] = {
                "x": c["x"], "y": c["y"], "count": c["pixel"],
                "variance": c["raw_variance"]}

    def intern_dark(c):
        dark = c["dark"]
        dark_id = dark["id"]
        if dark_id not in dark_table:
            dark_table[dark_id] = {
                "id": dark_id,
                "dark_exposure_s": dark["dark_exposure_s"], "samples": []}
            dark_records[dark_id] = dark
            dark_samples[dark_id] = {}
        position = (c["x"], c["y"])
        if position not in dark_samples[dark_id]:
            dark_samples[dark_id][position] = {
                "x": c["x"], "y": c["y"],
                "dark": dark["dark"][c["y"]][c["x"]],
                "dark_variance": c["dark_variance"]}

    def intern_flat(c):
        flat_record = c["flat_record"]
        flat_id = flat_record["id"]
        if flat_id not in flat_table:
            flat_table[flat_id] = {
                "id": flat_id, "mean_flat": str(c["mean_flat"]),
                "samples": []}
            flat_mean[flat_id] = c["mean_flat"]
            flat_samples[flat_id] = {}
        position = (c["x"], c["y"])
        if position not in flat_samples[flat_id]:
            flat_samples[flat_id][position] = {
                "x": c["x"], "y": c["y"],
                "flat": flat_record["flat"][c["y"]][c["x"]], "masked": False}

    def intern(c, out_x, out_y):
        intern_frame(c)
        intern_dark(c)
        intern_flat(c)
        release_records[c["release"]["id"]] = c["release"]
        raw_contributions.append({
            "frame_id": c["frame_id"], "sample_key": (c["x"], c["y"]),
            "release_id": c["release"]["id"], "dark_id": c["dark"]["id"],
            "flat_id": c["flat_record"]["id"],
            "output_x": out_x, "output_y": out_y,
            "exposure": c["exposure"], "cal_rate": c["cal_rate"],
            "qf_term": c["qf_term"], "dark_h": c["dark_h"],
            "dark_variance": c["dark_variance"],
            "dark_key": (c["dark"]["id"], c["x"], c["y"])})

    cells = []
    counts = {"published": 0, "withheld": 0, "uncovered": 0}
    rate_sum = Fraction(0)
    region_variance = Fraction(0)
    region_dark_g = {}
    for out_y in range(region["y0"], region["y1"]):
        for out_x in range(region["x0"], region["x1"]):
            cell_contributions = accepted.get((out_x, out_y))
            if not cell_contributions:
                status = "withheld" if (out_x, out_y) in blocked else "uncovered"
                cells.append({"x": out_x, "y": out_y, "status": status})
                counts[status] += 1
                continue
            if (out_x, out_y) in blocked:
                cells.append({"x": out_x, "y": out_y, "status": "withheld"})
                counts["withheld"] += 1
                continue
            total_exposure = 0
            weighted = Fraction(0)
            raw_accum = Fraction(0)
            dark_accum = {}
            keys = []
            for c in cell_contributions:
                intern(c, out_x, out_y)
                keys.append((c["frame_id"], c["y"], c["x"]))
                total_exposure += c["exposure"]
                weighted += c["cal_rate"] * c["exposure"]
                raw_accum += c["qf_term"]
                source = (c["dark"]["id"], c["x"], c["y"])
                term = c["dark_h"] * c["exposure"]
                dark_accum[source] = term if source not in dark_accum \
                    else dark_accum[source] + term
            total = Fraction(total_exposure)
            rate = weighted / total
            variance = raw_accum / total ** 2
            for (dark_id, det_x, det_y), numerator in dark_accum.items():
                g = numerator / total
                source_variance = dark_samples[dark_id][(det_x, det_y)][
                    "dark_variance"]
                variance += Fraction(source_variance) * g ** 2
                key = (dark_id, det_x, det_y)
                region_dark_g[key] = region_dark_g.get(key, Fraction(0)) + g
            region_variance += raw_accum / total ** 2
            rate_sum += rate
            counts["published"] += 1
            cells.append({
                "x": out_x, "y": out_y, "status": "published",
                "rate": str(rate), "variance": str(variance),
                "exposure_s": total_exposure,
                "contributor_count": len(cell_contributions),
                "_contribution_keys": sorted(keys)})
    for (dark_id, det_x, det_y), g in region_dark_g.items():
        source_variance = dark_samples[dark_id][(det_x, det_y)][
            "dark_variance"]
        region_variance += Fraction(source_variance) * g ** 2

    frame_order = sorted(frame_samples)
    dark_order = sorted(dark_samples)
    flat_order = sorted(flat_samples)
    release_order = sorted(release_records)
    frame_pos = {key: i for i, key in enumerate(frame_order)}
    dark_pos = {key: i for i, key in enumerate(dark_order)}
    flat_pos = {key: i for i, key in enumerate(flat_order)}
    release_pos = {key: i for i, key in enumerate(release_order)}
    sample_index = {}
    for frame_id, samples in frame_samples.items():
        sample_index[frame_id] = {
            position: i for i, position in enumerate(sorted(samples))}
    for dark_id, samples in dark_samples.items():
        sample_index[dark_id] = {
            position: i for i, position in enumerate(sorted(samples))}
    for flat_id, samples in flat_samples.items():
        sample_index[flat_id] = {
            position: i for i, position in enumerate(sorted(samples))}
    raw_contributions.sort(key=lambda entry: (
        entry["frame_id"], entry["output_y"], entry["output_x"],
        entry["sample_key"][1], entry["sample_key"][0]))
    contribution_pos = {
        (entry["frame_id"], entry["sample_key"][1], entry["sample_key"][0]): i
        for i, entry in enumerate(raw_contributions)}
    contributions = [{
        "frame": frame_pos[entry["frame_id"]],
        "sample": sample_index[entry["frame_id"]][entry["sample_key"]],
        "release": release_pos[entry["release_id"]],
        "dark": dark_pos[entry["dark_id"]],
        "dark_sample": sample_index[entry["dark_id"]][
            (entry["sample_key"][0], entry["sample_key"][1])],
        "flat": flat_pos[entry["flat_id"]],
        "flat_sample": sample_index[entry["flat_id"]][
            (entry["sample_key"][0], entry["sample_key"][1])],
        "output_x": entry["output_x"], "output_y": entry["output_y"]}
        for entry in raw_contributions]
    frames = []
    for frame_id in frame_order:
        frame = frame_records[frame_id]
        frames.append({
            "frame_id": frame_id, "frame_version": frame["frame_version"],
            "exposure_s": frame["exposure_s"], "dx": frame["dx"],
            "dy": frame["dy"], "saturation": frame["saturation"],
            "observed_at_s": frame["observed_at_s"],
            "samples": [frame_samples[frame_id][position]
                        for position in sorted(frame_samples[frame_id])]})
    darks = [{
        "id": dark_id,
        "dark_exposure_s": dark_records[dark_id]["dark_exposure_s"],
        "samples": [dark_samples[dark_id][position]
                    for position in sorted(dark_samples[dark_id])]}
        for dark_id in dark_order]
    flats = [{
        "id": flat_id, "mean_flat": str(flat_mean[flat_id]),
        "samples": [flat_samples[flat_id][position]
                    for position in sorted(flat_samples[flat_id])]}
        for flat_id in flat_order]
    releases = [{"id": release_id,
                 "version": release_records[release_id]["version"],
                 "start_s": release_records[release_id]["start_s"],
                 "end_s": release_records[release_id]["end_s"],
                 "dark_id": release_records[release_id]["dark_id"],
                 "flat_id": release_records[release_id]["flat_id"]}
                for release_id in release_order]
    for cell in cells:
        if cell["status"] == "published":
            cell["contributions"] = sorted(
                contribution_pos[key] for key in cell.pop("_contribution_keys"))
    document = {
        "format": FORMAT,
        "request": {
            "request_id": request["request_id"],
            "dataset_id": request["dataset_id"],
            "revision": request["revision"],
            "region": {key: region[key]
                      for key in ("x0", "y0", "x1", "y1")}},
        "frame": {"width": snapshot.width, "height": snapshot.height},
        "cells": cells,
        "region": {
            "rate_sum": str(rate_sum) if counts["published"] else None,
            "variance": str(region_variance) if counts["published"] else None,
            "counts": dict(counts)},
        "frames": frames, "releases": releases, "darks": darks,
        "flats": flats, "contributions": contributions}
    return document
