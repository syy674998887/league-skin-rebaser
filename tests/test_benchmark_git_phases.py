from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools import benchmark_git_phases


class GitPhaseBenchmarkTests(unittest.TestCase):
    def test_nodes_are_interleaved_by_phase(self) -> None:
        nodes = benchmark_git_phases.selected_nodes(
            ("old", "new"),
            (2, 1),
        )

        self.assertEqual(
            [(node.cohort, node.phase) for node in nodes],
            [
                ("old", 1),
                ("new", 1),
                ("old", 2),
                ("new", 2),
            ],
        )

    def test_unit_fixture_changes_only_auxiliary_units(self) -> None:
        registry = {
            "schemaVersion": 1,
            "champions": {
                "1": {
                    "alias": "Annie",
                    "wadBase": "Annie",
                    "mainUnit": "annie",
                    "auxiliaryUnits": ["annietibbers"],
                }
            },
        }
        fixture = {
            "schemaVersion": 1,
            "poolId": "test",
            "champions": {
                "1": {
                    "alias": "Annie",
                    "wadBase": "Annie",
                    "mainUnit": "annie",
                    "auxiliaryUnits": [
                        "annietibbers",
                        "jade_annie",
                    ],
                }
            },
        }
        with tempfile.TemporaryDirectory() as temp_name:
            runner = Path(temp_name)
            path = runner / "champion-units.generated.json"
            path.write_text(
                json.dumps(registry),
                encoding="utf-8",
            )

            original = benchmark_git_phases.apply_unit_fixture(
                runner,
                fixture,
            )
            changed = json.loads(path.read_text(encoding="utf-8"))

        self.assertIsNotNone(original)
        self.assertEqual(
            changed["champions"]["1"],
            {
                "alias": "Annie",
                "wadBase": "Annie",
                "mainUnit": "annie",
                "auxiliaryUnits": [
                    "annietibbers",
                    "jade_annie",
                ],
            },
        )

    def test_materialized_workload_requires_exact_counts(self) -> None:
        pool = {
            "champions": [{"championId": 1}],
            "totals": {
                "skins": 2,
                "paired": 3,
                "uniqueBase": 2,
            },
        }
        with tempfile.TemporaryDirectory() as temp_name:
            work_root = Path(temp_name)
            input_root = (
                work_root
                / "work"
                / "phase"
                / "1"
                / "input"
            )
            for skin, units in (
                ("skin-one", ("annie", "annietibbers")),
                ("skin-two", ("annie",)),
            ):
                for unit in units:
                    unit_root = input_root / skin / unit
                    unit_root.mkdir(parents=True, exist_ok=True)
                    (unit_root / "skin0.bin").write_bytes(b"base")
                    (unit_root / "skin1.bin").write_bytes(b"target")
            (input_root / "skin-one" / "step1").mkdir()

            workload = (
                benchmark_git_phases.inspect_materialized_workload(
                    work_root,
                    "phase",
                    pool,
                )
            )

        self.assertEqual(workload["skinCount"], 2)
        self.assertEqual(workload["pairedCount"], 3)
        self.assertEqual(workload["uniqueChampionUnits"], 2)
        self.assertTrue(workload["valid"])

    def test_comparison_requires_the_same_client_input(self) -> None:
        old = {
            "clientInputSha256": "a" * 64,
            "scenarios": {"app-cold-build": {"wallMs": 100.0}},
        }
        new = {
            "clientInputSha256": "a" * 64,
            "scenarios": {"app-cold-build": {"wallMs": 40.0}},
        }

        comparable = benchmark_git_phases.comparison_row(
            1,
            old,
            new,
            "app-cold-build",
        )
        new["clientInputSha256"] = "b" * 64
        changed = benchmark_git_phases.comparison_row(
            1,
            old,
            new,
            "app-cold-build",
        )

        self.assertTrue(comparable["sameClientInput"])
        self.assertEqual(comparable["deltaMs"], -60.0)
        self.assertEqual(comparable["deltaPercent"], -60.0)
        self.assertFalse(changed["sameClientInput"])
        self.assertIsNone(changed["deltaMs"])
        self.assertIsNone(changed["deltaPercent"])

    def test_adjacent_comparison_is_scoped_to_one_cohort(self) -> None:
        before = {
            "clientInputSha256": "a" * 64,
            "scenarios": {"app-cold-build": {"wallMs": 100.0}},
        }
        after = {
            "clientInputSha256": "a" * 64,
            "scenarios": {"app-cold-build": {"wallMs": 75.0}},
        }

        comparable = benchmark_git_phases.adjacent_comparison_row(
            "new",
            5,
            before,
            after,
            "app-cold-build",
        )
        after["clientInputSha256"] = "b" * 64
        changed = benchmark_git_phases.adjacent_comparison_row(
            "new",
            5,
            before,
            after,
            "app-cold-build",
        )

        self.assertEqual(comparable["cohort"], "new")
        self.assertEqual(comparable["fromPhase"], 5)
        self.assertEqual(comparable["toPhase"], 6)
        self.assertEqual(comparable["deltaMs"], -25.0)
        self.assertEqual(comparable["deltaPercent"], -25.0)
        self.assertFalse(changed["sameClientInput"])
        self.assertIsNone(changed["deltaMs"])
        self.assertIsNone(changed["deltaPercent"])

    def test_additional_derived_warm_phase_is_explicit(self) -> None:
        args = benchmark_git_phases.parse_args(
            ["--derived-warm-phase", "6"]
        )

        self.assertEqual(args.derived_warm_phase, [6])
        self.assertFalse(args.no_phase7_derived_warm)

    def test_derived_warm_selection_applies_to_any_phase(self) -> None:
        self.assertEqual(
            benchmark_git_phases.selected_scenarios(
                "app-cold-build,output-cache-hit",
                True,
            ),
            [
                "app-cold-build",
                "output-cache-hit",
                "derived-warm-build",
            ],
        )


if __name__ == "__main__":
    unittest.main()
