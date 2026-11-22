"""Helpers shared by the release test modules."""
import json
import tempfile
from pathlib import Path

from astro_stack.archive import apply_batch, initialize


def write_json(directory, name, value):
    path = Path(directory, name)
    path.write_text(json.dumps(value, ensure_ascii=False))
    return path


class TempPaths:
    def __init__(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.archive = self.root / "archive"
        self.library = self.root / "library"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    def close(self):
        self._temp.cleanup()


def two_by_one_definition(dataset_id="ds-2x1"):
    return {"dataset_id": dataset_id, "width": 2, "height": 1,
            "apertures": [], "products": None}


def two_by_one_batch(batch_id="b1", expected_revision=0,
                     g_version_expected=0, g_pixels=(30, 40)):
    """F at dx=0 with [10,20], G at dx=1 with [30,40]; unit variance, one dark."""
    return {
        "batch_id": batch_id, "expected_revision": expected_revision,
        "add_darks": [{"id": "d", "dark_exposure_s": 1,
                       "dark": [[0, 0]], "dark_variance": [[4, 4]]}],
        "add_flats": [{"id": "f", "flat": [[1, 1]],
                       "bad_mask": [[False, False]]}],
        "add_releases": [{"id": "r", "version": 1, "start_s": 0, "end_s": 100,
                          "dark_id": "d", "flat_id": "f"}],
        "retract_releases": [],
        "put_frames": [
            {"id": "F", "exposure_s": 1, "dx": 0, "dy": 0, "saturation": 1000,
             "pixels": [[10, 20]], "observed_at_s": 5, "variance": [[1, 1]],
             "expected_version": 0},
            {"id": "G", "exposure_s": 1, "dx": 1, "dy": 0, "saturation": 1000,
             "pixels": [list(g_pixels)], "observed_at_s": 5,
             "variance": [[1, 1]],
             "expected_version": g_version_expected}],
        "withdraw_frames": []}


def replace_g_batch(expected_revision, g_pixels=(30, 40)):
    return {
        "batch_id": f"b-gv2-r{expected_revision}",
        "expected_revision": expected_revision,
        "add_darks": [], "add_flats": [], "add_releases": [],
        "retract_releases": [],
        "put_frames": [
            {"id": "G", "exposure_s": 1, "dx": 1, "dy": 0, "saturation": 1000,
             "pixels": [list(g_pixels)], "observed_at_s": 5,
             "variance": [[1, 1]], "expected_version": 1}],
        "withdraw_frames": []}


def build_two_by_one_archive(path, definition=None):
    initialize(str(path), definition or two_by_one_definition())
    apply_batch(str(path), two_by_one_batch())


def full_request(request_id="req-1", dataset_id="ds-2x1", revision=1,
                 region=None):
    return {"request_id": request_id, "dataset_id": dataset_id,
            "revision": revision,
            "region": region or {"x0": 0, "y0": 0, "x1": 2, "y1": 1}}


def license_with(denied, dataset_id="ds-2x1"):
    rows = [{"frame_id": frame_id, "frame_version": version, "x": x, "y": y}
            for frame_id, version, x, y in denied]
    return {"dataset_id": dataset_id, "denied": rows}
