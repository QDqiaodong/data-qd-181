"""Discovery configuration validation and the astro-discovery CLI lifecycle."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from astro_stack.archive import apply_batch, initialize
from astro_stack import discovery

from archive_helpers import batch, classic_definition, dark, flat, frame, release
from discovery_helpers import TempWorkspace, base_config, write_json

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


class ConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.workspace = TempWorkspace()
        self.addCleanup(self.workspace.close)
        initialize(str(self.workspace.archive), classic_definition(width=2,
            height=2, apertures=[
                {"id": "a0", "x": 0, "y": 0, "radius": 0},
                {"id": "a1", "x": 1, "y": 1, "radius": 0}]))

    def _config(self, **overrides):
        config = base_config(aperture_ids=["a0"])
        config.update(overrides)
        return config

    def _initialize(self, config=None):
        return discovery.initialize(str(self.workspace.path),
                                    str(self.workspace.archive),
                                    config or self._config())

    def test_valid_config_is_normalized_and_fixed(self):
        receipt = self._initialize(self._config(aperture_ids=["a1", "a0"],
                                               bootstrap_frames=2,
                                               reference_capacity=4))
        self.assertEqual(receipt, {"dataset_id": "ds",
                                   "processed_revision": None})
        # initialization never writes facts into the source archive
        from astro_stack.archive.store import Store
        with Store(str(self.workspace.archive)) as store:
            self.assertEqual(store.state["revision"], 0)

    def test_unknown_and_missing_fields_rejected(self):
        config = self._config()
        del config["sigma_squared"]
        with self.assertRaises(ValueError):
            self._initialize(config)
        config = self._config()
        config["unexpected"] = 1
        with self.assertRaises(ValueError):
            self._initialize(config)

    def test_booleans_are_not_integers(self):
        for field_name in ("bootstrap_frames", "reference_capacity",
                           "min_exposure_s"):
            for value in (True, False):
                config = self._config()
                config[field_name] = value
                with self.assertRaises(ValueError):
                    self._initialize(config)

    def test_aperture_reference_and_count_rules(self):
        for bad in ([], ["a0", "a0"], ["missing"], ["a0", "a1", "a0"]):
            with self.assertRaises(ValueError):
                self._initialize(self._config(aperture_ids=bad))
        # non-string aperture ids are rejected
        with self.assertRaises(ValueError):
            self._initialize(self._config(aperture_ids=[1]))

    def test_integer_bounds_and_capacity_relationship(self):
        with self.assertRaises(ValueError):
            self._initialize(self._config(bootstrap_frames=0))
        with self.assertRaises(ValueError):
            self._initialize(self._config(bootstrap_frames=9))
        with self.assertRaises(ValueError):
            self._initialize(self._config(bootstrap_frames=3,
                                         reference_capacity=2))
        with self.assertRaises(ValueError):
            self._initialize(self._config(min_exposure_s=86401))
        with self.assertRaises(ValueError):
            self._initialize(self._config(reference_capacity=9))

    def test_rational_strings_follow_the_archive_grammar(self):
        for field_name in ("min_abs_delta", "sigma_squared"):
            for bad in ("0", "-1", "1.5", "1/0", "01", "", 3, True, "1/"):
                with self.assertRaises(ValueError):
                    self._initialize(self._config(**{field_name: bad}))
        # large numerators/denominators share the archive's 10^12 ceiling
        with self.assertRaises(ValueError):
            self._initialize(self._config(min_abs_delta="1000000000001"))

    def test_initialize_never_overwrites_and_failure_leaves_nothing(self):
        self._initialize()
        with self.assertRaises(OSError):
            self._initialize()
        # a failed initialization against a fresh path leaves no directory
        other = Path(self.workspace._temp.name, "other")
        with self.assertRaises(ValueError):
            discovery.initialize(str(other), str(self.workspace.archive),
                                 base_config(aperture_ids=["missing"]))
        self.assertFalse(other.exists())

    def test_workspace_rejects_foreign_archive_dataset(self):
        other_archive = Path(self.workspace._temp.name, "other-archive")
        initialize(str(other_archive),
                   classic_definition(dataset_id="different"))
        self._initialize()
        # repoint the durable binding at a foreign dataset: integrity must fail
        state_path = self.workspace.path / "state.json"
        state = json.loads(state_path.read_text())
        state["archive_path"] = str(other_archive)
        state_path.write_text(json.dumps(state))
        with self.assertRaises(ValueError):
            discovery.advance(str(self.workspace.path), 0)
        # pointing at a directory that is not an archive is an integrity error
        non_archive = Path(self.workspace._temp.name, "non-archive")
        non_archive.mkdir()
        with self.assertRaises((ValueError, OSError)):
            discovery.initialize(
                str(Path(self.workspace._temp.name, "x")),
                str(non_archive), base_config(aperture_ids=["a0"]))


class CliLifecycleTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "astro_stack.cli_discovery", *args],
            cwd=ROOT, capture_output=True, text=True,
            env=dict(__import__("os").environ, PYTHONPATH=str(SRC)))

    def _archive(self, directory):
        archive = directory / "archive"
        initialize(str(archive), classic_definition())
        apply_batch(str(archive), batch("b1", 0,
            add_darks=[dark("d0", value=0, variance=0)],
            add_flats=[flat()],
            add_releases=[release("r1", 1)],
            put_frames=[frame("a", 0, value=10, variance=1, observed_at=1),
                        frame("b", 0, value=12, variance=1, observed_at=2)]))
        return archive

    def test_full_cli_lifecycle_and_snapshot_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            archive = self._archive(directory)
            workspace = directory / "workspace"
            config_path = write_json(directory, "config.json", base_config())
            init = self.run_cli("--workspace", str(workspace),
                                "--initialize", str(config_path),
                                "--archive", str(archive))
            self.assertEqual(init.returncode, 0, init.stderr)
            self.assertEqual(json.loads(init.stdout),
                             {"dataset_id": "ds", "processed_revision": None})
            advanced = self.run_cli("--workspace", str(workspace),
                                    "--advance", "1")
            self.assertEqual(advanced.returncode, 0, advanced.stderr)
            self.assertEqual(json.loads(advanced.stdout),
                             {"dataset_id": "ds", "revision": 1,
                              "first_seq": None, "last_seq": None})
            report_path = directory / "snapshot.json"
            snap = self.run_cli("--workspace", str(workspace),
                                "--snapshot", "--output", str(report_path))
            self.assertEqual(snap.returncode, 0, snap.stderr)
            published = json.loads(report_path.read_text())
            self.assertEqual(json.loads(snap.stdout), published["summary"])
            # create-only publication: existing report is never overwritten
            blocked = self.run_cli("--workspace", str(workspace),
                                   "--snapshot", "--output", str(report_path))
            self.assertEqual(blocked.returncode, 2)
            pending = self.run_cli("--workspace", str(workspace),
                                   "--pending", "50")
            self.assertEqual(pending.returncode, 0, pending.stderr)
            self.assertEqual(json.loads(pending.stdout),
                             {"acknowledged_through": 0, "events": []})
            ack = self.run_cli("--workspace", str(workspace), "--ack", "0")
            self.assertEqual(json.loads(ack.stdout),
                             {"acknowledged_through": 0})

    def test_cli_rejects_bad_inputs_and_missing_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            archive = self._archive(directory)
            workspace = directory / "workspace"
            bad_config = write_json(directory, "bad.json", {"bootstrap_frames": 1})
            failed = self.run_cli("--workspace", str(workspace),
                                  "--initialize", str(bad_config),
                                  "--archive", str(archive))
            self.assertEqual(failed.returncode, 2)
            self.assertFalse(workspace.exists())
            # initialize without --archive
            good_config = write_json(directory, "good.json", base_config())
            failed = self.run_cli("--workspace", str(workspace),
                                  "--initialize", str(good_config))
            self.assertEqual(failed.returncode, 2)
            self.run_cli("--workspace", str(workspace),
                         "--initialize", str(good_config),
                         "--archive", str(archive))
            # unknown revision, out-of-range limits and tail overshoot
            self.assertEqual(self.run_cli("--workspace", str(workspace),
                                          "--advance", "99").returncode, 2)
            self.assertEqual(self.run_cli("--workspace", str(workspace),
                                          "--pending", "0").returncode, 2)
            self.assertEqual(self.run_cli("--workspace", str(workspace),
                                          "--pending", "101").returncode, 2)
            self.assertEqual(self.run_cli("--workspace", str(workspace),
                                          "--ack", "1").returncode, 2)
            # snapshot before any advance is a business error
            fresh = directory / "fresh"
            self.run_cli("--workspace", str(fresh),
                         "--initialize", str(good_config),
                         "--archive", str(archive))
            no_snapshot = self.run_cli(
                "--workspace", str(fresh), "--snapshot",
                "--output", str(directory / "none.json"))
            self.assertEqual(no_snapshot.returncode, 2)
            self.assertFalse((directory / "none.json").exists())

    def test_strict_config_json_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            archive = self._archive(directory)
            workspace = directory / "workspace"
            for text in ('{"bootstrap_frames": 1, "bootstrap_frames": 2}',
                         '{"x": NaN}'):
                path = directory / "bad.json"
                path.write_text(text)
                result = self.run_cli("--workspace", str(workspace),
                                      "--initialize", str(path),
                                      "--archive", str(archive))
                self.assertEqual(result.returncode, 2)
            big = bytearray(b'{"a":')
            big.extend(b"1" * 8_000_000)
            big.extend(b"}")
            path = directory / "big.json"
            path.write_bytes(bytes(big))
            result = self.run_cli("--workspace", str(workspace),
                                  "--initialize", str(path),
                                  "--archive", str(archive))
            self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
