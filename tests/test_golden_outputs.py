from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from helpers.synthetic_wad import SyntheticChunk, write_synthetic_wad


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "golden_outputs.py"
SPEC = importlib.util.spec_from_file_location("golden_outputs", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
golden_outputs = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = golden_outputs
SPEC.loader.exec_module(golden_outputs)


def identity_payload(
    *,
    skin_key: int = 10,
    skin_name: str = "Annie",
    resolver_link: int = 20,
    resolver_key: int = 30,
) -> dict[str, object]:
    return {
        "entries": {
            "value": {
                "items": [
                    {
                        "key": skin_key,
                        "value": {
                            "name": "SkinCharacterDataProperties",
                            "items": [
                                {
                                    "key": "ChampionSkinName",
                                    "value": skin_name,
                                },
                                {
                                    "key": "mResourceResolver",
                                    "value": resolver_link,
                                },
                            ],
                        },
                    },
                    {
                        "key": resolver_key,
                        "value": {
                            "name": "ResourceResolver",
                            "items": [],
                        },
                    },
                ]
            }
        }
    }


def source_golden() -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "poolId": "upgrade-v1",
        "gameVersion": "1.2.3",
        "hashSource": {"sha256": "a" * 64},
        "champions": [
            {
                "championId": 1,
                "champion": "Annie",
                "status": "success",
                "skinSet": [1],
                "pairedCount": 1,
                "pairs": [
                    {
                        "context": {
                            "champion": "Annie",
                            "skin_number": 1,
                            "unit": "annie",
                            "stage": "phase0-direct-legacy-bytes",
                        },
                        "basePath": "data/characters/annie/skins/skin0.bin",
                        "targetPath": "data/characters/annie/skins/skin1.bin",
                        "baseSha256": "b" * 64,
                        "targetSha256": "c" * 64,
                    }
                ],
                "wad": {
                    "path": "Annie.wad.client",
                    "size": 100,
                    "modifiedNs": 200,
                    "sha256": "d" * 64,
                },
            }
        ],
    }


class FixedDictionaryTests(unittest.TestCase):
    def test_only_fixed_dictionary_resolves_expected_paths(self) -> None:
        context = golden_outputs.OutputContext("Annie", 1, "annie", "hashes")
        resolved = golden_outputs.parse_hash_dictionary(
            [
                "0000000000000001 data/characters/annie/skins/skin0.bin",
                "0000000000000002 data/characters/annie/skins/skin1.bin",
            ],
            {"DATA\\Characters\\Annie\\Skins\\Skin0.bin"},
            context,
        )

        self.assertEqual(
            resolved,
            {"data/characters/annie/skins/skin0.bin": 1},
        )

    def test_missing_dictionary_path_has_full_context(self) -> None:
        context = golden_outputs.OutputContext("Annie", 1, "annie", "path-hash-oracle")
        with self.assertRaises(golden_outputs.OutputGoldenError) as caught:
            golden_outputs.parse_hash_dictionary(
                [],
                {"data/characters/annie/skins/skin0.bin"},
                context,
            )

        message = str(caught.exception)
        for value in ("champion=Annie", "skin=1", "unit=annie", "stage=path-hash-oracle"):
            self.assertIn(value, message)

    def test_direct_source_hash_fills_a_stale_dictionary_gap(self) -> None:
        payload = source_golden()
        pair = payload["champions"][0]["pairs"][0]
        base_path = pair["basePath"]
        pair["basePathHash"] = (
            f"{golden_outputs.xxhash.xxh64(base_path.encode('utf-8'), seed=0).intdigest():016x}"
        )
        pair["targetPathHash"] = (
            f"{golden_outputs.xxhash.xxh64(pair['targetPath'].encode('utf-8'), seed=0).intdigest():016x}"
        )
        expectations = golden_outputs.build_source_expectations(payload)
        with tempfile.TemporaryDirectory() as temp_name:
            dictionary = Path(temp_name) / "hashes.game.txt"
            dictionary.write_text("", encoding="utf-8")
            payload["hashSource"] = golden_outputs.file_identity(
                dictionary,
                golden_outputs.global_context("test"),
            )
            with patch.object(
                golden_outputs,
                "BUNDLED_HASHES_GAME",
                dictionary,
            ):
                resolved, identity = golden_outputs.load_fixed_path_hashes(
                    payload,
                    expectations,
                )

        self.assertEqual(
            resolved[base_path],
            int(pair["basePathHash"], 16),
        )
        self.assertEqual(identity["dictionaryResolvedPathCount"], 0)
        self.assertEqual(identity["computedDirectPathCount"], 1)


class SourceGoldenTests(unittest.TestCase):
    def test_schema_two_builds_exact_skin_unit_map(self) -> None:
        expectations = golden_outputs.build_source_expectations(source_golden())

        pair = expectations[1].units_by_skin[1]["annie"]
        self.assertEqual(pair.base_path, "data/characters/annie/skins/skin0.bin")
        self.assertEqual(pair.target_path, "data/characters/annie/skins/skin1.bin")

    def test_schema_one_is_rejected(self) -> None:
        payload = source_golden()
        payload["schemaVersion"] = 1
        with self.assertRaisesRegex(
            golden_outputs.OutputGoldenError,
            "source-golden",
        ):
            golden_outputs.build_source_expectations(payload)


class OutputArchiveTests(unittest.TestCase):
    def make_archive(
        self,
        root: Path,
        *,
        extra_hash: int | None = None,
    ) -> tuple[bytes, golden_outputs.PairExpectation]:
        pair = golden_outputs.PairExpectation(
            unit="annie",
            base_path="data/characters/annie/skins/skin0.bin",
            target_path="data/characters/annie/skins/skin1.bin",
            base_sha256="b" * 64,
            target_sha256="c" * 64,
        )
        chunks = [SyntheticChunk(1, b"rebased-bin", 0)]
        if extra_hash is not None:
            chunks.append(SyntheticChunk(extra_hash, b"extra", 0))
        wad_path = root / "Annie.wad.client"
        write_synthetic_wad(wad_path, chunks, version_minor=4)
        info = {
            "Name": "Goth Annie",
            "Author": "Untargetable",
            "Version": "1.0.0",
            "Description": "test",
        }
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("META/info.json", json.dumps(info))
            archive.writestr("WAD/Annie.wad.client", wad_path.read_bytes())
        return archive_bytes.getvalue(), pair

    def test_archive_structure_path_hashes_and_bin_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            archive_data, pair = self.make_archive(Path(temp_name))

        collected = golden_outputs.inspect_archive_bytes(
            archive_data,
            relative_path="work/1/output/Goth Annie.zip",
            champion="Annie",
            skin_number=1,
            display_name="Goth Annie",
            expected_wad_name="Annie.wad.client",
            expected_units={"annie": pair},
            expected_path_hashes={pair.base_path: 1},
        )

        self.assertEqual(collected.path_hashes, (1,))
        self.assertEqual(collected.units[0].bin_data, b"rebased-bin")

    def test_extra_wad_hash_is_rejected_with_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            archive_data, pair = self.make_archive(
                Path(temp_name),
                extra_hash=2,
            )

        with self.assertRaises(golden_outputs.OutputGoldenError) as caught:
            golden_outputs.inspect_archive_bytes(
                archive_data,
                relative_path="Goth Annie.zip",
                champion="Annie",
                skin_number=1,
                display_name="Goth Annie",
                expected_wad_name="Annie.wad.client",
                expected_units={"annie": pair},
                expected_path_hashes={pair.base_path: 1},
            )

        message = str(caught.exception)
        for value in ("champion=Annie", "skin=1", "unit=<wad>", "stage=output-wad-index"):
            self.assertIn(value, message)

    def test_archive_must_resolve_inside_champion_output_root(self) -> None:
        context = golden_outputs.OutputContext(
            "Annie",
            1,
            "<archive>",
            "archive-discovery",
        )
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output_root = root / "champion-output"
            output_root.mkdir()
            outside = root / "outside.zip"
            outside.write_bytes(b"outside")

            with self.assertRaisesRegex(
                golden_outputs.OutputGoldenError,
                "outside champion output root",
            ):
                golden_outputs.resolve_output_file(
                    output_root,
                    outside,
                    context,
                )

    def test_output_tree_reparse_point_is_rejected_during_discovery(self) -> None:
        context = golden_outputs.OutputContext(
            "Annie",
            "*",
            "*",
            "archive-discovery",
        )
        with tempfile.TemporaryDirectory() as temp_name:
            output_root = Path(temp_name) / "output"
            output_root.mkdir()
            marked = output_root / "linked.zip"
            marked.write_bytes(b"not-read")
            with patch.object(
                golden_outputs,
                "_is_link_or_reparse_point",
                side_effect=lambda path: path.name == "linked.zip",
            ):
                with self.assertRaisesRegex(
                    golden_outputs.OutputGoldenError,
                    "symlink or reparse point",
                ):
                    golden_outputs.discover_output_zips(
                        output_root,
                        context,
                    )


class SemanticTests(unittest.TestCase):
    def test_four_identity_fields_are_extracted(self) -> None:
        context = golden_outputs.OutputContext("Annie", 1, "annie", "semantic")
        result = golden_outputs.extract_identity_fields(
            identity_payload(),
            context,
        )

        self.assertEqual(
            result,
            {
                "skinCharacterDataPropertiesEntryKey": 10,
                "championSkinName": "Annie",
                "mResourceResolver": 20,
                "resourceResolverEntryKey": 30,
            },
        )

    def test_archive_record_requires_output_identity_to_match_base(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            archive_data, pair = OutputArchiveTests().make_archive(Path(temp_name))
        collected = golden_outputs.inspect_archive_bytes(
            archive_data,
            relative_path="Goth Annie.zip",
            champion="Annie",
            skin_number=1,
            display_name="Goth Annie",
            expected_wad_name="Annie.wad.client",
            expected_units={"annie": pair},
            expected_path_hashes={pair.base_path: 1},
        )
        output_keys = {(1, "annie"): "output"}
        base_keys = {("annie", pair.base_sha256): "base"}
        converted = {
            "output": (identity_payload(), b"output-json"),
            "base": (identity_payload(skin_name="Different"), b"base-json"),
        }

        with self.assertRaises(golden_outputs.OutputGoldenError) as caught:
            golden_outputs.archive_record(
                collected,
                converted,
                output_keys,
                base_keys,
            )

        message = str(caught.exception)
        for value in (
            "champion=Annie",
            "skin=1",
            "unit=annie",
            "stage=rebased-bin-semantics",
        ):
            self.assertIn(value, message)


class BindingTests(unittest.TestCase):
    def test_benchmark_schema_one_is_rejected(self) -> None:
        benchmark = {
            "schemaVersion": 1,
            "selectedChampionIds": [1],
            "pool": {"champions": []},
            "runs": [],
        }

        with self.assertRaisesRegex(
            golden_outputs.OutputGoldenError,
            "unsupported benchmark result schema",
        ):
            golden_outputs.successful_cold_runs(benchmark)

    def test_selected_champion_missing_cold_run_is_rejected(self) -> None:
        benchmark = {
            "schemaVersion": 2,
            "currentInputStability": {"status": "passed"},
            "operationBaselineGate": {"status": "not_requested"},
            "selectedChampionIds": [1],
            "pool": {
                "champions": [
                    {
                        "championId": 1,
                        "query": "Annie",
                        "legacyExpectation": "success",
                    }
                ]
            },
            "runs": [],
        }

        with self.assertRaisesRegex(
            golden_outputs.OutputGoldenError,
            "benchmark-completeness",
        ):
            golden_outputs.successful_cold_runs(benchmark)

    def test_direct_expectation_promotes_legacy_unsupported_cold_run(
        self,
    ) -> None:
        champion = {
            "championId": 805,
            "query": "Locke",
            "legacyExpectation": "unsupported",
            "directExpectation": "success",
        }
        run = {
            "championId": 805,
            "scenario": "app-cold-build",
            "status": "success",
        }
        benchmark = {
            "schemaVersion": 2,
            "currentInputStability": {"status": "passed"},
            "operationBaselineGate": {"status": "not_requested"},
            "selectedChampionIds": [805],
            "pool": {"champions": [champion]},
            "runs": [run],
        }

        self.assertEqual(
            golden_outputs.successful_cold_runs(benchmark),
            [(champion, run)],
        )

    def test_failed_benchmark_gate_is_rejected_before_output_reads(self) -> None:
        benchmark = {
            "schemaVersion": 2,
            "currentInputStability": {"status": "failed"},
            "operationBaselineGate": {"status": "not_requested"},
            "selectedChampionIds": [1],
            "pool": {"champions": []},
            "runs": [],
        }

        with self.assertRaisesRegex(
            golden_outputs.OutputGoldenError,
            "currentInputStability",
        ):
            golden_outputs.successful_cold_runs(benchmark)

        benchmark["currentInputStability"]["status"] = "passed"
        benchmark["operationBaselineGate"]["status"] = "failed"
        with self.assertRaisesRegex(
            golden_outputs.OutputGoldenError,
            "operationBaselineGate",
        ):
            golden_outputs.successful_cold_runs(benchmark)

    def test_raw_metrics_selection_and_output_root_are_bound(self) -> None:
        expectation = golden_outputs.build_source_expectations(source_golden())[1]
        with tempfile.TemporaryDirectory() as temp_name:
            output_root = Path(temp_name) / "output"
            full_skin_id = 1001
            skin_set_sha256 = hashlib.sha256(b"[1001]").hexdigest()
            run = {
                "skinCount": 1,
                "expectedSkinCount": 1,
                "skinSetSha256": skin_set_sha256,
                "expectedSkinSetSha256": skin_set_sha256,
                "validationErrors": [],
                "metrics": {
                    "skinNumbers": [1],
                    "fullSkinIds": [full_skin_id],
                },
            }
            metrics = {
                "status": "success",
                "facts": {
                    "run": {"outputRoot": str(output_root)},
                    "selection": [
                        {
                            "championId": 1,
                            "skinNumber": 1,
                            "fullSkinId": full_skin_id,
                            "displayName": "Goth Annie",
                        }
                    ],
                },
            }
            selections = golden_outputs.validate_selection(
                run,
                metrics,
                {"query": "Annie", "skinCount": 1},
                expectation,
                output_root,
            )

        self.assertEqual(selections[0]["skinNumber"], 1)

    def test_bundled_ritobin_identity_must_match_benchmark(self) -> None:
        benchmark = {
            "identity": {
                "tools": [
                    {
                        "path": "bin\\ritobin_cli.exe",
                        "size": 10,
                        "sha256": "a" * 64,
                    }
                ]
            }
        }
        actual = {
            "path": "bin/ritobin_cli.exe",
            "size": 10,
            "sha256": "b" * 64,
        }

        with self.assertRaisesRegex(
            golden_outputs.OutputGoldenError,
            "tool-identity",
        ):
            golden_outputs.require_bundled_ritobin_identity(
                benchmark,
                actual,
            )

    def test_bundled_ritobin_support_files_must_match_benchmark(self) -> None:
        expected_sha = "a" * 64
        benchmark = {
            "identity": {
                "tools": [
                    {
                        "path": relative_path.as_posix(),
                        "size": 10,
                        "sha256": expected_sha,
                    }
                    for relative_path in (
                        golden_outputs.BUNDLED_RITOBIN_SUPPORT_RELATIVE_PATHS
                    )
                ]
            }
        }

        def identity(path: Path, _context: object) -> dict:
            return {
                "path": path.relative_to(golden_outputs.REPO_ROOT).as_posix(),
                "size": 10,
                "modifiedNs": 20,
                "sha256": expected_sha,
            }

        with patch.object(
            golden_outputs,
            "file_identity",
            side_effect=identity,
        ):
            actual = (
                golden_outputs.require_bundled_ritobin_support_identities(
                    benchmark
                )
            )
        self.assertEqual(
            len(actual),
            len(golden_outputs.BUNDLED_RITOBIN_SUPPORT_RELATIVE_PATHS),
        )

        benchmark["identity"]["tools"][0]["sha256"] = "b" * 64
        with (
            patch.object(
                golden_outputs,
                "file_identity",
                side_effect=identity,
            ),
            self.assertRaisesRegex(
                golden_outputs.OutputGoldenError,
                "tool-support-identity",
            ),
        ):
            golden_outputs.require_bundled_ritobin_support_identities(
                benchmark
            )

    def test_raw_source_wad_is_bound_to_benchmark_identity(self) -> None:
        identity = {
            "path": "B:/League/Annie.wad.client",
            "size": 10,
            "modifiedNs": 20,
        }
        metrics = {"facts": {"sourceWads": [dict(identity)]}}

        golden_outputs.require_raw_source_wad_identity(
            metrics,
            identity,
            "Annie",
        )

        metrics["facts"]["sourceWads"][0]["size"] = 11
        with self.assertRaisesRegex(
            golden_outputs.OutputGoldenError,
            "raw-source-identity",
        ):
            golden_outputs.require_raw_source_wad_identity(
                metrics,
                identity,
                "Annie",
            )

    def test_source_pool_binding_records_locke_and_wukong_alias(self) -> None:
        source = {
            "champions": [
                {
                    "championId": 62,
                    "champion": "Wukong",
                    "status": "success",
                    "wad": {"path": r"C:\League\MonkeyKing.wad.client"},
                },
                {
                    "championId": 805,
                    "champion": "Locke",
                    "status": "expected_unsupported",
                    "legacyFailure": {
                        "type": "SystemExit",
                        "message": "exact Locke failure",
                        "validated": True,
                    },
                },
            ]
        }
        pool = {
            "champions": [
                {
                    "championId": 62,
                    "query": "Wukong",
                    "wadName": "MonkeyKing.wad.client",
                    "legacyExpectation": "success",
                },
                {
                    "championId": 805,
                    "query": "Locke",
                    "wadName": "Locke.wad.client",
                    "legacyExpectation": "unsupported",
                    "legacyFailureType": "SystemExit",
                    "legacyFailureMessage": "exact Locke failure",
                },
            ]
        }

        unsupported = golden_outputs.validate_source_pool_bindings(
            source,
            pool,
        )

        self.assertEqual(
            unsupported,
            [
                {
                    "championId": 805,
                    "champion": "Locke",
                    "status": "expected_unsupported",
                    "legacyFailure": {
                        "type": "SystemExit",
                        "message": "exact Locke failure",
                        "validated": True,
                    },
                }
            ],
        )

    def test_source_pool_binding_records_direct_supported_locke(self) -> None:
        source = {
            "champions": [
                {
                    "championId": 805,
                    "champion": "Locke",
                    "status": "success",
                    "wad": {"path": r"C:\League\Locke.wad.client"},
                    "legacyFailure": {
                        "type": "SystemExit",
                        "message": "exact Locke failure",
                        "validated": True,
                    },
                    "directSupport": {
                        "status": "success",
                        "oracleVerified": True,
                    },
                }
            ]
        }
        pool = {
            "champions": [
                {
                    "championId": 805,
                    "query": "Locke",
                    "wadName": "Locke.wad.client",
                    "legacyExpectation": "unsupported",
                    "directExpectation": "success",
                    "legacyFailureType": "SystemExit",
                    "legacyFailureMessage": "exact Locke failure",
                }
            ]
        }

        unsupported = golden_outputs.validate_source_pool_bindings(
            source,
            pool,
        )

        self.assertEqual(
            unsupported[0]["status"],
            "direct_supported_legacy_unsupported",
        )

    def test_source_pool_binding_rejects_locke_contract_and_wukong_name(
        self,
    ) -> None:
        source = {
            "champions": [
                {
                    "championId": 62,
                    "champion": "Wukong",
                    "status": "success",
                    "wad": {"path": "MonkeyKing.wad.client"},
                },
                {
                    "championId": 805,
                    "champion": "Locke",
                    "status": "expected_unsupported",
                    "legacyFailure": {
                        "type": "SystemExit",
                        "message": "expected",
                        "validated": True,
                    },
                },
            ]
        }
        pool = {
            "champions": [
                {
                    "championId": 62,
                    "query": "Wukong",
                    "wadName": "MonkeyKing.wad.client",
                    "legacyExpectation": "success",
                },
                {
                    "championId": 805,
                    "query": "Locke",
                    "wadName": "Locke.wad.client",
                    "legacyExpectation": "unsupported",
                    "legacyFailureType": "SystemExit",
                    "legacyFailureMessage": "expected",
                },
            ]
        }
        wrong_name = json.loads(json.dumps(source))
        wrong_name["champions"][0]["champion"] = "MonkeyKing"
        with self.assertRaisesRegex(
            golden_outputs.OutputGoldenError,
            "champion query differs",
        ):
            golden_outputs.validate_source_pool_bindings(wrong_name, pool)

        wrong_failure = json.loads(json.dumps(source))
        wrong_failure["champions"][1]["legacyFailure"]["message"] = "partial"
        with self.assertRaisesRegex(
            golden_outputs.OutputGoldenError,
            "failure contract",
        ):
            golden_outputs.validate_source_pool_bindings(
                wrong_failure,
                pool,
            )


class ResultLifecycleTests(unittest.TestCase):
    def cli_args(self, root: Path) -> list[str]:
        return [
            "--benchmark-result",
            str(root / "benchmark.json"),
            "--source-golden",
            str(root / "source.json"),
            "--work-root",
            str(root / "work"),
            "--output",
            str(root / "result.json"),
        ]

    def test_interrupt_leaves_running_marker_not_partial_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output = root / "result.json"
            with (
                patch.object(
                    golden_outputs,
                    "run",
                    side_effect=KeyboardInterrupt,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                golden_outputs.main(self.cli_args(root))
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "running")
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["champions"], [])

    def test_expected_error_replaces_running_marker_with_failure(self) -> None:
        error = golden_outputs.OutputGoldenError(
            golden_outputs.global_context("test"),
            "boom",
        )
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            output = root / "result.json"
            with patch.object(
                golden_outputs,
                "run",
                side_effect=error,
            ):
                return_code = golden_outputs.main(self.cli_args(root))
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(return_code, 1)
        self.assertEqual(payload["status"], "failure")
        self.assertTrue(payload["complete"])
        self.assertIn("boom", payload["error"])

    def test_success_cannot_be_written_before_all_champions_complete(self) -> None:
        result = {
            "schemaVersion": 1,
            "status": "running",
            "complete": False,
            "champions": [{"championId": 1, "status": "success"}],
        }
        with tempfile.TemporaryDirectory() as temp_name:
            output = Path(temp_name) / "result.json"
            with self.assertRaisesRegex(
                golden_outputs.OutputGoldenError,
                "processed 1 champions, expected 2",
            ):
                golden_outputs.write_completed_result(
                    output,
                    result,
                    expected_count=2,
                    failed=False,
                )
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
