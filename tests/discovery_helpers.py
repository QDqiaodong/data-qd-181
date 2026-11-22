"""Helpers shared by the discovery test modules."""
import json
import tempfile
from pathlib import Path

from astro_stack import discovery


def write_json(directory, name, value):
    path = Path(directory, name)
    path.write_text(json.dumps(value, ensure_ascii=False))
    return path


def base_config(**overrides):
    config = {"aperture_ids": ["a"], "bootstrap_frames": 1,
              "reference_capacity": 1, "min_exposure_s": 1,
              "min_abs_delta": "1", "sigma_squared": "4"}
    config.update(overrides)
    return config


class TempWorkspace:
    def __init__(self):
        self._temp = tempfile.TemporaryDirectory()
        self.archive = Path(self._temp.name, "archive")
        self.path = Path(self._temp.name, "workspace")

    def initialize(self, archive_path=None, config=None):
        return discovery.initialize(
            str(self.path), str(archive_path or self.archive),
            config or base_config())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    def close(self):
        self._temp.cleanup()
