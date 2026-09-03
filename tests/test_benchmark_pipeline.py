from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers.synthetic_wad import SyntheticChunk, write_synthetic_wad


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "benchmark_pipeline.py"
)
SPEC = importlib.util.spec_from_file_location("benchmark_pipeline", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
benchmark_pipeline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark_pipeline)


class BenchmarkPoolTests(unittest.TestCase):
    @staticmethod
    def load_upgrade_pool() -> dict:
        return json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "benchmarks"
                / "pools"
                / "upgrade-v1.json"
            ).read_text(encoding="utf-8")
        )

    def test_default_pool_is_the_current_upgrade_v2_snapshot(self) -> None:
        self.assertEqual(
            benchmark_pipeline.DEFAULT_POOL,
            (
                Path(__file__).resolve().parents[1]
                / "benchmarks"
                / "pools"
                / "upgrade-v2.json"
            ),
        )

    def test_source_identity_includes_runtime_layout_inputs(self) -> None:
        paths = set(
            benchmark_pipeline.source_file_paths(
                benchmark_pipeline.DEFAULT_POOL
            )
        )
        root = Path(__file__).resolve().parents[1]

        self.assertIn(
            (root / "data" / "champion-units.generated.json").resolve(),
            paths,
        )

    def test_fast5_pool_is_an_exact_full_pool_subset(self) -> None:
        root = Path(__file__).resolve().parents[1]
        full = benchmark_pipeline.load_pool(
            root / "benchmarks" / "pools" / "upgrade-v2.json"
        )
        fast = benchmark_pipeline.load_pool(
            root / "benchmarks" / "pools" / "upgrade-v2-fast5.json"
        )
        expected_ids = [1, 74, 62, 805, 142]
        full_by_id = {
            champion["championId"]: champion
            for champion in full["champions"]
        }

        self.assertEqual(fast["poolId"], "upgrade-v2-fast5")
        self.assertEqual(
            [champion["championId"] for champion in fast["champions"]],
            expected_ids,
        )
        for champion in fast["champions"]:
            parent = full_by_id[champion["championId"]]
            self.assertEqual(champion, parent)
            self.assertEqual(
                benchmark_pipeline.expected_full_skin_ids(champion),
                benchmark_pipeline.expected_full_skin_ids(parent),
            )

        self.assertEqual(
            fast["totals"],
            {
                "champions": 5,
                "skins": 172,
                "paired": 362,
                "uniqueBase": 11,
                "currentRebaseRitobinProcesses": 1086,
                "phase5LogicalConversions": 735,
            },
        )
        self.assertEqual(
            fast["commonSuccess"],
            {
                "excludeChampionIds": [805],
                "champions": 4,
                "skins": 163,
                "paired": 353,
                "uniqueBase": 10,
            },
        )

    def test_phase4_fast5_snapshot_keeps_membership_and_requires_direct_locke(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        previous = benchmark_pipeline.load_pool(
            root / "benchmarks" / "pools" / "upgrade-v2-fast5.json"
        )
        refreshed = benchmark_pipeline.load_pool(
            root / "benchmarks" / "pools" / "upgrade-v3-fast5.json"
        )

        self.assertEqual(
            [item["championId"] for item in refreshed["champions"]],
            [item["championId"] for item in previous["champions"]],
        )
        self.assertEqual(refreshed["totals"]["champions"], 5)
        self.assertEqual(refreshed["totals"]["skins"], 173)
        annie = refreshed["champions"][0]
        self.assertEqual(benchmark_pipeline.expand_skin_set(annie)[-1], 60)
        locke = next(
            item for item in refreshed["champions"] if item["query"] == "Locke"
        )
        self.assertEqual(locke["legacyExpectation"], "unsupported")
        self.assertEqual(
            benchmark_pipeline.runtime_expectation(locke),
            "success",
        )
        self.assertEqual(
            refreshed["commonSuccess"]["excludeChampionIds"],
            [],
        )

    def test_runtime_tool_manifest_binds_executables_and_dictionaries(self) -> None:
        self.assertEqual(
            benchmark_pipeline.TOOL_RUNTIME_RELATIVE_PATHS,
            (
                Path("bin") / "ritobin_cli.exe",
                Path("bin") / "hashes" / "hashes.binentries.txt",
                Path("bin") / "hashes" / "hashes.binfields.txt",
                Path("bin") / "hashes" / "hashes.binhashes.txt",
                Path("bin") / "hashes" / "hashes.bintypes.txt",
                Path("bin") / "hashes" / "hashes.game.txt.0",
                Path("bin") / "hashes" / "hashes.game.txt.1",
                Path("bin") / "hashes" / "hashes.lcu.txt",
                Path("cslol-tools") / "wad-extract.exe",
                Path("cslol-tools") / "hashes.game.txt",
                Path("cslol-tools") / "wad-make.exe",
            ),
        )

    def test_upgrade_pool_has_stable_unique_ids_and_expected_totals(self) -> None:
        pool = benchmark_pipeline.load_pool(
            Path(__file__).resolve().parents[1]
            / "benchmarks"
            / "pools"
            / "upgrade-v1.json"
        )

        self.assertEqual(pool["poolId"], "upgrade-v1")
        self.assertEqual(pool["totals"]["champions"], 10)
        self.assertEqual(pool["totals"]["skins"], 411)
        self.assertEqual(pool["totals"]["paired"], 918)
        self.assertEqual(
            len({item["championId"] for item in pool["champions"]}),
            10,
        )
        locke = next(
            item
            for item in pool["champions"]
            if item["query"] == "Locke"
        )
        self.assertEqual(locke["legacyExpectation"], "unsupported")
        self.assertEqual(locke["legacyFailureType"], "SystemExit")
        self.assertEqual(
            locke["legacyFailureMessage"],
            "no data/characters directory found after extracting Locke.wad.client",
        )
        self.assertEqual(
            benchmark_pipeline.expand_skin_set(locke),
            list(range(1, 10)),
        )

    def test_pool_payload_is_bound_to_the_bytes_that_were_parsed(self) -> None:
        payload = self.load_upgrade_pool()
        raw = (
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "pool.json"
            path.write_bytes(raw)
            loaded, identity = benchmark_pipeline.load_pool_with_identity(path)

            mutated = json.loads(raw.decode("utf-8"))
            mutated["poolId"] = "upgrade-x1"
            mutated_raw = (
                json.dumps(mutated, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            self.assertEqual(len(mutated_raw), len(raw))
            path.write_bytes(mutated_raw)
            os.utime(
                path,
                ns=(path.stat().st_atime_ns, identity["modifiedNs"]),
            )
            mutated_loaded, mutated_identity = (
                benchmark_pipeline.load_pool_with_identity(path)
            )

        self.assertEqual(loaded, payload)
        self.assertEqual(identity["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(mutated_loaded, mutated)
        self.assertNotEqual(identity["sha256"], mutated_identity["sha256"])

    def test_safe_reset_is_confined_to_benchmark_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            benchmark_pipeline.ensure_scratch_root(root, allow_initialize=True)
            target = root / "work" / "phase-0" / "1"
            target.mkdir(parents=True)
            (target / "old.txt").write_text("old", encoding="utf-8")
            cache_marker = target / "cache" / "keep.txt"
            cache_marker.parent.mkdir()
            cache_marker.write_text("cold cache", encoding="utf-8")

            benchmark_pipeline.safe_reset_directory(target, root)

            self.assertTrue(target.is_dir())
            self.assertFalse((target / "old.txt").exists())
            self.assertFalse(cache_marker.exists())
            with self.assertRaisesRegex(ValueError, "refusing"):
                benchmark_pipeline.safe_reset_directory(root, root)
            with self.assertRaisesRegex(ValueError, "refusing"):
                benchmark_pipeline.safe_reset_directory(
                    root.parent / "outside",
                    root,
                )
            with self.assertRaisesRegex(ValueError, "anything except"):
                benchmark_pipeline.safe_reset_directory(
                    root / "results" / "phase-0" / "1",
                    root,
                )

    def test_custom_scratch_root_requires_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            with self.assertRaisesRegex(ValueError, "must already contain"):
                benchmark_pipeline.ensure_scratch_root(
                    root,
                    allow_initialize=False,
                )
            benchmark_pipeline.ensure_scratch_root(root, allow_initialize=True)
            self.assertEqual(
                benchmark_pipeline.ensure_scratch_root(
                    root,
                    allow_initialize=False,
                ),
                root.resolve(),
            )

    def test_safe_reset_rejects_reparse_component(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            benchmark_pipeline.ensure_scratch_root(root, allow_initialize=True)
            target = root / "work" / "phase-0" / "1"
            target.mkdir(parents=True)
            original = benchmark_pipeline._is_reparse_point

            def fake_is_reparse(path: Path) -> bool:
                return path == root / "work" or original(path)

            with mock.patch.object(
                benchmark_pipeline,
                "_is_reparse_point",
                side_effect=fake_is_reparse,
            ):
                with self.assertRaisesRegex(ValueError, "reparse"):
                    benchmark_pipeline.safe_reset_directory(target, root)

    def test_scenarios_are_canonical_and_derived_is_explicit(self) -> None:
        self.assertEqual(
            benchmark_pipeline.resolve_scenarios(
                "app-cold-build,output-cache-hit",
                include_derived_warm=False,
            ),
            ["app-cold-build", "output-cache-hit"],
        )
        self.assertEqual(
            benchmark_pipeline.resolve_scenarios(
                "app-cold-build,output-cache-hit",
                include_derived_warm=True,
            ),
            list(benchmark_pipeline.SCENARIOS),
        )
        invalid = (
            "",
            "output-cache-hit",
            "app-cold-build,app-cold-build",
            "app-cold-build,derived-warm-build",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    benchmark_pipeline.resolve_scenarios(
                        value,
                        include_derived_warm=False,
                    )

    def test_derived_resets_only_io_and_never_forces_archive_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            champion_root = Path(temp_name) / "champion"
            input_marker = champion_root / "input" / "old-input.txt"
            output_marker = champion_root / "output" / "old-output.txt"
            cache_marker = champion_root / "cache" / "keep-cache.txt"
            for marker in (input_marker, output_marker, cache_marker):
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(marker.name, encoding="utf-8")

            benchmark_pipeline.prepare_scenario_directories(
                champion_root,
                "output-cache-hit",
            )
            self.assertTrue(input_marker.exists())
            self.assertTrue(output_marker.exists())
            self.assertTrue(cache_marker.exists())

            input_root, output_root, cache_root = (
                benchmark_pipeline.prepare_scenario_directories(
                    champion_root,
                    "derived-warm-build",
                )
            )

            self.assertTrue(input_root.is_dir())
            self.assertTrue(output_root.is_dir())
            self.assertFalse(input_marker.exists())
            self.assertFalse(output_marker.exists())
            self.assertEqual(cache_root, cache_marker.parent)
            self.assertEqual(
                cache_marker.read_text(encoding="utf-8"),
                "keep-cache.txt",
            )
            command = benchmark_pipeline.scenario_command(
                Path("python.exe"),
                {"query": "Hwei"},
                "derived-warm-build",
                Path("metrics.json"),
            )
            self.assertNotIn("--force", command)
            self.assertIn("--hash-update", command)
            self.assertEqual(
                command[command.index("--hash-update") + 1],
                "never",
            )

    def test_scenario_environment_always_includes_fixed_cache_root(self) -> None:
        with mock.patch.dict(os.environ, {"KEEP": "yes"}, clear=True):
            env = benchmark_pipeline.scenario_environment(
                Path("input"),
                Path("output"),
                Path("cache"),
            )
        self.assertEqual(env["KEEP"], "yes")
        self.assertEqual(env["LEAGUE_SKIN_REBASER_INPUT_ROOT"], "input")
        self.assertEqual(env["LEAGUE_SKIN_REBASER_OUTPUT_ROOT"], "output")
        self.assertEqual(env["LEAGUE_SKIN_REBASER_CACHE_ROOT"], "cache")

    def test_derived_rejects_reparse_before_deleting_io(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            champion_root = Path(temp_name) / "champion"
            input_marker = champion_root / "input" / "keep.txt"
            input_marker.parent.mkdir(parents=True)
            input_marker.write_text("keep", encoding="utf-8")
            original = benchmark_pipeline._is_reparse_point

            def fake_is_reparse(path: Path) -> bool:
                return path == input_marker.parent or original(path)

            with mock.patch.object(
                benchmark_pipeline,
                "_is_reparse_point",
                side_effect=fake_is_reparse,
            ):
                with self.assertRaisesRegex(ValueError, "reparse"):
                    benchmark_pipeline.prepare_scenario_directories(
                        champion_root,
                        "derived-warm-build",
                    )
            self.assertTrue(input_marker.exists())

    def test_compact_operations_removes_scope_but_preserves_tool_labels(self) -> None:
        metrics = {
            "operations": [
                {
                    "name": "process.attempts",
                    "labels": {
                        "champion": "Annie",
                        "skin": "Goth Annie",
                        "unit": "annie",
                        "tool": "ritobin-file",
                    },
                    "value": 1,
                },
                {
                    "name": "process.attempts",
                    "labels": {
                        "champion": "Annie",
                        "skin": "Red Riding Annie",
                        "unit": "annie",
                        "tool": "ritobin-file",
                    },
                    "value": 2,
                },
            ]
        }

        compact = benchmark_pipeline.compact_operations(metrics)

        self.assertEqual(
            compact,
            [
                {
                    "name": "process.attempts",
                    "labels": {"tool": "ritobin-file"},
                    "value": 3,
                }
            ],
        )

    def test_invalid_duplicate_pool_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "pool.json"
            payload = self.load_upgrade_pool()
            payload["champions"].append(dict(payload["champions"][0]))
            path.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicate"):
                benchmark_pipeline.load_pool(path)

    def test_metrics_schema_and_exact_selection_are_validated(self) -> None:
        champion = self.load_upgrade_pool()["champions"][0]
        expected_numbers = benchmark_pipeline.expand_skin_set(champion)
        selection = [
            {
                "championId": champion["championId"],
                "skinNumber": number,
                "fullSkinId": champion["championId"] * 1000 + number,
            }
            for number in expected_numbers
        ]
        metrics = {
            "schemaVersion": 1,
            "status": "success",
            "error": None,
            "timing": {"summary": {}},
            "operations": [],
            "facts": {"selection": selection},
        }
        summary = benchmark_pipeline.summarize_metrics(metrics)
        self.assertEqual(
            benchmark_pipeline.selection_validation_errors(
                champion,
                summary["selection"],
            ),
            [],
        )
        duplicate = [*summary["selection"][:-1], summary["selection"][0]]
        self.assertTrue(
            benchmark_pipeline.selection_validation_errors(
                champion,
                duplicate,
            )
        )
        metrics["schemaVersion"] = 999
        with self.assertRaisesRegex(ValueError, "schema"):
            benchmark_pipeline.summarize_metrics(metrics)

    def test_aggregate_never_sums_a_partial_fixed_cohort(self) -> None:
        pool = benchmark_pipeline.load_pool(
            Path(__file__).resolve().parents[1]
            / "benchmarks"
            / "pools"
            / "upgrade-v1.json"
        )
        selected = [910, 1]
        runs = [
            {
                "championId": 910,
                "scenario": "app-cold-build",
                "status": "success",
                "wallMs": 100.0,
                "skinCount": 20,
            },
            {
                "championId": 1,
                "scenario": "app-cold-build",
                "status": "failure",
                "wallMs": 1.0,
                "skinCount": 0,
            },
        ]
        aggregate = benchmark_pipeline.aggregate_runs(
            pool,
            runs,
            selected_champion_ids=selected,
            scenarios=["app-cold-build"],
        )["app-cold-build"]
        cohort = aggregate["fixedComparableCohort"]
        self.assertFalse(cohort["comparable"])
        self.assertIsNone(cohort["wallMs"])
        self.assertEqual(cohort["nonSuccessChampionIds"], [1])


class OperationBaselineGateTests(unittest.TestCase):
    @staticmethod
    def clone(payload: dict) -> dict:
        return json.loads(json.dumps(payload))

    @classmethod
    def make_result(cls) -> dict:
        byte_operations = [
            {
                "name": name,
                "labels": {"source": "fixture"},
                "value": index,
            }
            for index, name in enumerate(
                sorted(benchmark_pipeline.BYTE_VOLUME_OPERATION_NAMES),
                start=1,
            )
        ]
        return {
            "schemaVersion": benchmark_pipeline.RESULT_SCHEMA_VERSION,
            "pool": {
                "poolId": "operation-gate-fixture",
                "gameVersion": "99.1.2.3",
                "champions": [
                    {"championId": 1, "query": "Annie"},
                    {"championId": 805, "query": "Locke"},
                ],
            },
            "scenarios": [
                "app-cold-build",
                "output-cache-hit",
            ],
            "selectedChampionIds": [1, 805],
            "identity": {
                "python": {
                    "executable": "python.exe",
                    "executableSha256": "python-sha",
                    "version": "3.fixture",
                },
                "tools": [
                    {
                        "path": "bin/ritobin_cli.exe",
                        "size": 10,
                        "sha256": "tool-sha",
                    }
                ],
                "client": {
                    "declaredVersion": "99.1.2.3",
                    "actualVersion": "99.1.2.3",
                    "executable": {
                        "path": "League of Legends.exe",
                        "size": 20,
                        "modifiedNs": 100,
                        "sha256": "exe-sha",
                    },
                    "wads": [
                        {
                            "championId": 1,
                            "path": "Annie.wad.client",
                            "size": 30,
                            "tocSha256": "annie-toc",
                            "sha256": "annie-wad",
                        },
                        {
                            "championId": 805,
                            "path": "Locke.wad.client",
                            "size": 40,
                            "tocSha256": "locke-toc",
                            "sha256": "locke-wad",
                        },
                    ],
                    "lcuWads": [
                        {
                            "path": "game-data.wad",
                            "size": 50,
                            "modifiedNs": 200,
                            "tocSha256": "lcu-toc",
                        }
                    ],
                },
            },
            "runs": [
                {
                    "championId": 1,
                    "scenario": "app-cold-build",
                    "status": "success",
                    "metrics": {
                        "operations": [
                            {
                                "name": "process.attempts",
                                "labels": {
                                    "tool": "ritobin-file",
                                    "mode": "skin",
                                },
                                "value": 7,
                            },
                            {
                                "name": "custom.bytes",
                                "labels": {},
                                "value": 11,
                            },
                            *byte_operations,
                        ]
                    },
                },
                {
                    "championId": 1,
                    "scenario": "output-cache-hit",
                    "status": "success",
                    "metrics": {
                        "operations": [
                            {
                                "name": "cache.archive.hits",
                                "labels": {},
                                "value": 3,
                            }
                        ]
                    },
                },
                {
                    "championId": 805,
                    "scenario": "app-cold-build",
                    "status": "expected_unsupported",
                    "metrics": {
                        "operations": [
                            {
                                "name": "wad.copy.attempts",
                                "labels": {},
                                "value": 1,
                            }
                        ]
                    },
                },
                {
                    "championId": 805,
                    "scenario": "output-cache-hit",
                    "status": "expected_skipped",
                    "reason": "app-cold-build did not succeed",
                },
            ],
        }

    @staticmethod
    def baseline_identity() -> dict:
        return {
            "path": "baseline.json",
            "size": 123,
            "modifiedNs": 456,
            "sha256": "baseline-sha",
        }

    @staticmethod
    def cold_operations(result: dict) -> list[dict]:
        return next(
            run["metrics"]["operations"]
            for run in result["runs"]
            if run["championId"] == 1
            and run["scenario"] == "app-cold-build"
        )

    def test_exact_structure_passes_and_only_explicit_byte_volumes_are_ignored(
        self,
    ) -> None:
        baseline = self.make_result()
        current = self.clone(baseline)
        for operation in self.cold_operations(current):
            if operation["name"] in benchmark_pipeline.BYTE_VOLUME_OPERATION_NAMES:
                operation["value"] += 1000

        gate = benchmark_pipeline.compare_operation_baseline(
            current,
            baseline,
            self.baseline_identity(),
        )

        self.assertEqual(gate["status"], "passed")
        self.assertEqual(gate["comparability"]["status"], "passed")
        self.assertEqual(
            gate["excludedOperationNames"],
            sorted(benchmark_pipeline.BYTE_VOLUME_OPERATION_NAMES),
        )
        self.assertEqual(gate["summary"]["comparedRuns"], 4)
        skipped = next(
            comparison
            for comparison in gate["runComparisons"]
            if comparison["championId"] == 805
            and comparison["scenario"] == "output-cache-hit"
        )
        self.assertEqual(skipped["status"], "passed")
        self.assertEqual(skipped["missingOperations"], [])
        self.assertEqual(skipped["unexpectedOperations"], [])

    def test_missing_extra_value_and_label_changes_fail_exactly(self) -> None:
        def remove_operation(current: dict) -> None:
            operations = self.cold_operations(current)
            operations[:] = [
                item
                for item in operations
                if item["name"] != "process.attempts"
            ]

        def add_operation(current: dict) -> None:
            self.cold_operations(current).append(
                {
                    "name": "catalog.lookups",
                    "labels": {"kind": "skin"},
                    "value": 1,
                }
            )

        def change_non_excluded_bytes(current: dict) -> None:
            operation = next(
                item
                for item in self.cold_operations(current)
                if item["name"] == "custom.bytes"
            )
            operation["value"] += 1

        def change_label(current: dict) -> None:
            operation = next(
                item
                for item in self.cold_operations(current)
                if item["name"] == "process.attempts"
            )
            operation["labels"]["tool"] = "different-tool"

        cases = (
            ("missing", remove_operation, "missingOperations"),
            ("extra", add_operation, "unexpectedOperations"),
            ("value", change_non_excluded_bytes, "changedOperations"),
            ("label", change_label, "missingOperations"),
        )
        baseline = self.make_result()
        for name, mutate, expected_field in cases:
            with self.subTest(name=name):
                current = self.clone(baseline)
                mutate(current)
                gate = benchmark_pipeline.compare_operation_baseline(
                    current,
                    baseline,
                    self.baseline_identity(),
                )
                comparison = next(
                    item
                    for item in gate["runComparisons"]
                    if item["championId"] == 1
                    and item["scenario"] == "app-cold-build"
                )
                self.assertEqual(gate["status"], "failed")
                self.assertEqual(comparison["status"], "failed")
                self.assertTrue(comparison[expected_field])
                if name == "label":
                    self.assertTrue(comparison["unexpectedOperations"])

    def test_every_comparability_identity_mismatch_rejects_the_gate(self) -> None:
        def change_pool(current: dict) -> None:
            current["pool"]["gameVersion"] = "different"

        def change_selected(current: dict) -> None:
            current["selectedChampionIds"].reverse()

        def change_scenarios(current: dict) -> None:
            current["scenarios"].reverse()

        def remove_run_key(current: dict) -> None:
            current["runs"].pop()

        def change_wad(current: dict) -> None:
            current["identity"]["client"]["wads"][0]["tocSha256"] = "different"

        def change_lcu_wad(current: dict) -> None:
            current["identity"]["client"]["lcuWads"][0]["tocSha256"] = "different"

        def change_tool(current: dict) -> None:
            current["identity"]["tools"][0]["sha256"] = "different"

        def change_executable(current: dict) -> None:
            current["identity"]["client"]["executable"]["sha256"] = "different"

        def change_python(current: dict) -> None:
            current["identity"]["python"]["executableSha256"] = "different"

        cases = (
            ("pool", change_pool, "pool"),
            ("selected", change_selected, "selectedChampionIds"),
            ("scenarios", change_scenarios, "scenarios"),
            ("run-keys", remove_run_key, "runs.keys"),
            ("python", change_python, "identity.python"),
            ("champion-wad", change_wad, "identity.client.wads"),
            ("lcu-wad", change_lcu_wad, "identity.client.lcuWads"),
            ("tool", change_tool, "identity.tools"),
            (
                "client-executable",
                change_executable,
                "identity.client.executable",
            ),
        )
        baseline = self.make_result()
        for name, mutate, expected_field in cases:
            with self.subTest(name=name):
                current = self.clone(baseline)
                mutate(current)
                gate = benchmark_pipeline.compare_operation_baseline(
                    current,
                    baseline,
                    self.baseline_identity(),
                )
                fields = {
                    item["field"]
                    for item in gate["comparability"]["mismatches"]
                }
                self.assertEqual(gate["status"], "failed")
                self.assertEqual(gate["comparability"]["status"], "failed")
                self.assertIn(expected_field, fields)
                self.assertEqual(gate["runComparisons"], [])

    def test_baseline_file_sha_identity_is_stable_and_published(self) -> None:
        payload = self.make_result()
        raw = (
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "baseline.json"
            path.write_bytes(raw)

            loaded, identity = benchmark_pipeline.read_json_with_identity(path)
            gate = benchmark_pipeline.compare_operation_baseline_file(
                self.clone(payload),
                path,
                loaded,
                identity,
            )

        self.assertEqual(loaded, payload)
        self.assertEqual(identity["path"], str(path.resolve()))
        self.assertEqual(identity["size"], len(raw))
        self.assertEqual(identity["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(gate["baseline"], identity)
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(gate["sourceStability"]["status"], "passed")

    def test_baseline_file_same_stat_mutation_fails_final_source_gate(
        self,
    ) -> None:
        payload = self.make_result()
        raw = (
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "baseline.json"
            path.write_bytes(raw)
            loaded, identity = benchmark_pipeline.read_json_with_identity(path)
            mutated = raw.replace(
                b"operation-gate-fixture",
                b"operation-gate-fixturX",
                1,
            )
            self.assertEqual(len(mutated), len(raw))
            self.assertNotEqual(mutated, raw)
            path.write_bytes(mutated)
            os.utime(
                path,
                ns=(
                    path.stat().st_atime_ns,
                    int(identity["modifiedNs"]),
                ),
            )

            gate = benchmark_pipeline.compare_operation_baseline_file(
                self.clone(payload),
                path,
                loaded,
                identity,
            )

        self.assertEqual(gate["status"], "failed")
        self.assertEqual(gate["sourceStability"]["status"], "failed")
        self.assertEqual(gate["comparability"]["status"], "not_evaluated")

    def test_current_input_snapshot_full_hashes_lcu_wads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            lcu_wad = Path(temp_name) / "game-data.wad"
            write_synthetic_wad(
                lcu_wad,
                [
                    SyntheticChunk(
                        path_hash=1,
                        payload=b"before",
                        compression_type=0,
                    )
                ],
                version_minor=4,
            )
            toc_identity = benchmark_pipeline.wad_toc_identity(lcu_wad)
            identity = {
                "sourceFiles": [],
                "python": {},
                "tools": [],
                "client": {
                    "configuredRoot": temp_name,
                    "declaredVersion": "1",
                    "actualVersion": "1",
                    "executable": {},
                    "wads": [],
                    "lcuWads": [toc_identity],
                },
            }
            starting = benchmark_pipeline.current_input_snapshot(identity)
            write_synthetic_wad(
                lcu_wad,
                [
                    SyntheticChunk(
                        path_hash=1,
                        payload=b"after!",
                        compression_type=0,
                    )
                ],
                version_minor=4,
            )
            os.utime(
                lcu_wad,
                ns=(
                    lcu_wad.stat().st_atime_ns,
                    toc_identity["modifiedNs"],
                ),
            )
            self.assertEqual(
                benchmark_pipeline.wad_toc_identity(lcu_wad),
                toc_identity,
            )
            ending = benchmark_pipeline.current_input_snapshot(identity)

        self.assertNotEqual(
            starting["client"]["lcuWads"][0]["sha256"],
            ending["client"]["lcuWads"][0]["sha256"],
        )

    def test_failed_or_pending_operation_gate_affects_exit_status(self) -> None:
        for status, expected in (
            ("not_requested", False),
            ("passed", False),
            ("pending", True),
            ("failed", True),
        ):
            with self.subTest(status=status):
                result = {
                    "runs": [],
                    "currentInputStability": {"status": "passed"},
                    "operationBaselineGate": {"status": status},
                }
                self.assertEqual(
                    benchmark_pipeline.benchmark_result_failed(result),
                    expected,
                )

    def test_failed_current_inputs_block_operation_comparison(self) -> None:
        gate = (
            benchmark_pipeline.operation_baseline_blocked_by_current_inputs(
                self.baseline_identity(),
                {"status": "failed"},
            )
        )

        self.assertEqual(gate["status"], "failed")
        self.assertEqual(gate["sourceStability"]["status"], "not_evaluated")
        self.assertEqual(gate["comparability"]["status"], "not_evaluated")
        self.assertEqual(gate["currentInputStabilityStatus"], "failed")
        self.assertEqual(gate["runComparisons"], [])

    def test_failed_or_pending_current_input_gate_affects_exit_status(
        self,
    ) -> None:
        for status, expected in (
            ("pending", True),
            ("failed", True),
            ("passed", False),
        ):
            with self.subTest(status=status):
                result = {
                    "runs": [],
                    "currentInputStability": {"status": status},
                    "operationBaselineGate": {"status": "not_requested"},
                }
                self.assertEqual(
                    benchmark_pipeline.benchmark_result_failed(result),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
