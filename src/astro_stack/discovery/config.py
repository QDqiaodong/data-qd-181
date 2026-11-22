"""Strict validation for time-series discovery workspace configuration."""
from ..io import fields
from ..model import rational

CONFIG_FIELDS = ("aperture_ids", "bootstrap_frames", "reference_capacity",
                 "min_exposure_s", "min_abs_delta", "sigma_squared")


def _strict_int(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} requires integer in [{minimum},{maximum}]")
    return value


def _positive_rational(value, name):
    parsed = rational(value, name)
    if parsed <= 0:
        raise ValueError(f"{name} must be strictly positive")
    return parsed


def validate_config(value, archive_aperture_ids):
    """Validate a discovery config against the fixed source archive definition.

    Returns a normalized, JSON-ready stored copy: aperture ids are sorted by
    aperture id (Unicode code point order); rational strings are preserved.
    """
    if not isinstance(value, dict):
        raise ValueError("config must be an object")
    fields(value, CONFIG_FIELDS)
    aperture_ids = value["aperture_ids"]
    if not isinstance(aperture_ids, list) or not 1 <= len(aperture_ids) <= 8:
        raise ValueError("aperture_ids requires an array of 1..8 entries")
    known = set(archive_aperture_ids)
    seen = set()
    for aperture_id in aperture_ids:
        if not isinstance(aperture_id, str) or aperture_id not in known:
            raise ValueError(f"aperture_ids references unknown aperture: {aperture_id!r}")
        if aperture_id in seen:
            raise ValueError(f"duplicate aperture id in config: {aperture_id}")
        seen.add(aperture_id)
    bootstrap_frames = _strict_int(value["bootstrap_frames"],
                                   "bootstrap_frames", 1, 8)
    reference_capacity = _strict_int(value["reference_capacity"],
                                     "reference_capacity", bootstrap_frames, 8)
    min_exposure_s = _strict_int(value["min_exposure_s"],
                                 "min_exposure_s", 1, 86400)
    # parse first so malformed rational strings are rejected identically
    min_abs_delta = _positive_rational(value["min_abs_delta"], "min_abs_delta")
    sigma_squared = _positive_rational(value["sigma_squared"], "sigma_squared")
    return {
        "aperture_ids": sorted(seen),
        "bootstrap_frames": bootstrap_frames,
        "reference_capacity": reference_capacity,
        "min_exposure_s": min_exposure_s,
        "min_abs_delta": str(min_abs_delta),
        "sigma_squared": str(sigma_squared)}
