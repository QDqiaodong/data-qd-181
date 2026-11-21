"""Strict schema validation for local calibration archive inputs."""
from fractions import Fraction

from ..io import fields, integer, label, rows
from ..model import matrix, rational

BATCH_LIMIT = 50
CLASSIC_FRAME_LIMIT = 50
CAMPAIGN_FRAME_LIMIT = 14
FRAME_FIELDS = ("id", "exposure_s", "dx", "dy", "saturation", "pixels",
                "observed_at_s", "variance", "expected_version")
BATCH_FIELDS = ("batch_id", "expected_revision", "add_darks", "add_flats",
                "add_releases", "retract_releases", "put_frames",
                "withdraw_frames")


def strict_int(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} requires integer in [{minimum},{maximum}]")
    return value


def _aperture(value, width, height, seen):
    if not isinstance(value, dict):
        raise ValueError("aperture must be an object")
    fields(value, ("id", "x", "y", "radius"))
    aperture_id = label(value["id"], "aperture.id")
    if aperture_id in seen:
        raise ValueError(f"duplicate aperture id: {aperture_id}")
    seen.add(aperture_id)
    strict_int(value["x"], "aperture.x", 0, width - 1)
    strict_int(value["y"], "aperture.y", 0, height - 1)
    strict_int(value["radius"], "aperture.radius", 0, 64)
    return {"id": aperture_id, "x": value["x"], "y": value["y"],
            "radius": value["radius"]}


def _product(value, aperture_ids, seen):
    if not isinstance(value, dict):
        raise ValueError("product must be an object")
    fields(value, ("id", "start_s", "end_s", "priority", "max_frames",
                   "requirements"))
    product_id = label(value["id"], "product.id")
    if product_id in seen:
        raise ValueError(f"duplicate product id: {product_id}")
    seen.add(product_id)
    start_s = strict_int(value["start_s"], "start_s", 0, 10**12)
    end_s = strict_int(value["end_s"], "end_s", 0, 10**12)
    if start_s >= end_s:
        raise ValueError("product requires start_s < end_s")
    strict_int(value["priority"], "priority", 1, 10**6)
    strict_int(value["max_frames"], "max_frames", 1, 4)
    requirement_rows = rows(value["requirements"], "requirements", 1, 8)
    requirements = []
    seen_apertures = set()
    for requirement in requirement_rows:
        if not isinstance(requirement, dict):
            raise ValueError("requirement row must be an object")
        fields(requirement, ("aperture_id", "min_exposure_s", "max_variance"))
        aperture_id = requirement["aperture_id"]
        if not isinstance(aperture_id, str) or aperture_id not in aperture_ids:
            raise ValueError(f"requirement aperture_id unknown: {aperture_id!r}")
        if aperture_id in seen_apertures:
            raise ValueError(f"duplicate requirement aperture_id: {aperture_id}")
        seen_apertures.add(aperture_id)
        min_exposure_s = strict_int(requirement["min_exposure_s"],
                                    "min_exposure_s", 1, 345600)
        max_variance = rational(requirement["max_variance"], "max_variance")
        requirements.append({"aperture_id": aperture_id,
                             "min_exposure_s": min_exposure_s,
                             # store the exact rational as a canonical string
                             "max_variance": str(max_variance)})
    return {"id": product_id, "start_s": start_s, "end_s": end_s,
            "priority": value["priority"], "max_frames": value["max_frames"],
            "requirements": requirements}


def validate_definition(value):
    """Parse and fully validate an archive definition; return a stored copy."""
    if not isinstance(value, dict):
        raise ValueError("definition must be an object")
    fields(value, ("dataset_id", "width", "height", "apertures", "products"))
    dataset_id = label(value["dataset_id"], "dataset_id")
    width = strict_int(value["width"], "width", 1, 64)
    height = strict_int(value["height"], "height", 1, 64)
    aperture_rows = rows(value["apertures"], "apertures", 0, 100)
    aperture_ids = set()
    apertures = [_aperture(row, width, height, aperture_ids)
                 for row in aperture_rows]
    products = None
    if value["products"] is not None:
        product_rows = rows(value["products"], "products", 1, 4)
        seen_products = set()
        products = [_product(row, aperture_ids, seen_products)
                    for row in product_rows]
    return {"dataset_id": dataset_id, "width": width, "height": height,
            "apertures": apertures, "products": products}


def _dark_record(value, width, height):
    if not isinstance(value, dict):
        raise ValueError("add_darks row must be an object")
    fields(value, ("id", "dark_exposure_s", "dark", "dark_variance"))
    dark_id = label(value["id"], "dark.id")
    exposure = strict_int(value["dark_exposure_s"], "dark_exposure_s", 1, 86400)
    matrix(value["dark"], width, height, "dark")
    matrix(value["dark_variance"], width, height, "dark_variance")
    return {"id": dark_id, "dark_exposure_s": exposure,
            "dark": value["dark"], "dark_variance": value["dark_variance"]}


def _flat_record(value, width, height):
    if not isinstance(value, dict):
        raise ValueError("add_flats row must be an object")
    fields(value, ("id", "flat", "bad_mask"))
    flat_id = label(value["id"], "flat.id")
    matrix(value["flat"], width, height, "flat")
    matrix(value["bad_mask"], width, height, "bad_mask", boolean=True)
    usable = [value["flat"][y][x] for y in range(height) for x in range(width)
              if not value["bad_mask"][y][x] and value["flat"][y][x] > 0]
    if not usable:
        raise ValueError("flat has no usable unmasked positive pixels")
    mean_flat = Fraction(sum(usable), len(usable))
    return {"id": flat_id, "flat": value["flat"],
            "bad_mask": value["bad_mask"], "mean_flat": mean_flat}


def _release_record(value):
    if not isinstance(value, dict):
        raise ValueError("add_releases row must be an object")
    fields(value, ("id", "version", "start_s", "end_s", "dark_id", "flat_id"))
    release_id = label(value["id"], "release.id")
    version = strict_int(value["version"], "version", 1, 10**12)
    start_s = strict_int(value["start_s"], "start_s", 0, 10**12)
    end_s = strict_int(value["end_s"], "end_s", 0, 10**12)
    if start_s >= end_s:
        raise ValueError("release requires start_s < end_s")
    dark_id = label(value["dark_id"], "release.dark_id")
    flat_id = label(value["flat_id"], "release.flat_id")
    return {"id": release_id, "version": version, "start_s": start_s,
            "end_s": end_s, "dark_id": dark_id, "flat_id": flat_id}


def _frame_record(value, width, height):
    if not isinstance(value, dict):
        raise ValueError("put_frames row must be an object")
    fields(value, FRAME_FIELDS)
    frame_id = label(value["id"], "frame.id")
    strict_int(value["exposure_s"], "exposure_s", 1, 86400)
    strict_int(value["dx"], "dx", -64, 64)
    strict_int(value["dy"], "dy", -64, 64)
    strict_int(value["saturation"], "saturation", 1, 10**9)
    matrix(value["pixels"], width, height, "frame pixels")
    strict_int(value["observed_at_s"], "observed_at_s", 0, 10**12)
    matrix(value["variance"], width, height, "frame variance")
    strict_int(value["expected_version"], "expected_version", 0, 10**12)
    return {"id": frame_id, "exposure_s": value["exposure_s"],
            "dx": value["dx"], "dy": value["dy"],
            "saturation": value["saturation"], "pixels": value["pixels"],
            "observed_at_s": value["observed_at_s"],
            "variance": value["variance"],
            "expected_version": value["expected_version"]}


def validate_batch(value, width, height):
    """Parse and shape-validate a batch; cross-record state checks live in the store."""
    if not isinstance(value, dict):
        raise ValueError("batch must be an object")
    fields(value, BATCH_FIELDS)
    batch_id = label(value["batch_id"], "batch_id")
    strict_int(value["expected_revision"], "expected_revision", 0, 10**12)
    arrays = {}
    for name in ("add_darks", "add_flats", "add_releases", "retract_releases",
                 "put_frames", "withdraw_frames"):
        if not isinstance(value[name], list):
            raise ValueError(f"{name} requires an array")
        if len(value[name]) > BATCH_LIMIT:
            raise ValueError(f"{name} allows at most {BATCH_LIMIT} entries")
    if all(not value[name] for name in BATCH_FIELDS[2:]):
        raise ValueError("batch must contain at least one change")
    darks = []
    seen = set()
    for row in value["add_darks"]:
        record = _dark_record(row, width, height)
        if record["id"] in seen:
            raise ValueError(f"duplicate add_darks id: {record['id']}")
        seen.add(record["id"])
        darks.append(record)
    flats = []
    seen = set()
    for row in value["add_flats"]:
        record = _flat_record(row, width, height)
        if record["id"] in seen:
            raise ValueError(f"duplicate add_flats id: {record['id']}")
        seen.add(record["id"])
        flats.append(record)
    releases = []
    seen = set()
    for row in value["add_releases"]:
        record = _release_record(row)
        if record["id"] in seen:
            raise ValueError(f"duplicate add_releases id: {record['id']}")
        seen.add(record["id"])
        releases.append(record)
    retractions = []
    seen = set()
    for row in value["retract_releases"]:
        release_id = label(row, "retract_releases.id")
        if release_id in seen:
            raise ValueError(f"duplicate retract_releases id: {release_id}")
        seen.add(release_id)
        retractions.append(release_id)
    frames = []
    seen = set()
    for row in value["put_frames"]:
        record = _frame_record(row, width, height)
        if record["id"] in seen:
            raise ValueError(f"duplicate put_frames id: {record['id']}")
        seen.add(record["id"])
        frames.append(record)
    withdrawals = []
    seen = set()
    for row in value["withdraw_frames"]:
        if not isinstance(row, dict):
            raise ValueError("withdraw_frames row must be an object")
        fields(row, ("id", "expected_version"))
        frame_id = label(row["id"], "withdraw_frames.id")
        if frame_id in seen:
            raise ValueError(f"duplicate withdraw_frames id: {frame_id}")
        seen.add(frame_id)
        strict_int(row["expected_version"], "expected_version", 0, 10**12)
        withdrawals.append({"id": frame_id,
                            "expected_version": row["expected_version"]})
    put_ids = {record["id"] for record in frames}
    withdraw_ids = {record["id"] for record in withdrawals}
    overlap = put_ids & withdraw_ids
    if overlap:
        raise ValueError(f"frame id in both put_frames and withdraw_frames: "
                         f"{sorted(overlap)[0]}")
    return {"batch_id": batch_id,
            "expected_revision": value["expected_revision"],
            "add_darks": darks, "add_flats": flats,
            "add_releases": releases, "retract_releases": retractions,
            "put_frames": frames, "withdraw_frames": withdrawals}
