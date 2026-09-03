from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rebaser import registry_write


class RegistryWriteTests(unittest.TestCase):
    def test_lock_times_out_while_another_writer_holds_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            registry = Path(temp_name) / "registry.json"
            registry.write_text("{}\n", encoding="utf-8")
            with registry_write.exclusive_registry_lock(registry):
                with self.assertRaises(
                    registry_write.RegistryLockTimeout
                ):
                    with registry_write.exclusive_registry_lock(
                        registry,
                        timeout_seconds=0.05,
                        poll_seconds=0.01,
                    ):
                        self.fail("second writer unexpectedly acquired lock")

    def test_prepared_json_is_verified_before_atomic_replace(self) -> None:
        payload = {
            "schemaVersion": 1,
            "champions": {
                "1": {
                    "alias": "Annie",
                }
            },
        }
        with tempfile.TemporaryDirectory() as temp_name:
            registry = Path(temp_name) / "registry.json"
            registry.write_text('{"old":true}\n', encoding="utf-8")
            temp_path, digest = registry_write.prepare_atomic_json(
                registry,
                payload,
            )
            with registry_write.exclusive_registry_lock(registry):
                registry_write.commit_atomic_json(
                    temp_path,
                    registry,
                    digest,
                )

            self.assertEqual(
                json.loads(registry.read_text(encoding="utf-8")),
                payload,
            )
            self.assertFalse(temp_path.exists())

    def test_changed_temp_is_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            registry = Path(temp_name) / "registry.json"
            original = b'{"old":true}\n'
            registry.write_bytes(original)
            temp_path, digest = registry_write.prepare_atomic_json(
                registry,
                {"new": True},
            )
            temp_path.write_text('{"tampered":true}\n', encoding="utf-8")
            with (
                registry_write.exclusive_registry_lock(registry),
                self.assertRaises(registry_write.RegistryWriteError),
            ):
                registry_write.commit_atomic_json(
                    temp_path,
                    registry,
                    digest,
                )

            self.assertEqual(registry.read_bytes(), original)

    def test_replace_failure_keeps_original_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            registry = Path(temp_name) / "registry.json"
            original = b'{"old":true}\n'
            registry.write_bytes(original)
            temp_path, digest = registry_write.prepare_atomic_json(
                registry,
                {"new": True},
            )
            with (
                registry_write.exclusive_registry_lock(registry),
                patch.object(
                    registry_write.os,
                    "replace",
                    side_effect=OSError("simulated replace failure"),
                ),
                self.assertRaisesRegex(OSError, "simulated replace failure"),
            ):
                registry_write.commit_atomic_json(
                    temp_path,
                    registry,
                    digest,
                )

            self.assertEqual(registry.read_bytes(), original)
            self.assertTrue(temp_path.is_file())
            temp_path.unlink()


if __name__ == "__main__":
    unittest.main()
