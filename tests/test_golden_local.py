from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rebaser.champion_layout import ChampionIdentity
from tests.helpers.synthetic_wad import SyntheticChunk, write_synthetic_wad
from rebaser.wad_access import wad_path_hash


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "golden_local.py"
SPEC = importlib.util.spec_from_file_location("golden_local", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
golden_local = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(golden_local)


class LocalGoldenTests(unittest.TestCase):
    def test_main_overwrites_stale_success_before_setup_can_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output = Path(temp_name) / "source-golden.json"
            output.write_text(
                json.dumps(
                    {
                        "schemaVersion": 2,
                        "status": "passed",
                        "complete": True,
                        "champions": [{"status": "success"}],
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(
                    golden_local,
                    "require_bundled_hash_source",
                    side_effect=OSError("fixture setup failure"),
                ),
                self.assertRaisesRegex(OSError, "fixture setup failure"),
            ):
                golden_local.main(["--output", str(output)])
            marker = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(marker["status"], "running")
        self.assertIs(marker["complete"], False)
        self.assertEqual(marker["expectedChampionCount"], 10)
        self.assertEqual(marker["processedChampionCount"], 0)
        self.assertEqual(marker["champions"], [])

    def test_final_lifecycle_is_complete_and_count_bound(self) -> None:
        passed = {
            "status": "running",
            "complete": False,
            "expectedChampionCount": 2,
            "processedChampionCount": 2,
        }
        failed = golden_local.finalize_golden_lifecycle(
            passed,
            unexpected_failure=False,
        )
        self.assertFalse(failed)
        self.assertEqual(passed["status"], "passed")
        self.assertIs(passed["complete"], True)

        incomplete = {
            "status": "running",
            "complete": False,
            "expectedChampionCount": 2,
            "processedChampionCount": 1,
        }
        failed = golden_local.finalize_golden_lifecycle(
            incomplete,
            unexpected_failure=False,
        )
        self.assertTrue(failed)
        self.assertEqual(incomplete["status"], "failed")
        self.assertIs(incomplete["complete"], True)

    def test_unchanged_gate_detects_same_size_same_mtime_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "identity.bin"
            path.write_bytes(b"before")
            identity = golden_local.stable_file_identity(path)

            path.write_bytes(b"after!")
            os.utime(
                path,
                ns=(
                    path.stat().st_atime_ns,
                    int(identity["modifiedNs"]),
                ),
            )

            with self.assertRaisesRegex(OSError, "changed during Golden run"):
                golden_local.require_file_unchanged(path, identity)

    def test_private_input_snapshots_preserve_names_and_content_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source_dir = root / "source"
            source_dir.mkdir()
            tool = source_dir / "wad-extract.exe"
            hashes = source_dir / "hashes.game.txt"
            tool.write_bytes(b"fixture-tool")
            hashes.write_bytes(b"fixture-hashes")
            tool_identity = golden_local.stable_file_identity(tool)
            hashes_identity = golden_local.stable_file_identity(hashes)
            private_dir = root / "private"

            private_tool = private_dir / tool.name
            private_hashes = private_dir / hashes.name
            private_tool_identity = golden_local.copy_verified_input_snapshot(
                tool,
                private_tool,
                tool_identity,
            )
            private_hashes_identity = golden_local.copy_verified_input_snapshot(
                hashes,
                private_hashes,
                hashes_identity,
            )

            self.assertEqual(private_tool.parent, private_hashes.parent)
            self.assertEqual(private_tool.name, "wad-extract.exe")
            self.assertEqual(private_hashes.name, "hashes.game.txt")
            self.assertNotEqual(private_tool, tool)
            for original, snapshot in (
                (tool_identity, private_tool_identity),
                (hashes_identity, private_hashes_identity),
            ):
                self.assertEqual(snapshot["size"], original["size"])
                self.assertEqual(snapshot["sha256"], original["sha256"])

    def test_execution_input_gate_detects_same_stat_original_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "wad-extract.exe"
            path.write_bytes(b"original")
            identity = golden_local.stable_file_identity(path)
            path.write_bytes(b"mutated!")
            os.utime(
                path,
                ns=(
                    path.stat().st_atime_ns,
                    int(identity["modifiedNs"]),
                ),
            )

            errors = golden_local.require_execution_inputs_unchanged(
                (("original legacy tool", path, identity),)
            )

        self.assertEqual(len(errors), 1)
        self.assertIn("original legacy tool", errors[0])

    def test_hash_source_scan_only_keeps_wanted_skin_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "hashes.game.txt"
            skin0 = "data/characters/annie/skins/skin0.bin"
            skin1 = "data/characters/annie/skins/skin1.bin"
            skin0_hash = golden_local.wad_access.wad_path_hash(skin0)
            skin1_hash = golden_local.wad_access.wad_path_hash(skin1)
            path.write_text(
                "\n".join(
                    (
                        f"{skin0_hash:016x} {skin0}",
                        f"{skin1_hash:016x} {skin1}",
                        "0000000000000003 data/characters/annie/annie.skn",
                        "not-a-hash ignored",
                    )
                ),
                encoding="utf-8",
            )

            found = golden_local.scan_known_skin_paths(
                path,
                {skin0_hash, skin1_hash, 3},
            )

        self.assertEqual(
            found,
            {
                skin0_hash: skin0,
                skin1_hash: skin1,
            },
        )

    def test_hash_source_rejects_mismatch_even_when_hash_is_not_wanted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "hashes.game.txt"
            path.write_text(
                "0000000000000001 "
                "data/characters/annie/skins/skin1.bin\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "declared hash mismatch"):
                golden_local.scan_known_skin_paths(path, set())

    def test_pair_classification_is_per_unit_and_per_skin(self) -> None:
        paths = {
            1: "data/characters/annie/skins/skin0.bin",
            2: "data/characters/annie/skins/skin1.bin",
            3: "data/characters/annietibbers/skins/skin0.bin",
            4: "data/characters/annietibbers/skins/skin2.bin",
            5: "data/characters/targetonly/skins/skin7.bin",
            6: "data/characters/baseonly/skins/skin0.bin",
        }

        layouts = golden_local.classify_skin_paths(paths, (1, 2, 7))

        self.assertEqual(
            [
                [item["unit"] for item in layout["paired"]]
                for layout in layouts
            ],
            [["annie"], ["annietibbers"], []],
        )
        self.assertEqual(
            [
                [item["unit"] for item in layout["baseOnly"]]
                for layout in layouts
            ],
            [
                ["annietibbers", "baseonly"],
                ["annie", "baseonly"],
                ["annie", "annietibbers", "baseonly"],
            ],
        )
        self.assertEqual(
            [
                [item["unit"] for item in layout["targetOnly"]]
                for layout in layouts
            ],
            [[], [], ["targetonly"]],
        )
        self.assertEqual(
            layouts[0]["absent"],
            ["targetonly"],
        )

    def test_skin_set_and_success_coverage_are_strict(self) -> None:
        champion = {
            "query": "Annie",
            "mainUnit": "annie",
            "skinSet": {"ranges": [[1, 2]], "exclude": []},
            "skinCount": 2,
            "pairedCount": 3,
            "uniqueBaseCount": 2,
        }
        paths = {
            1: "data/characters/annie/skins/skin0.bin",
            2: "data/characters/annie/skins/skin1.bin",
            3: "data/characters/annie/skins/skin2.bin",
            4: "data/characters/annietibbers/skins/skin0.bin",
            5: "data/characters/annietibbers/skins/skin2.bin",
        }
        skin_numbers = golden_local.expand_skin_set(champion)
        layouts = golden_local.classify_skin_paths(paths, skin_numbers)

        self.assertEqual(
            golden_local.validate_success_layouts(
                champion,
                paths,
                layouts,
            ),
            {
                "skinCount": 2,
                "pairedCount": 3,
                "uniqueBaseCount": 2,
            },
        )

        for field, value, message in (
            ("skinCount", 1, "skinSet expands"),
            ("pairedCount", 2, "pairedCount"),
            ("uniqueBaseCount", 1, "uniqueBaseCount"),
        ):
            broken = dict(champion)
            broken[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError,
                message,
            ):
                if field == "skinCount":
                    golden_local.expand_skin_set(broken)
                else:
                    golden_local.validate_success_layouts(
                        broken,
                        paths,
                        layouts,
                    )

        missing_main = dict(paths)
        del missing_main[3]
        missing_layouts = golden_local.classify_skin_paths(
            missing_main,
            skin_numbers,
        )
        with self.assertRaisesRegex(ValueError, "main unit.*skinSet mismatch"):
            golden_local.validate_success_layouts(
                champion,
                missing_main,
                missing_layouts,
            )

    def test_prepare_champion_wads_creates_one_session_per_champion(
        self,
    ) -> None:
        champions = [
            {
                "championId": 1,
                "query": "Annie",
                "wadName": "Annie.wad.client",
            },
            {
                "championId": 805,
                "query": "Locke",
                "wadName": "Locke.wad.client",
            },
        ]
        created: list[Path] = []

        class FakePrepared:
            def __init__(self, wad_path: Path) -> None:
                self.wad_path = wad_path
                self.chunks_by_hash = {len(created) + 1: object()}
                created.append(wad_path)

        with tempfile.TemporaryDirectory() as temp_name:
            champions_dir = Path(temp_name)
            for champion in champions:
                (champions_dir / champion["wadName"]).write_bytes(b"wad")
            with patch.object(
                golden_local.wad_access,
                "PreparedChampionWad",
                FakePrepared,
            ):
                prepared, identities, all_hashes = (
                    golden_local.prepare_champion_wads(
                        champions,
                        champions_dir,
                    )
                )

        self.assertEqual(
            created,
            [
                champions_dir / "Annie.wad.client",
                champions_dir / "Locke.wad.client",
            ],
        )
        self.assertEqual(set(prepared), {1, 805})
        self.assertEqual(set(identities), {1, 805})
        self.assertEqual(all_hashes, {1, 2})

    def test_champion_golden_batches_unique_direct_paths_once(
        self,
    ) -> None:
        base_path = "data/characters/annie/skins/skin0.bin"
        target1_path = "data/characters/annie/skins/skin1.bin"
        target2_path = "data/characters/annie/skins/skin2.bin"
        paths = (base_path, target1_path, target2_path)
        hashes = {
            golden_local.wad_access.wad_path_hash(path): path
            for path in paths
        }
        payloads = {
            base_path: b"PROP-base",
            target1_path: b"PROP-target-1",
            target2_path: b"PROP-target-2",
        }
        champion = {
            "championId": 1,
            "query": "Annie",
            "mainUnit": "annie",
            "skinSet": {"ranges": [[1, 2]], "exclude": []},
            "skinCount": 2,
            "pairedCount": 2,
            "uniqueBaseCount": 1,
        }
        direct_calls: list[tuple[tuple[str, ...], bool]] = []
        legacy_reads: list[str] = []

        class FakePrepared:
            def __init__(self, wad_path: Path) -> None:
                self.wad_path = wad_path

            def read_many(
                self,
                requested: tuple[str, ...],
                *,
                validate_bin: bool = False,
            ) -> dict[str, bytes]:
                direct_calls.append((tuple(requested), validate_bin))
                return {
                    path: payloads[path]
                    for path in requested
                }

        class FakeLegacyIndex:
            def read(
                self,
                relative_path: str,
                expected_hash: int,
                context: object,
            ) -> bytes:
                self.assert_hash(relative_path, expected_hash)
                legacy_reads.append(relative_path)
                return payloads[relative_path]

            @staticmethod
            def assert_hash(relative_path: str, expected_hash: int) -> None:
                actual = golden_local.wad_access.wad_path_hash(relative_path)
                if actual != expected_hash:
                    raise AssertionError("legacy received the wrong path hash")

        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "Annie.wad.client"
            wad_path.write_bytes(b"wad")
            prepared = FakePrepared(wad_path)
            with (
                patch.object(
                    golden_local,
                    "extract_legacy_for_golden",
                    return_value=Path(temp_name),
                ) as legacy_extract,
                patch.object(
                    golden_local.golden_oracle,
                    "LegacyExtractIndex",
                    return_value=FakeLegacyIndex(),
                ),
            ):
                record = golden_local.build_champion_golden(
                    champion,
                    prepared,
                    hashes,
                    Path(temp_name) / "hashes.game.txt",
                    Path(temp_name) / "wad-extract.exe",
                )

        self.assertEqual(
            direct_calls,
            [((base_path, target1_path, target2_path), True)],
        )
        self.assertEqual(
            legacy_reads,
            [base_path, target1_path, base_path, target2_path],
        )
        legacy_extract.assert_called_once()
        self.assertEqual(
            legacy_extract.call_args.args[2:],
            (
                Path(temp_name) / "hashes.game.txt",
                Path(temp_name) / "wad-extract.exe",
            ),
        )
        self.assertEqual(
            [pair["targetPath"] for pair in record["pairs"]],
            [target1_path, target2_path],
        )
        self.assertEqual(
            [skin["skinNumber"] for skin in record["skins"]],
            [1, 2],
        )
        self.assertEqual(
            set(record),
            {
                "championId",
                "champion",
                "status",
                "skinSet",
                "skinCount",
                "pairedCount",
                "uniqueBaseCount",
                "pairs",
                "skins",
            },
        )

    def test_annie_fixed_vector_is_checked_without_claiming_full_validation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "hashes.game.txt"
            path.write_text(
                f"{golden_local.ANNIE_XXH64_VECTOR:016x} "
                f"{golden_local.ANNIE_XXH64_VECTOR_PATH}\n",
                encoding="utf-8",
            )
            self.assertEqual(
                golden_local.scan_known_skin_paths(
                    path,
                    set(),
                    validate_annie_vector=True,
                ),
                {},
            )

            path.write_text(
                "0000000000000001 "
                f"{golden_local.ANNIE_XXH64_VECTOR_PATH}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "fixed vector mismatch"):
                golden_local.scan_known_skin_paths(
                    path,
                    set(),
                    validate_annie_vector=True,
                )

    def test_expected_legacy_failure_is_actually_executed_and_validated(
        self,
    ) -> None:
        champion = {
            "championId": 805,
            "query": "Locke",
            "skinSet": {"ranges": [[1, 2]], "exclude": []},
            "skinCount": 2,
            "pairedCount": 2,
            "uniqueBaseCount": 1,
            "legacyFailureType": "SystemExit",
            "legacyFailureMessage": (
                "no data/characters directory found after extracting "
                "Locke.wad.client"
            ),
        }
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "Locke.wad.client"
            wad_path.write_bytes(b"wad")
            with patch.object(
                golden_local.script,
                "build_local_catalog",
                side_effect=SystemExit(
                    "no data/characters directory found after extracting "
                    "Locke.wad.client"
                ),
            ) as legacy:
                record = golden_local.validate_expected_legacy_failure(
                    champion,
                    wad_path,
                    Path(temp_name) / "hashes.game.txt",
                    Path(temp_name) / "wad-extract.exe",
                )

        legacy.assert_called_once_with(
            "Locke",
            wad_path,
            wad_extract_path=Path(temp_name) / "wad-extract.exe",
            hashes_path=Path(temp_name) / "hashes.game.txt",
        )
        self.assertEqual(record["status"], "expected_unsupported")
        self.assertTrue(record["legacyFailure"]["validated"])

    def test_direct_supported_legacy_failure_uses_computed_main_paths(
        self,
    ) -> None:
        champion = {
            "championId": 805,
            "query": "Locke",
            "mainUnit": "locke",
            "skinSet": {"ranges": [[1, 2]], "exclude": []},
            "skinCount": 2,
            "pairedCount": 2,
            "uniqueBaseCount": 1,
        }

        class FakePrepared:
            def inspect_paths(self, paths: tuple[str, ...]) -> dict[str, object]:
                return {path: object() for path in paths}

        paths = golden_local.computed_pool_skin_paths(
            champion,
            FakePrepared(),
            {},
        )

        expected = {
            "data/characters/locke/skins/skin0.bin",
            "data/characters/locke/skins/skin1.bin",
            "data/characters/locke/skins/skin2.bin",
        }
        self.assertEqual(set(paths.values()), expected)
        self.assertEqual(
            set(paths),
            {wad_path_hash(path) for path in expected},
        )

    def test_expected_legacy_failure_executes_private_tool_and_hashes(
        self,
    ) -> None:
        champion = {
            "championId": 805,
            "query": "Locke",
            "skinSet": {"ranges": [[1, 1]], "exclude": []},
            "skinCount": 1,
            "pairedCount": 1,
            "uniqueBaseCount": 1,
            "legacyFailureType": "SystemExit",
            "legacyFailureMessage": (
                "no data/characters directory found after extracting "
                "Locke.wad.client"
            ),
        }
        commands: list[tuple[str, ...]] = []
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            wad_path = root / "Locke.wad.client"
            main_path = "data/characters/locke/skins/skin0.bin"
            write_synthetic_wad(
                wad_path,
                [
                    SyntheticChunk(
                        path_hash=wad_path_hash(main_path),
                        payload=b"PROPfixture",
                        compression_type=0,
                    )
                ],
                version_minor=4,
            )
            private = root / "private"
            private.mkdir()
            tool = private / "wad-extract.exe"
            hashes = private / "hashes.game.txt"
            tool.write_bytes(b"fixture-tool")
            hashes.write_bytes(b"fixture-hashes")

            def fake_process(
                command: list[str],
                **_: object,
            ) -> None:
                commands.append(tuple(command))
                Path(command[2]).mkdir()

            with (
                patch.object(
                    golden_local.script,
                    "capture_lcu_wad_generation",
                    return_value=(),
                ),
                patch.object(
                    golden_local.script,
                    "load_champion_identity",
                    return_value=ChampionIdentity(
                        champion_id=805,
                        display_name="Locke",
                        alias="Locke",
                        wad_base="Locke",
                        main_unit="locke",
                    ),
                ),
                patch.object(
                    golden_local.script,
                    "run_external_process",
                    side_effect=fake_process,
                ),
            ):
                record = golden_local.validate_expected_legacy_failure(
                    champion,
                    wad_path,
                    hashes,
                    tool,
                )

        self.assertEqual(record["status"], "expected_unsupported")
        self.assertEqual(len(commands), 1)
        self.assertEqual(Path(commands[0][0]), tool)
        self.assertEqual(Path(commands[0][3]), hashes)

    def test_expected_legacy_failure_rejects_wrong_error(self) -> None:
        champion = {
            "championId": 805,
            "query": "Locke",
            "skinSet": {"ranges": [[1, 1]], "exclude": []},
            "skinCount": 1,
            "pairedCount": 1,
            "uniqueBaseCount": 1,
            "legacyFailureType": "SystemExit",
            "legacyFailureMessage": "expected text",
        }
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "Locke.wad.client"
            wad_path.write_bytes(b"wad")
            with (
                patch.object(
                    golden_local.script,
                    "build_local_catalog",
                    side_effect=ValueError("wrong"),
                ),
                self.assertRaisesRegex(ValueError, "failure type"),
            ):
                golden_local.validate_expected_legacy_failure(
                    champion,
                    wad_path,
                    Path(temp_name) / "hashes.game.txt",
                    Path(temp_name) / "wad-extract.exe",
                )

    def test_expected_legacy_failure_rejects_wrong_message(self) -> None:
        champion = {
            "championId": 805,
            "query": "Locke",
            "skinSet": {"ranges": [[1, 1]], "exclude": []},
            "skinCount": 1,
            "pairedCount": 1,
            "uniqueBaseCount": 1,
            "legacyFailureType": "SystemExit",
            "legacyFailureMessage": "expected exact message",
        }
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "Locke.wad.client"
            wad_path.write_bytes(b"wad")
            with (
                patch.object(
                    golden_local.script,
                    "build_local_catalog",
                    side_effect=SystemExit("expected exact message plus suffix"),
                ),
                self.assertRaisesRegex(ValueError, "failure message"),
            ):
                golden_local.validate_expected_legacy_failure(
                    champion,
                    wad_path,
                    Path(temp_name) / "hashes.game.txt",
                    Path(temp_name) / "wad-extract.exe",
                )

    def test_expected_legacy_failure_rejects_contains_contract(self) -> None:
        champion = {
            "championId": 805,
            "query": "Locke",
            "skinSet": {"ranges": [[1, 1]], "exclude": []},
            "skinCount": 1,
            "pairedCount": 1,
            "uniqueBaseCount": 1,
            "legacyFailureType": "SystemExit",
            "legacyFailureMessage": "exact",
            "legacyFailureContains": "legacy",
        }
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "Locke.wad.client"
            wad_path.write_bytes(b"wad")
            with self.assertRaisesRegex(
                ValueError,
                "legacyFailureContains is not supported",
            ):
                golden_local.validate_expected_legacy_failure(
                    champion,
                    wad_path,
                    Path(temp_name) / "hashes.game.txt",
                    Path(temp_name) / "wad-extract.exe",
                )

    def test_game_versions_compare_pool_and_content_forms(self) -> None:
        self.assertEqual(
            golden_local.comparable_game_version("16.14.794.9266"),
            "16.14.7949266",
        )
        self.assertEqual(
            golden_local.comparable_game_version(
                "16.14.7949266+branch.releases-16-14.content.release"
            ),
            "16.14.7949266",
        )


if __name__ == "__main__":
    unittest.main()
