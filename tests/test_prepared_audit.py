from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from helpers.synthetic_wad import SyntheticChunk, write_synthetic_wad
from rebaser.wad_access import wad_path_hash


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "prepared_audit.py"
SPEC = importlib.util.spec_from_file_location("prepared_audit", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
prepared_audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = prepared_audit
SPEC.loader.exec_module(prepared_audit)


@dataclass
class AuditFixture:
    root: Path
    inputs: object
    alpha_wad: Path
    lcu_wad: Path
    pool_path: Path
    source_path: Path
    output_path: Path


class PreparedAuditTests(unittest.TestCase):
    def test_complete_audit_deduplicates_paths_and_passes_hard_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))

            result = prepared_audit.run_audit(fixture.inputs)

        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["complete"])
        self.assertEqual(result["identityGate"]["status"], "passed")
        self.assertEqual(result["hardGate"]["status"], "passed")
        self.assertEqual(
            result["metrics"],
            {
                "selectedChampions": 2,
                "comparableChampions": 1,
                "directOnlyChampions": 1,
                "comparableSkins": 2,
                "directOnlyDeclaredSkins": 1,
                "totalDeclaredSkins": 3,
                "skins": 2,
                "comparablePairReferences": 2,
                "directOnlyPairReferences": 1,
                "totalPairReferences": 3,
                "pairReferences": 2,
                "comparableUniqueBaseChunks": 1,
                "directOnlyBaseChunks": 1,
                "totalUniqueBaseChunks": 2,
                "uniqueBaseChunks": 1,
                "comparableLogicalPathReferences": 4,
                "directOnlyLogicalPathReferences": 2,
                "totalLogicalPathReferences": 6,
                "comparableUniqueChunks": 3,
                "directOnlyChunks": 2,
                "totalRequiredChunks": 5,
                "preparedSessions": 2,
                "wadIndexes": 2,
                "readManyCalls": 2,
                "physicalChunkReads": 5,
                "successfulChunkReads": 5,
                "failedChunkReads": 0,
                "compressionType0Reads": 5,
                "compressionType1Reads": 0,
                "compressionType3Reads": 0,
                "compressionOtherReads": 0,
                "shaComparisons": 3,
                "shaMismatches": 0,
                "missingRequiredPaths": 0,
                "unsupportedRequiredChunks": 0,
                "readFailures": 0,
                "lcuOfficialChampionIds": 2,
                "lcuOfficialComputedPaths": 4,
                "lcuOfficialWadHits": 4,
                "lcuOfficialWrongCompression": 0,
                "lcuOfficialNonZeroSubchunks": 0,
                "lcuOfficialDuplicateHits": 0,
                "lcuOfficialReadablePaths": 4,
                "lcuOfficialUnsupportedPaths": 0,
                "lcuOfficialReadFailures": 0,
                "lcuRegressionComputedPaths": 6,
                "lcuRegressionLegacyTableMissing": 6,
                "lcuRegressionLegacyTableMismatches": 0,
                "lcuRegressionWadHits": 6,
                "lcuRegressionWrongCompression": 0,
                "lcuRegressionNonZeroSubchunks": 0,
                "lcuRegressionDuplicateHits": 0,
                "lcuRegressionReadablePaths": 6,
                "lcuRegressionUnsupportedPaths": 0,
                "lcuRegressionReadFailures": 0,
                "lcuCombinedReadFailures": 0,
            },
        )
        alpha = result["champions"][0]
        self.assertEqual(alpha["logicalPathReferences"], 4)
        self.assertEqual(alpha["uniqueRequiredPaths"], 3)
        self.assertEqual(alpha["prepared"]["physicalChunkReads"], 3)
        self.assertEqual(alpha["prepared"]["successfulChunkReads"], 3)
        self.assertEqual(alpha["prepared"]["failedChunkReads"], 0)
        self.assertEqual(alpha["prepared"]["compressionReads"], {"0": 3})

    def test_formal_compression_distribution_is_a_hard_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))

            result = prepared_audit.run_audit(
                fixture.inputs,
                fixed_contract={
                    "compressionType0Reads": 0,
                    "compressionType3Reads": 5,
                },
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["hardGate"]["status"], "failed")
        self.assertEqual(
            result["hardGate"]["mismatches"]["compressionType3Reads"],
            {"expected": 5, "actual": 0},
        )

    def test_sha_mismatch_is_reported_and_fails_complete_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))
            source = read_json(fixture.source_path)
            source["champions"][0]["pairs"][0]["baseSha256"] = "0" * 64
            source["champions"][0]["pairs"][1]["baseSha256"] = "0" * 64
            write_json(fixture.source_path, source)

            result = prepared_audit.run_audit(fixture.inputs)

        self.assertEqual(result["identityGate"]["status"], "passed")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["metrics"]["shaComparisons"], 3)
        self.assertEqual(result["metrics"]["shaMismatches"], 1)
        self.assertEqual(result["hardGate"]["status"], "failed")
        self.assertEqual(
            result["champions"][0]["shaMismatches"][0]["path"],
            "data/characters/alpha/skins/skin0.bin",
        )

    def test_pair_semantics_are_bound_to_pool_skin_and_unit_contract(self) -> None:
        for case in (
            "context-skin",
            "source-skin-set",
            "context-unit",
            "target-skin",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_name:
                fixture = build_fixture(Path(temp_name))
                source = read_json(fixture.source_path)
                champion = source["champions"][0]
                if case == "context-skin":
                    champion["pairs"][0]["context"]["skin_number"] = 999
                elif case == "source-skin-set":
                    champion["skinSet"] = [1]
                elif case == "context-unit":
                    champion["pairs"][0]["context"]["unit"] = "wrong"
                else:
                    champion["pairs"][0]["targetPath"] = (
                        "data/characters/alpha/skins/skin2.bin"
                    )
                write_json(fixture.source_path, source)

                with patch.object(
                    prepared_audit,
                    "PreparedChampionWad",
                ) as prepared:
                    result = prepared_audit.run_audit(fixture.inputs)

                prepared.assert_not_called()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["identityGate"]["status"], "failed")
                self.assertFalse(
                    result["identityGate"]["preparedReadsStarted"],
                )

    def test_pool_identity_mismatch_fails_before_prepared_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))
            pool = read_json(fixture.pool_path)
            pool["description"] = "identity changed after Golden"
            write_json(fixture.pool_path, pool)

            with patch.object(
                prepared_audit,
                "PreparedChampionWad",
            ) as prepared:
                result = prepared_audit.run_audit(fixture.inputs)

        prepared.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["identityGate"]["status"], "failed")
        self.assertFalse(result["identityGate"]["preparedReadsStarted"])
        self.assertEqual(result["metrics"]["preparedSessions"], 0)

    def test_failed_source_golden_input_stability_fails_before_reads(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))
            source = read_json(fixture.source_path)
            source["inputStability"] = {
                "status": "failed",
                "executionSnapshot": {
                    "legacyTool": source["legacyTool"],
                    "hashSource": source["hashSource"],
                },
            }
            write_json(fixture.source_path, source)

            with patch.object(
                prepared_audit,
                "PreparedChampionWad",
            ) as prepared:
                result = prepared_audit.run_audit(fixture.inputs)

        prepared.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["identityGate"]["status"], "failed")
        self.assertFalse(result["identityGate"]["preparedReadsStarted"])

    def test_incomplete_source_golden_lifecycle_fails_before_reads(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))
            source = read_json(fixture.source_path)
            source.update(
                {
                    "status": "running",
                    "complete": False,
                    "expectedChampionCount": 2,
                    "processedChampionCount": 1,
                }
            )
            write_json(fixture.source_path, source)

            with patch.object(
                prepared_audit,
                "PreparedChampionWad",
            ) as prepared:
                result = prepared_audit.run_audit(fixture.inputs)

        prepared.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["identityGate"]["status"], "failed")
        self.assertFalse(result["identityGate"]["preparedReadsStarted"])

    def test_wad_identity_mismatch_fails_before_prepared_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))
            write_synthetic_wad(
                fixture.alpha_wad,
                [
                    wad_chunk(ALPHA_BASE, b"PROP-alpha-base-changed"),
                    wad_chunk(ALPHA_SKIN1, b"PROP-alpha-one"),
                    wad_chunk(ALPHA_SKIN2, b"PROP-alpha-two"),
                ],
                version_minor=4,
            )

            with patch.object(
                prepared_audit,
                "PreparedChampionWad",
            ) as prepared:
                result = prepared_audit.run_audit(fixture.inputs)

        prepared.assert_not_called()
        self.assertEqual(result["identityGate"]["status"], "failed")
        self.assertIn("Alpha WAD", result["identityGate"]["error"])

    def test_wad_replacement_after_identity_gate_cannot_pass_on_required_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))
            real_prepared = prepared_audit.PreparedChampionWad
            replaced = False

            def replace_after_gate(
                wad_path: Path,
                **kwargs: object,
            ) -> object:
                nonlocal replaced
                if Path(wad_path) == fixture.alpha_wad and not replaced:
                    replaced = True
                    write_synthetic_wad(
                        fixture.alpha_wad,
                        [
                            wad_chunk(ALPHA_BASE, b"PROP-alpha-base"),
                            wad_chunk(ALPHA_SKIN1, b"PROP-alpha-one"),
                            wad_chunk(ALPHA_SKIN2, b"PROP-alpha-two"),
                            wad_chunk(
                                "data/characters/alpha/skins/skin999.bin",
                                b"PROP-unrelated-replacement",
                            ),
                        ],
                        version_minor=4,
                    )
                return real_prepared(wad_path, **kwargs)

            with patch.object(
                prepared_audit,
                "PreparedChampionWad",
                side_effect=replace_after_gate,
            ):
                result = prepared_audit.run_audit(fixture.inputs)

        self.assertTrue(replaced)
        self.assertEqual(result["identityGate"]["status"], "passed")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["metrics"]["readFailures"], 1)
        self.assertEqual(result["champions"][0]["status"], "failed")
        self.assertIn(
            "SHA-bound snapshot",
            result["champions"][0]["error"]["message"],
        )

    def test_same_stat_wad_mutation_after_read_fails_full_sha_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))
            real_prepared = prepared_audit.PreparedChampionWad
            mutated = False

            class MutatingPrepared(real_prepared):
                def read_many(
                    self,
                    paths: Iterable[str],
                    *,
                    validate_bin: bool = False,
                ) -> dict[str, bytes]:
                    nonlocal mutated
                    payloads = super().read_many(
                        paths,
                        validate_bin=validate_bin,
                    )
                    if Path(self.wad_path) == fixture.alpha_wad and not mutated:
                        mutated = True
                        stat = fixture.alpha_wad.stat()
                        raw = bytearray(fixture.alpha_wad.read_bytes())
                        raw[-1] ^= 1
                        fixture.alpha_wad.write_bytes(raw)
                        os.utime(
                            fixture.alpha_wad,
                            ns=(stat.st_atime_ns, stat.st_mtime_ns),
                        )
                    return payloads

            with patch.object(
                prepared_audit,
                "PreparedChampionWad",
                MutatingPrepared,
            ):
                result = prepared_audit.run_audit(fixture.inputs)

        self.assertTrue(mutated)
        self.assertEqual(result["identityGate"]["status"], "passed")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["metrics"]["readFailures"], 1)
        self.assertIn(
            "SHA-bound snapshot",
            result["champions"][0]["error"]["message"],
        )

    def test_partial_selection_is_explicitly_not_a_hard_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))

            result = prepared_audit.run_audit(
                fixture.inputs,
                champion_names=["Alpha"],
            )

        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["complete"])
        self.assertEqual(result["selectedChampionIds"], [101])
        self.assertEqual(result["metrics"]["preparedSessions"], 1)
        self.assertEqual(result["metrics"]["readManyCalls"], 1)
        self.assertEqual(result["hardGate"]["status"], "not_applicable")
        self.assertEqual(
            result["hardGate"]["reason"],
            "partial champion selection",
        )

    def test_lcu_official_audit_discovers_ids_and_keeps_regression_separate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))

            result = prepared_audit.run_audit(fixture.inputs)

        official = result["lcuOfficialDataAudit"]
        self.assertEqual(official["officialChampionIds"], 2)
        self.assertEqual(official["championIds"], [101, 805])
        self.assertEqual(official["computedPaths"], 4)
        self.assertEqual(official["wadHits"], 4)
        self.assertEqual(official["wrongCompression"], 0)
        self.assertEqual(official["nonZeroSubchunks"], 0)
        self.assertEqual(official["duplicateHits"], 0)
        self.assertEqual(official["readablePaths"], 4)
        self.assertEqual(official["readFailures"], 0)
        self.assertEqual(
            official["excludedSummaryEntries"],
            {
                "nonObjectEntries": 1,
                "invalidChampionIds": 2,
                "missingBaseSkinRecords": 0,
            },
        )
        self.assertEqual(
            official["stages"]["officialIndexes"]["preparedSessions"],
            1,
        )
        self.assertEqual(
            official["stages"]["officialIndexes"]["readManyCalls"],
            1,
        )
        self.assertEqual(
            official["stages"]["officialChampionsAndRegressions"][
                "preparedSessions"
            ],
            1,
        )
        self.assertEqual(
            official["stages"]["officialChampionsAndRegressions"][
                "readManyCalls"
            ],
            1,
        )

        regression = result["lcuLegacyHashRegressionAudit"]
        self.assertEqual(regression["legacyTableMissing"], 6)
        self.assertEqual(regression["wadHits"], 6)
        self.assertEqual(regression["wrongCompression"], 0)
        self.assertEqual(regression["nonZeroSubchunks"], 0)
        self.assertEqual(regression["duplicateHits"], 0)
        self.assertEqual(regression["readablePaths"], 6)
        self.assertEqual(regression["readFailures"], 0)
        self.assertEqual(
            [item["championId"] for item in regression["paths"]],
            [799, 800, 804, 805, 893, 904],
        )
        for item in regression["paths"]:
            self.assertFalse(item["legacyTable"]["present"])
            self.assertEqual(len(item["computedPathHash"]), 16)
            self.assertEqual(item["wadHits"][0]["compressionType"], 3)
            self.assertEqual(item["read"]["status"], "passed")

    def test_lcu_official_ids_are_derived_from_the_installed_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))
            write_lcu_wad(fixture.lcu_wad, official_ids=(7, 101, 805))

            result = prepared_audit.run_audit(fixture.inputs)

        official = result["lcuOfficialDataAudit"]
        self.assertEqual(result["status"], "passed")
        self.assertEqual(official["championIds"], [7, 101, 805])
        self.assertEqual(official["officialChampionIds"], 3)
        self.assertEqual(official["computedPaths"], 5)
        self.assertEqual(official["readablePaths"], 5)
        self.assertEqual(result["metrics"]["lcuRegressionComputedPaths"], 6)

    def test_summary_mode_variant_without_base_skin_is_not_official(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))
            write_lcu_wad(
                fixture.lcu_wad,
                official_ids=(101, 805, 60001),
                base_skin_ids=(101, 805),
            )

            result = prepared_audit.run_audit(fixture.inputs)

        official = result["lcuOfficialDataAudit"]
        self.assertEqual(result["status"], "passed")
        self.assertEqual(official["championIds"], [101, 805])
        self.assertEqual(official["officialChampionIds"], 2)
        self.assertEqual(official["computedPaths"], 4)
        self.assertEqual(
            official["excludedSummaryEntries"]["missingBaseSkinRecords"],
            1,
        )

    def test_same_stat_lcu_mutation_after_read_fails_full_sha_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))
            real_prepared = prepared_audit.PreparedChampionWad
            mutated = False

            class MutatingLcuPrepared(real_prepared):
                def read_many(
                    self,
                    paths: Iterable[str],
                    *,
                    validate_bin: bool = False,
                ) -> dict[str, bytes]:
                    nonlocal mutated
                    payloads = super().read_many(
                        paths,
                        validate_bin=validate_bin,
                    )
                    if Path(self.wad_path) == fixture.lcu_wad and not mutated:
                        mutated = True
                        stat = fixture.lcu_wad.stat()
                        raw = bytearray(fixture.lcu_wad.read_bytes())
                        raw[-1] ^= 1
                        fixture.lcu_wad.write_bytes(raw)
                        os.utime(
                            fixture.lcu_wad,
                            ns=(stat.st_atime_ns, stat.st_mtime_ns),
                        )
                    return payloads

            with patch.object(
                prepared_audit,
                "PreparedChampionWad",
                MutatingLcuPrepared,
            ):
                result = prepared_audit.run_audit(fixture.inputs)

        self.assertTrue(mutated)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["hardGate"]["status"], "failed")
        self.assertGreater(result["metrics"]["lcuOfficialReadFailures"], 0)

    def test_invalid_lcu_summary_fails_the_complete_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))
            write_lcu_wad(
                fixture.lcu_wad,
                official_ids=(101, 805),
                summary={"not": "an array"},
            )

            result = prepared_audit.run_audit(fixture.inputs)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["hardGate"]["status"], "failed")
        self.assertEqual(result["lcuOfficialDataAudit"]["status"], "failed")
        self.assertIn(
            "must be a JSON array",
            result["lcuOfficialDataAudit"]["error"],
        )

    def test_mismatched_lcu_champion_json_id_fails_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))
            write_lcu_wad(
                fixture.lcu_wad,
                official_ids=(101, 805),
                champion_payload_overrides={101: {"id": 999}},
            )

            result = prepared_audit.run_audit(fixture.inputs)

        self.assertEqual(result["status"], "failed")
        official = result["lcuOfficialDataAudit"]
        self.assertEqual(official["status"], "failed")
        self.assertEqual(official["readFailures"], 1)
        record = next(
            item
            for item in official["paths"]
            if item["path"] == lcu_path(101)
        )
        self.assertEqual(record["read"]["status"], "invalid_schema")

    def test_boolean_lcu_champion_id_is_not_accepted_as_integer_one(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))
            write_lcu_wad(
                fixture.lcu_wad,
                official_ids=(1, 101, 805),
                champion_payload_overrides={1: {"id": True}},
            )

            result = prepared_audit.run_audit(fixture.inputs)

        self.assertEqual(result["status"], "failed")
        official = result["lcuOfficialDataAudit"]
        record = next(
            item
            for item in official["paths"]
            if item["path"] == lcu_path(1)
        )
        self.assertEqual(record["read"]["status"], "invalid_schema")

    def test_duplicate_lcu_toc_hit_is_not_read_and_fails_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))
            duplicate_wad = fixture.lcu_wad.parent / "duplicate.wad"
            write_synthetic_wad(
                duplicate_wad,
                [
                    wad_chunk(
                        lcu_path(101),
                        json.dumps({"id": 101}, separators=(",", ":")).encode(),
                        compression_type=3,
                    )
                ],
                version_minor=4,
            )

            result = prepared_audit.run_audit(fixture.inputs)

        official = result["lcuOfficialDataAudit"]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(official["duplicateHits"], 1)
        self.assertEqual(official["readablePaths"], 3)
        duplicate = next(
            item for item in official["paths"] if item["path"] == lcu_path(101)
        )
        self.assertEqual(duplicate["read"]["status"], "ambiguous")

    def test_unsupported_lcu_subchunk_is_reported_and_fails_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))
            write_lcu_wad(
                fixture.lcu_wad,
                official_ids=(101, 805),
                nonzero_subchunk_paths={lcu_path(101)},
            )

            result = prepared_audit.run_audit(fixture.inputs)

        official = result["lcuOfficialDataAudit"]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(official["wrongCompression"], 0)
        self.assertEqual(official["nonZeroSubchunks"], 1)
        self.assertEqual(official["unsupportedPaths"], 1)
        unsupported = next(
            item for item in official["paths"] if item["path"] == lcu_path(101)
        )
        self.assertEqual(unsupported["read"]["status"], "unsupported")

    def test_wrong_lcu_compression_is_reported_and_fails_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))
            write_lcu_wad(
                fixture.lcu_wad,
                official_ids=(101, 805),
                wrong_compression_paths={lcu_path(101)},
            )

            result = prepared_audit.run_audit(fixture.inputs)

        official = result["lcuOfficialDataAudit"]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(official["wrongCompression"], 1)
        self.assertEqual(official["nonZeroSubchunks"], 0)
        self.assertEqual(official["unsupportedPaths"], 1)

    def test_cli_writes_schema_one_result_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))
            inputs = fixture.inputs

            return_code = prepared_audit.main(
                [
                    "--pool",
                    str(inputs.pool),
                    "--source-golden",
                    str(inputs.source_golden),
                    "--output",
                    str(inputs.output),
                    "--config",
                    str(inputs.config),
                    "--hashes-game",
                    str(inputs.hash_source),
                    "--legacy-tool",
                    str(inputs.legacy_tool),
                    "--lcu-hashes",
                    str(inputs.lcu_hashes),
                ]
            )
            result = read_json(fixture.output_path)
            leftovers = list(
                fixture.output_path.parent.glob(
                    f".{fixture.output_path.name}.*.tmp"
                )
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(result["schemaVersion"], 1)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(leftovers, [])

    def test_relevant_hash_source_line_mismatch_fails_before_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            fixture = build_fixture(Path(temp_name))
            fixture.inputs.hash_source.write_text(
                "0000000000000001 "
                "data/characters/alpha/skins/skin0.bin\n",
                encoding="utf-8",
            )

            with patch.object(
                prepared_audit,
                "PreparedChampionWad",
            ) as prepared:
                result = prepared_audit.run_audit(fixture.inputs)

        prepared.assert_not_called()
        self.assertEqual(result["identityGate"]["status"], "failed")
        self.assertEqual(result["identityGate"]["phase"], "hash-source")
        self.assertIn("xxh64 mismatch", result["identityGate"]["error"].lower())


ALPHA_BASE = "data/characters/alpha/skins/skin0.bin"
ALPHA_SKIN1 = "data/characters/alpha/skins/skin1.bin"
ALPHA_SKIN2 = "data/characters/alpha/skins/skin2.bin"
LOCKE_BASE = "data/characters/locke/skins/skin0.bin"
LOCKE_SKIN1 = "data/characters/locke/skins/skin1.bin"


def build_fixture(root: Path) -> AuditFixture:
    league_root = root / "League"
    champions_dir = league_root / "Game" / "DATA" / "FINAL" / "Champions"
    champions_dir.mkdir(parents=True)
    metadata_path = league_root / "Game" / "content-metadata.json"
    write_json(metadata_path, {"version": "16.15.7996036+fixture"})

    alpha_payloads = {
        ALPHA_BASE: b"PROP-alpha-base",
        ALPHA_SKIN1: b"PROP-alpha-one",
        ALPHA_SKIN2: b"PROP-alpha-two",
    }
    alpha_wad = champions_dir / "Alpha.wad.client"
    write_synthetic_wad(
        alpha_wad,
        [
            wad_chunk(path, payload)
            for path, payload in alpha_payloads.items()
        ],
        version_minor=4,
    )
    locke_wad = champions_dir / "Locke.wad.client"
    write_synthetic_wad(
        locke_wad,
        [
            wad_chunk(LOCKE_BASE, b"PROP-locke-base"),
            wad_chunk(LOCKE_SKIN1, b"PROP-locke-one"),
        ],
        version_minor=4,
    )

    lcu_dir = league_root / prepared_audit.LCU_GAME_DATA_REL
    lcu_dir.mkdir(parents=True)
    lcu_wad = lcu_dir / "game-data.wad"
    write_lcu_wad(lcu_wad, official_ids=(101, 805))

    pool = {
        "schemaVersion": 1,
        "poolId": "prepared-audit-fixture",
        "gameVersion": "16.15.799.6036",
        "champions": [
            {
                "championId": 101,
                "query": "Alpha",
                "wadName": "Alpha.wad.client",
                "mainUnit": "alpha",
                "skinSet": {"ranges": [[1, 2]], "exclude": []},
                "skinCount": 2,
                "pairedCount": 2,
                "uniqueBaseCount": 1,
                "legacyExpectation": "success",
            },
            {
                "championId": 805,
                "query": "Locke",
                "wadName": "Locke.wad.client",
                "mainUnit": "locke",
                "skinSet": {"ranges": [[1, 1]], "exclude": []},
                "skinCount": 1,
                "pairedCount": 1,
                "uniqueBaseCount": 1,
                "legacyExpectation": "unsupported",
            },
        ],
    }
    pool_path = root / "pool.json"
    write_json(pool_path, pool)

    hash_source = root / "hashes.game.txt"
    hash_source.write_text(
        "".join(
            f"{wad_path_hash(path):016x} {path}\n"
            for path in alpha_payloads
        ),
        encoding="utf-8",
    )
    legacy_tool = root / "wad-extract.exe"
    legacy_tool.write_bytes(b"fixture tool identity")
    lcu_hashes = root / "hashes.lcu.txt"
    lcu_hashes.write_text(
        f"{wad_path_hash('unrelated/path'):016x} unrelated/path\n",
        encoding="utf-8",
    )
    config = root / "config.json"
    write_json(config, {"lol_path": str(league_root)})

    source = {
        "schemaVersion": 2,
        "poolId": pool["poolId"],
        "gameVersion": pool["gameVersion"],
        "pool": prepared_audit.stable_file_identity(pool_path),
        "client": {
            "expectedVersion": pool["gameVersion"],
            "actualVersion": "16.15.7996036+fixture",
            "comparableVersion": "16.15.7996036",
            "source": prepared_audit.stable_file_identity(metadata_path),
        },
        "hashSource": prepared_audit.stable_file_identity(hash_source),
        "legacyTool": prepared_audit.stable_file_identity(legacy_tool),
        "champions": [
            {
                "championId": 101,
                "champion": "Alpha",
                "status": "success",
                "skinSet": [1, 2],
                "skinCount": 2,
                "pairedCount": 2,
                "uniqueBaseCount": 1,
                "pairs": [
                    pair_record(
                        "Alpha",
                        1,
                        ALPHA_BASE,
                        alpha_payloads[ALPHA_BASE],
                        ALPHA_SKIN1,
                        alpha_payloads[ALPHA_SKIN1],
                    ),
                    pair_record(
                        "Alpha",
                        2,
                        ALPHA_BASE,
                        alpha_payloads[ALPHA_BASE],
                        ALPHA_SKIN2,
                        alpha_payloads[ALPHA_SKIN2],
                    ),
                ],
                "wad": prepared_audit.stable_file_identity(alpha_wad),
            },
            {
                "championId": 805,
                "champion": "Locke",
                "status": "expected_unsupported",
                "declaredPairCount": 1,
                "declaredUniqueBaseCount": 1,
                "wad": prepared_audit.stable_file_identity(locke_wad),
            },
        ],
    }
    source_path = root / "source-golden.json"
    write_json(source_path, source)
    output_path = root / "prepared-result.json"
    inputs = prepared_audit.AuditInputs(
        pool=pool_path,
        source_golden=source_path,
        output=output_path,
        config=config,
        hash_source=hash_source,
        legacy_tool=legacy_tool,
        lcu_hashes=lcu_hashes,
    )
    return AuditFixture(
        root=root,
        inputs=inputs,
        alpha_wad=alpha_wad,
        lcu_wad=lcu_wad,
        pool_path=pool_path,
        source_path=source_path,
        output_path=output_path,
    )


def wad_chunk(
    path: str,
    payload: bytes,
    *,
    compression_type: int = 0,
) -> SyntheticChunk:
    return SyntheticChunk(
        path_hash=wad_path_hash(path),
        payload=payload,
        compression_type=compression_type,
    )


def lcu_path(champion_id: int) -> str:
    return (
        "plugins/rcp-be-lol-game-data/global/default/v1/"
        f"champions/{champion_id}.json"
    )


def write_lcu_wad(
    path: Path,
    *,
    official_ids: tuple[int, ...],
    summary: object | None = None,
    nonzero_subchunk_paths: set[str] | None = None,
    wrong_compression_paths: set[str] | None = None,
    champion_payload_overrides: dict[int, object] | None = None,
    base_skin_ids: tuple[int, ...] | None = None,
) -> None:
    if summary is None:
        summary = [
            {"id": -1, "name": "None"},
            {"id": "invalid", "name": "Invalid"},
            "non-object",
            *(
                {"id": champion_id, "name": f"Champion {champion_id}"}
                for champion_id in official_ids
            ),
        ]
    champion_payloads = {
        champion_id: {"id": champion_id}
        for champion_id in (
            *official_ids,
            *prepared_audit.LCU_REGRESSION_IDS,
        )
    }
    champion_payloads.update(champion_payload_overrides or {})
    official_base_ids = official_ids if base_skin_ids is None else base_skin_ids
    payloads: dict[str, object] = {
        prepared_audit.LCU_CHAMPION_SUMMARY_PATH: summary,
        prepared_audit.LCU_SKINS_PATH: {
            str(champion_id * 1000): {
                "id": champion_id * 1000,
                "isBase": True,
            }
            for champion_id in official_base_ids
        },
        **{
            lcu_path(champion_id): value
            for champion_id, value in champion_payloads.items()
        },
    }
    chunks: list[SyntheticChunk] = []
    nonzero = nonzero_subchunk_paths or set()
    wrong_compression = wrong_compression_paths or set()
    for chunk_path, value in payloads.items():
        chunks.append(
            SyntheticChunk(
                path_hash=wad_path_hash(chunk_path),
                payload=json.dumps(value, separators=(",", ":")).encode(),
                compression_type=4 if chunk_path in wrong_compression else 3,
                subchunk_count=1 if chunk_path in nonzero else 0,
            )
        )
    write_synthetic_wad(path, chunks, version_minor=4)


def pair_record(
    champion: str,
    skin_number: int,
    base_path: str,
    base_payload: bytes,
    target_path: str,
    target_payload: bytes,
) -> dict[str, object]:
    return {
        "context": {
            "champion": champion,
            "skin_number": skin_number,
            "unit": "alpha",
            "stage": "phase0-direct-legacy-bytes",
        },
        "basePath": base_path,
        "targetPath": target_path,
        "baseSha256": hashlib.sha256(base_payload).hexdigest(),
        "targetSha256": hashlib.sha256(target_payload).hexdigest(),
    }


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
