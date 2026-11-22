"""Strict validation for release requests and delivery licenses."""
from ..io import fields, label, rows

MAX_DENIED = 50_000
INTEGER_CEILING = 10**12


def _strict_int(value, name, minimum, maximum=INTEGER_CEILING):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} requires integer in [{minimum},{maximum}]")
    return value


def validate_request(value):
    """Parse and fully validate a release request; return a canonical copy."""
    if not isinstance(value, dict):
        raise ValueError("request must be an object")
    fields(value, ("request_id", "dataset_id", "revision", "region"))
    request_id = label(value["request_id"], "request_id")
    dataset_id = label(value["dataset_id"], "dataset_id")
    revision = _strict_int(value["revision"], "revision", 0)
    region = value["region"]
    if not isinstance(region, dict):
        raise ValueError("region must be an object")
    fields(region, ("x0", "y0", "x1", "y1"))
    bounds = {}
    for name in ("x0", "y0", "x1", "y1"):
        value = region[name]
        if type(value) is not int or not -10**12 <= value <= 10**12:
            raise ValueError(
                f"region.{name} requires integer in [-10^12,10^12]")
        bounds[name] = value
    if any(bounds[name] < 0 for name in ("x0", "y0", "x1", "y1")):
        raise ValueError("region coordinates must be non-negative")
    if not (bounds["x1"] > bounds["x0"] and bounds["y1"] > bounds["y0"]):
        raise ValueError("region must be a non-empty half-open rectangle")
    return {"request_id": request_id, "dataset_id": dataset_id,
            "revision": revision,
            "region": {name: bounds[name]
                       for name in ("x0", "y0", "x1", "y1")}}


def validate_license(value):
    """Parse and fully validate a license; return the exact denied identity set."""
    if not isinstance(value, dict):
        raise ValueError("license must be an object")
    fields(value, ("dataset_id", "denied"))
    dataset_id = label(value["dataset_id"], "license.dataset_id")
    rows(value["denied"], "denied", 0, MAX_DENIED)
    denied = set()
    for entry in value["denied"]:
        if not isinstance(entry, dict):
            raise ValueError("denied row must be an object")
        fields(entry, ("frame_id", "frame_version", "x", "y"))
        frame_id = label(entry["frame_id"], "denied.frame_id")
        frame_version = _strict_int(entry["frame_version"],
                                    "denied.frame_version", 1)
        x = _strict_int(entry["x"], "denied.x", 0)
        y = _strict_int(entry["y"], "denied.y", 0)
        identity = (frame_id, frame_version, x, y)
        if identity in denied:
            raise ValueError(f"duplicate denied identity: {identity}")
        denied.add(identity)
    return {"dataset_id": dataset_id, "denied": denied}
