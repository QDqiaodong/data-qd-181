"""Helpers shared by the archive test modules."""
import json
import tempfile
from pathlib import Path


def classic_definition(dataset_id="ds", width=1, height=1, apertures=None):
    if apertures is None:
        apertures = [{"id": "a", "x": 0, "y": 0, "radius": 0}]
    return {"dataset_id": dataset_id, "width": width, "height": height,
            "apertures": apertures, "products": None}


def campaign_definition(dataset_id="ds", products=None, apertures=None):
    if apertures is None:
        apertures = [{"id": "a", "x": 0, "y": 0, "radius": 0}]
    if products is None:
        products = [{"id": "p", "start_s": 0, "end_s": 100, "priority": 1,
                     "max_frames": 1,
                     "requirements": [{"aperture_id": "a", "min_exposure_s": 1,
                                       "max_variance": "2"}]}]
    return {"dataset_id": dataset_id, "width": 1, "height": 1,
            "apertures": apertures, "products": products}


def dark(dark_id, value=0, variance=1, exposure=1, width=1, height=1):
    return {"id": dark_id, "dark_exposure_s": exposure,
            "dark": [[value for _ in range(width)] for _ in range(height)],
            "dark_variance": [[variance for _ in range(width)]
                              for _ in range(height)]}


def flat(flat_id="f0", values=None, mask=None):
    if values is None:
        values = [[1]]
    if mask is None:
        mask = [[False for _ in row] for row in values]
    return {"id": flat_id, "flat": values, "bad_mask": mask}


def release(release_id, version, dark_id="d0", flat_id="f0", start=0, end=100):
    return {"id": release_id, "version": version, "start_s": start,
            "end_s": end, "dark_id": dark_id, "flat_id": flat_id}


def frame(frame_id, expected_version, value=10, variance=0, observed_at=5,
          exposure=1, dx=0, dy=0, saturation=100, pixels=None,
          frame_variance=None):
    if pixels is None:
        pixels = [[value]]
    if frame_variance is None:
        frame_variance = [[variance]]
    return {"id": frame_id, "exposure_s": exposure, "dx": dx, "dy": dy,
            "saturation": saturation, "pixels": pixels,
            "observed_at_s": observed_at, "variance": frame_variance,
            "expected_version": expected_version}


def batch(batch_id, expected_revision, *, add_darks=None, add_flats=None,
          add_releases=None, retract_releases=None, put_frames=None,
          withdraw_frames=None):
    return {"batch_id": batch_id, "expected_revision": expected_revision,
            "add_darks": add_darks or [], "add_flats": add_flats or [],
            "add_releases": add_releases or [],
            "retract_releases": retract_releases or [],
            "put_frames": put_frames or [],
            "withdraw_frames": withdraw_frames or []}


def bootstrap(root, definition, batches):
    from astro_stack.archive import initialize, apply_batch
    initialize(str(root), definition)
    receipts = []
    for value in batches:
        receipts.append(apply_batch(str(root), value))
    return receipts


def write_json(directory, name, value):
    path = Path(directory, name)
    path.write_text(json.dumps(value, ensure_ascii=False))
    return path


class TempArchive:
    def __init__(self):
        self._temp = tempfile.TemporaryDirectory()
        self.path = Path(self._temp.name, "archive")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    def close(self):
        self._temp.cleanup()
