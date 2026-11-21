"""Image coordinates are detector row/column, not celestial coordinates."""
import re
from fractions import Fraction

from .io import fields, integer, records, rows

_RATIONAL = re.compile(r"(0|[1-9][0-9]*)(?:/([1-9][0-9]*))?")


def matrix(value, width, height, name, boolean=False, maximum=10**9):
    rows(value, name, height, height)
    for row in value:
        rows(row, name, width, width)
        for pixel in row:
            if boolean:
                if type(pixel) is not bool:
                    raise ValueError(f"{name} requires boolean pixels")
            else:
                integer(pixel, name, 0, maximum)


def rational(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} requires rational string n or n/d")
    match = _RATIONAL.fullmatch(value)
    if not match:
        raise ValueError(f"{name} requires rational string n or n/d without sign, whitespace or decimals")
    numerator = int(match.group(1))
    denominator = int(match.group(2)) if match.group(2) else 1
    if numerator > 10**12 or denominator > 10**12:
        raise ValueError(f"{name} requires numerator and denominator within 0..10^12")
    return Fraction(numerator, denominator)


def _validate_campaign(data, width, height, frames, apertures):
    campaign = data["campaign"]
    fields(campaign, ("dark_variance", "products"))
    matrix(campaign["dark_variance"], width, height, "dark_variance")
    product_rows = rows(campaign["products"], "products", 1, 4)
    products = records(campaign["products"], "product", 1, 4)
    parsed = []
    for product in product_rows:
        fields(product, ("id", "start_s", "end_s", "priority", "max_frames", "requirements"))
        start_s = integer(product["start_s"], "start_s", 0, 10**12)
        end_s = integer(product["end_s"], "end_s", 0, 10**12)
        if start_s >= end_s:
            raise ValueError("product requires start_s < end_s")
        priority = integer(product["priority"], "priority", 1, 10**6)
        max_frames = integer(product["max_frames"], "max_frames", 1, 4)
        requirement_rows = rows(product["requirements"], "requirements", 1, 8)
        requirements = []
        seen_apertures = set()
        for requirement in requirement_rows:
            if not isinstance(requirement, dict):
                raise ValueError("requirement row must be an object")
            fields(requirement, ("aperture_id", "min_exposure_s", "max_variance"))
            aperture_id = requirement["aperture_id"]
            if not isinstance(aperture_id, str) or aperture_id not in apertures:
                raise ValueError(f"requirement aperture_id unknown: {aperture_id!r}")
            if aperture_id in seen_apertures:
                raise ValueError(f"duplicate requirement aperture_id: {aperture_id}")
            seen_apertures.add(aperture_id)
            min_exposure_s = integer(requirement["min_exposure_s"], "min_exposure_s", 1, 345600)
            max_variance = rational(requirement["max_variance"], "max_variance")
            requirements.append({"aperture_id": aperture_id,
                                 "min_exposure_s": min_exposure_s,
                                 "max_variance": max_variance})
        parsed.append({"id": product["id"], "start_s": start_s, "end_s": end_s,
                       "priority": priority, "max_frames": max_frames,
                       "requirements": requirements})
    return {"dark_variance": campaign["dark_variance"], "products": parsed}


def validate(data):
    fields(data, ("width", "height", "dark_exposure_s", "dark", "flat", "bad_mask",
                  "frames", "apertures"), ("campaign",))
    width = integer(data["width"], "width", 1, 64)
    height = integer(data["height"], "height", 1, 64)
    integer(data["dark_exposure_s"], "dark_exposure_s", 1, 86400)
    for name in ("dark", "flat"):
        matrix(data[name], width, height, name)
    matrix(data["bad_mask"], width, height, "bad_mask", True)
    usable = [data["flat"][y][x] for y in range(height) for x in range(width)
              if not data["bad_mask"][y][x] and data["flat"][y][x] > 0]
    if not usable:
        raise ValueError("flat has no usable unmasked positive pixels")
    campaign_mode = "campaign" in data
    frame_limit = 14 if campaign_mode else 50
    frame_rows = rows(data["frames"], "frames", 1, frame_limit)
    frames = records(data["frames"], "frame", 1, frame_limit)
    frame_fields = ("id", "exposure_s", "dx", "dy", "saturation", "pixels")
    if campaign_mode:
        frame_fields += ("observed_at_s", "variance")
    for frame in frame_rows:
        fields(frame, frame_fields)
        integer(frame["exposure_s"], "exposure_s", 1, 86400)
        integer(frame["dx"], "dx", -64, 64)
        integer(frame["dy"], "dy", -64, 64)
        integer(frame["saturation"], "saturation", 1, 10**9)
        matrix(frame["pixels"], width, height, "frame pixels")
        if campaign_mode:
            integer(frame["observed_at_s"], "observed_at_s", 0, 10**12)
            matrix(frame["variance"], width, height, "frame variance")
    apertures = records(data["apertures"], "aperture", 0, 100)
    for aperture in apertures.values():
        fields(aperture, ("id", "x", "y", "radius"))
        integer(aperture["x"], "x", 0, width - 1)
        integer(aperture["y"], "y", 0, height - 1)
        integer(aperture["radius"], "radius", 0, 64)
    campaign = _validate_campaign(data, width, height, frames, apertures) if campaign_mode else None
    return width, height, frames, apertures, usable, campaign
