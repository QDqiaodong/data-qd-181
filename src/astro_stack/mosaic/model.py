"""Strict request validation for the sub-pixel mosaic mode.

Common-plane coordinates and per-frame corrections are decimal rational
strings ``n`` or ``n/d``; the numerator may be negative and need not be
reduced.  Plus signs, leading zeros, negative zero, decimals and whitespace
are all rejected, and both ``|numerator|`` and the positive denominator are
bounded by 10^12.  Integers reject bool, exactly like the rest of the
project.
"""
import re
from fractions import Fraction

from ..io import fields, label, rows

_LIMIT = 10**12
_SIGNED_RATIONAL = re.compile(r"(-?(?:0|[1-9][0-9]*))(?:/([1-9][0-9]*))?")


class MosaicError(ValueError):
    """Identifiable mosaic failure (bad request, source or geometry)."""


def rational(value, name):
    """Parse a signed rational string ``n`` or ``n/d`` under the project bounds."""
    if not isinstance(value, str):
        raise MosaicError(f"{name} requires rational string n or n/d")
    match = _SIGNED_RATIONAL.fullmatch(value)
    if not match:
        raise MosaicError(
            f"{name} requires rational string n or n/d without plus signs, "
            "leading zeros, decimals or whitespace")
    numerator_text = match.group(1)
    if numerator_text == "-0":
        raise MosaicError(f"{name} rejects negative zero")
    numerator = int(numerator_text)
    denominator = int(match.group(2)) if match.group(2) else 1
    if abs(numerator) > _LIMIT or denominator > _LIMIT:
        raise MosaicError(
            f"{name} requires |numerator| and denominator within 10^12")
    return Fraction(numerator, denominator)


def strict_int(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise MosaicError(f"{name} requires integer in [{minimum},{maximum}]")
    return value


def _source(value):
    if not isinstance(value, dict):
        raise MosaicError("sources row must be an object")
    fields(value, ("archive", "dataset_id", "revision", "origin_x",
                   "origin_y", "offsets"))
    archive = value["archive"]
    if not isinstance(archive, str) or not archive:
        raise MosaicError("archive requires a nonempty local path string")
    dataset_id = label(value["dataset_id"], "dataset_id")
    revision = strict_int(value["revision"], "revision", 0, _LIMIT)
    origin_x = rational(value["origin_x"], "origin_x")
    origin_y = rational(value["origin_y"], "origin_y")
    offset_rows = rows(value["offsets"], "offsets", 0, 50)
    offsets = {}
    for entry in offset_rows:
        if not isinstance(entry, dict):
            raise MosaicError("offsets row must be an object")
        fields(entry, ("frame_id", "frame_version", "dx", "dy"))
        frame_id = label(entry["frame_id"], "offsets.frame_id")
        if frame_id in offsets:
            raise MosaicError(f"duplicate offsets frame_id: {frame_id}")
        frame_version = strict_int(entry["frame_version"], "frame_version",
                                   1, _LIMIT)
        dx = rational(entry["dx"], "offsets.dx")
        dy = rational(entry["dy"], "offsets.dy")
        if not -1 <= dx <= 1 or not -1 <= dy <= 1:
            raise MosaicError("offsets dx/dy require range [-1,1]")
        offsets[frame_id] = (frame_version, dx, dy)
    return {"archive": archive, "dataset_id": dataset_id,
            "revision": revision, "origin_x": origin_x, "origin_y": origin_y,
            "offsets": offsets}


def _grid(value):
    if not isinstance(value, dict):
        raise MosaicError("grid must be an object")
    fields(value, ("x0", "y0", "width", "height"))
    return {"x0": rational(value["x0"], "grid.x0"),
            "y0": rational(value["y0"], "grid.y0"),
            "width": strict_int(value["width"], "grid.width", 1, 64),
            "height": strict_int(value["height"], "grid.height", 1, 64)}


def _region(value, seen):
    if not isinstance(value, dict):
        raise MosaicError("regions row must be an object")
    fields(value, ("id", "x0", "y0", "x1", "y1"))
    region_id = label(value["id"], "regions.id")
    if region_id in seen:
        raise MosaicError(f"duplicate region id: {region_id}")
    seen.add(region_id)
    x0 = rational(value["x0"], "regions.x0")
    y0 = rational(value["y0"], "regions.y0")
    x1 = rational(value["x1"], "regions.x1")
    y1 = rational(value["y1"], "regions.y1")
    if not x0 < x1 or not y0 < y1:
        raise MosaicError(f"region {region_id!r} requires a nonempty "
                          "half-open rectangle")
    return {"id": region_id, "x0": x0, "y0": y0, "x1": x1, "y1": y1}


def validate_specification(specification):
    """Parse and fully validate a mosaic request; archive facts are not read yet."""
    if not isinstance(specification, dict):
        raise MosaicError("specification must be an object")
    fields(specification, ("sources", "grid", "regions", "min_visits"))
    source_rows = rows(specification["sources"], "sources", 1, 8)
    sources = []
    seen_datasets = set()
    for row in source_rows:
        source = _source(row)
        if source["dataset_id"] in seen_datasets:
            raise MosaicError(f"duplicate dataset_id: {source['dataset_id']}")
        seen_datasets.add(source["dataset_id"])
        sources.append(source)
    grid = _grid(specification["grid"])
    region_rows = rows(specification["regions"], "regions", 0, 16)
    seen_regions = set()
    regions = [_region(row, seen_regions) for row in region_rows]
    min_visits = strict_int(specification["min_visits"], "min_visits",
                            1, len(sources))
    return {"sources": sources, "grid": grid, "regions": regions,
            "min_visits": min_visits}
