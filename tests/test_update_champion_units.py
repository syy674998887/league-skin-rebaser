from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from rebaser.champion_layout import ChampionIdentity
from tests.helpers.synthetic_wad import SyntheticChunk, write_synthetic_wad
from rebaser.maintenance.champion_units import (
    HashSourceError,
    UnitUpdaterError,
    _audit_champion_with_retry,
    locate_official_wad,
    main,
    official_target_skin_numbers,
    parse_args,
    probe_unit_paths,
    retain_existing_candidates,
    scan_hash_source,
    wad_error_category,
)
from rebaser.wad_access import (
    CorruptWad,
    PreparedChampionWad,
    UnsupportedWadVersion,
    WadChangedDuringRead,
    WadVersion,
    wad_path_hash,
)

import script


def annie_identity() -> ChampionIdentity:
    return ChampionIdentity(
        champion_id=1,
        display_name="Annie",
        alias="Annie",
        wad_base="Annie",
        main_unit="annie",
    )


class CommandLineTests(unittest.TestCase):
    def test_check_is_default_and_write_is_explicit_and_exclusive(
        self,
    ) -> None:
        default = parse_args([])
        self.assertTrue(default.check)
        self.assertFalse(default.write)
        write = parse_args(["--write"])
        self.assertTrue(write.write)
        self.assertFalse(write.check)
        with (
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            parse_args(["--check", "--write"])

    def test_exit_codes_distinguish_clean_change_and_degraded(self) -> None:
        empty_categories = {
            "added": [],
            "seen": [],
            "not_seen": [],
            "base_only": [],
            "target_only": [],
            "ambiguous_hash": [],
            "missing_wad": [],
            "unsupported_wad": [],
            "incomplete_source": [],
        }
        cases = (
            (False, False, 0),
            (True, False, 1),
            (False, True, 2),
        )
        for changed, blocking, expected in cases:
            with self.subTest(
                changed=changed,
                blocking=blocking,
            ):
                categories = {
                    key: list(value)
                    for key, value in empty_categories.items()
                }
                if blocking:
                    categories["incomplete_source"].append(
                        {"error": "fixture"}
                    )
                report = {
                    "status": "failed" if blocking else "passed",
                    "categories": categories,
                    "champions": [],
                }
                with (
                    patch(
                        "rebaser.maintenance.champion_units.resolve_champions_dir",
                        return_value=Path("Champions"),
                    ),
                    patch(
                        "rebaser.maintenance.champion_units.build_audit",
                        return_value=(
                            report,
                            {"schemaVersion": 1, "champions": {}},
                            changed,
                            blocking,
                        ),
                    ),
                    patch(
                        "rebaser.maintenance.champion_units.protected_wad_paths",
                        return_value=(),
                    ),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(main([]), expected)

    def test_report_cannot_alias_registry_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            registry = Path(temp_name) / "registry.json"
            original = b'{"schemaVersion":1,"champions":{}}\n'
            registry.write_bytes(original)

            with redirect_stderr(io.StringIO()):
                exit_code = main(
                    [
                        "--registry",
                        str(registry),
                        "--report",
                        str(registry),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertEqual(registry.read_bytes(), original)

    def test_report_cannot_overwrite_champion_wad(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            champions_dir = Path(temp_name) / "Champions"
            champions_dir.mkdir()
            wad_path = champions_dir / "Annie.wad.client"
            original = b"fixture WAD"
            wad_path.write_bytes(original)
            with (
                patch(
                    "rebaser.maintenance.champion_units.resolve_champions_dir",
                    return_value=champions_dir,
                ),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = main(["--report", str(wad_path)])

            self.assertEqual(exit_code, 2)
            self.assertEqual(wad_path.read_bytes(), original)

    def test_lcu_failure_still_writes_incomplete_source_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            champions_dir = root / "Champions"
            champions_dir.mkdir()
            report_path = root / "failure.json"
            error = UnitUpdaterError("LCU source unavailable")
            with (
                patch(
                    "rebaser.maintenance.champion_units.resolve_champions_dir",
                    return_value=champions_dir,
                ),
                patch(
                    "rebaser.maintenance.champion_units.protected_wad_paths",
                    return_value=(),
                ),
                patch(
                    "rebaser.maintenance.champion_units.build_audit",
                    side_effect=error,
                ),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = main(["--report", str(report_path)])

            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 2)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(
            report["categories"]["incomplete_source"][0]["source"],
            "audit input",
        )
        self.assertIn(
            "LCU source unavailable",
            report["categories"]["incomplete_source"][0]["error"],
        )

    def test_report_write_failure_returns_two_without_traceback(self) -> None:
        report = {
            "status": "passed",
            "categories": {
                "added": [],
                "seen": [],
                "not_seen": [],
                "base_only": [],
                "target_only": [],
                "ambiguous_hash": [],
                "missing_wad": [],
                "unsupported_wad": [],
                "incomplete_source": [],
            },
            "champions": [],
        }
        with (
            patch(
                "rebaser.maintenance.champion_units.resolve_champions_dir",
                return_value=Path("Champions"),
            ),
            patch(
                "rebaser.maintenance.champion_units.protected_wad_paths",
                return_value=(),
            ),
            patch(
                "rebaser.maintenance.champion_units.build_audit",
                return_value=(
                    report,
                    {"schemaVersion": 1, "champions": {}},
                    False,
                    False,
                ),
            ),
            patch(
                "rebaser.maintenance.champion_units.script.write_json_atomically",
                side_effect=OSError("disk full"),
            ),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(["--report", "report.json"]), 2)

    def test_write_revalidates_under_lock_and_replaces_registry(self) -> None:
        categories = {
            "added": [{"unit": "newpet"}],
            "seen": [],
            "not_seen": [],
            "base_only": [],
            "target_only": [],
            "ambiguous_hash": [],
            "missing_wad": [],
            "unsupported_wad": [],
            "incomplete_source": [],
        }
        report = {
            "status": "passed",
            "mode": "check",
            "inputs": {"stable": True},
            "categories": categories,
            "champions": [{"championId": 1}],
        }
        proposed = {
            "schemaVersion": 1,
            "champions": {
                "1": {
                    "alias": "Annie",
                }
            },
        }
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            registry = root / "registry.json"
            registry.write_text(
                '{"schemaVersion":1,"champions":{}}\n',
                encoding="utf-8",
            )
            with (
                patch(
                    "rebaser.maintenance.champion_units.resolve_champions_dir",
                    return_value=root,
                ),
                patch(
                    "rebaser.maintenance.champion_units.protected_wad_paths",
                    return_value=(),
                ),
                patch(
                    "rebaser.maintenance.champion_units.build_audit",
                    side_effect=(
                        (report, proposed, True, False),
                        (report, proposed, True, False),
                    ),
                ) as audit,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = main(
                    [
                        "--write",
                        "--registry",
                        str(registry),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(audit.call_count, 2)
            self.assertEqual(
                json.loads(registry.read_text(encoding="utf-8")),
                proposed,
            )

    def test_write_no_diff_does_not_replace_or_create_lock(self) -> None:
        categories = {
            "added": [],
            "seen": [],
            "not_seen": [],
            "base_only": [],
            "target_only": [],
            "ambiguous_hash": [],
            "missing_wad": [],
            "unsupported_wad": [],
            "incomplete_source": [],
        }
        report = {
            "status": "passed",
            "inputs": {"stable": True},
            "categories": categories,
            "champions": [],
        }
        proposed = {"schemaVersion": 1, "champions": {}}
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            registry = root / "registry.json"
            original = b'{"schemaVersion":1,"champions":{}}\n'
            registry.write_bytes(original)
            with (
                patch(
                    "rebaser.maintenance.champion_units.resolve_champions_dir",
                    return_value=root,
                ),
                patch(
                    "rebaser.maintenance.champion_units.protected_wad_paths",
                    return_value=(),
                ),
                patch(
                    "rebaser.maintenance.champion_units.build_audit",
                    return_value=(report, proposed, False, False),
                ) as audit,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = main(
                    [
                        "--write",
                        "--registry",
                        str(registry),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(audit.call_count, 1)
            self.assertEqual(registry.read_bytes(), original)
            self.assertFalse(
                (root / f".{registry.name}.lock").exists()
            )

    def test_write_revalidation_failure_keeps_original_registry(self) -> None:
        categories = {
            "added": [{"unit": "newpet"}],
            "seen": [],
            "not_seen": [],
            "base_only": [],
            "target_only": [],
            "ambiguous_hash": [],
            "missing_wad": [],
            "unsupported_wad": [],
            "incomplete_source": [],
        }
        initial_report = {
            "status": "passed",
            "inputs": {"generation": 1},
            "categories": categories,
            "champions": [],
        }
        changed_report = {
            **initial_report,
            "inputs": {"generation": 2},
        }
        proposed = {
            "schemaVersion": 1,
            "champions": {"1": {"alias": "Annie"}},
        }
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            registry = root / "registry.json"
            original = b'{"schemaVersion":1,"champions":{}}\n'
            registry.write_bytes(original)
            with (
                patch(
                    "rebaser.maintenance.champion_units.resolve_champions_dir",
                    return_value=root,
                ),
                patch(
                    "rebaser.maintenance.champion_units.protected_wad_paths",
                    return_value=(),
                ),
                patch(
                    "rebaser.maintenance.champion_units.build_audit",
                    side_effect=(
                        (initial_report, proposed, True, False),
                        (changed_report, proposed, True, False),
                    ),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = main(
                    [
                        "--write",
                        "--registry",
                        str(registry),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertEqual(registry.read_bytes(), original)
            self.assertEqual(
                list(root.glob(f".{registry.name}.*.tmp")),
                [],
            )


class OfficialWadSelectionTests(unittest.TestCase):
    def test_only_exact_official_wad_is_selected(self) -> None:
        champion = annie_identity()
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            official = root / "Annie.wad.client"
            for name in (
                official.name,
                "Annie.en_US.wad.client",
                "Ruby_Annie.wad.client",
                "Strawberry_Annie.wad.client",
                "TFTChampion.wad.client",
            ):
                (root / name).write_bytes(b"fixture")

            selected = locate_official_wad(champion, root)

        self.assertEqual(selected, official)


class HashSourceTests(unittest.TestCase):
    def test_scan_streams_relevant_paths_and_hashes_exact_input_bytes(
        self,
    ) -> None:
        path0 = "data/characters/annie/skins/skin0.bin"
        path1 = "data/characters/annietibbers/skins/skin1.bin"
        content = (
            f"{wad_path_hash(path0):016x} {path0}\n"
            "0000000000000000 assets/unrelated.texture\n"
            f"{wad_path_hash(path1):016x} {path1}\n"
        ).encode()
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "hashes.game.txt"
            source.write_bytes(content)

            scan = scan_hash_source(source)

        self.assertEqual(scan.lines, 3)
        self.assertEqual(scan.relevant_lines, 2)
        self.assertEqual(len(scan.records), 2)
        self.assertEqual(
            scan.identity["sha256"],
            hashlib.sha256(content).hexdigest(),
        )
        self.assertEqual(
            scan.records[wad_path_hash(path1)].unit,
            "annietibbers",
        )

    def test_relevant_xxh64_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "hashes.game.txt"
            source.write_text(
                "0000000000000001 "
                "data/characters/annie/skins/skin0.bin\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(HashSourceError, "XXH64 mismatch"):
                scan_hash_source(source)

    def test_distinct_paths_with_one_hash_are_reported_as_ambiguous(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "hashes.game.txt"
            source.write_text(
                (
                    "0000000000000001 "
                    "data/characters/annie/skins/skin0.bin\n"
                    "0000000000000001 "
                    "data/characters/annietibbers/skins/skin0.bin\n"
                ),
                encoding="utf-8",
            )
            with patch(
                "rebaser.maintenance.champion_units.wad_path_hash",
                return_value=1,
            ):
                scan = scan_hash_source(source)

        self.assertEqual(len(scan.ambiguous_hashes), 1)
        self.assertEqual(
            scan.ambiguous_hashes[0]["pathHash"],
            "0000000000000001",
        )

    def test_hash_source_final_content_identity_mismatch_fails_closed(
        self,
    ) -> None:
        path0 = "data/characters/annie/skins/skin0.bin"
        with tempfile.TemporaryDirectory() as temp_name:
            source = Path(temp_name) / "hashes.game.txt"
            source.write_text(
                f"{wad_path_hash(path0):016x} {path0}\n",
                encoding="utf-8",
            )
            with (
                patch(
                    "rebaser.maintenance.champion_units.stable_file_identity",
                    return_value={
                        "path": str(source.resolve()),
                        "size": source.stat().st_size,
                        "modifiedNs": source.stat().st_mtime_ns,
                        "sha256": "0" * 64,
                    },
                ),
                self.assertRaisesRegex(
                    HashSourceError,
                    "changed after scanning",
                ),
            ):
                scan_hash_source(source)


class CandidateUpdateTests(unittest.TestCase):
    def test_not_seen_candidates_are_retained_without_deletion(self) -> None:
        self.assertEqual(
            retain_existing_candidates(
                ("historicpet",),
                ("newpet",),
            ),
            ("historicpet", "newpet"),
        )

    def test_direct_probe_does_not_depend_on_hash_source_text(self) -> None:
        paths = [
            "data/characters/annie/skins/skin0.bin",
            "data/characters/annietibbers/skins/skin1.bin",
        ]
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "Annie.wad.client"
            chunks = [
                SyntheticChunk(
                    path_hash=wad_path_hash(path),
                    payload=b"PROPfixture",
                    compression_type=0,
                )
                for path in paths
            ]
            chunks.sort(key=lambda chunk: chunk.path_hash)
            write_synthetic_wad(
                wad_path,
                chunks,
                version_minor=4,
            )

            observed = probe_unit_paths(
                PreparedChampionWad(wad_path),
                ("annie", "annietibbers", "missingpet"),
                (1,),
            )

        self.assertEqual(observed["annie"], {0})
        self.assertEqual(observed["annietibbers"], {1})
        self.assertEqual(observed["missingpet"], set())

    def test_official_skin_above_999_blocks_identity_schema(self) -> None:
        source = object()
        record = type("Record", (), {"data": {}, "source": source})()
        catalog = script.OfficialNameCatalog(
            champion_id=1,
            names_by_skin_number={
                0: "Annie",
                1000: "Out of schema",
            },
        )
        with (
            patch(
                "rebaser.maintenance.champion_units.script.load_lcu_json_with_identity",
                return_value=record,
            ),
            patch(
                "rebaser.maintenance.champion_units.script.parse_official_name_catalog",
                return_value=catalog,
            ),
            self.assertRaisesRegex(
                UnitUpdaterError,
                "outside the fullSkinId 0..999 schema",
            ),
        ):
            official_target_skin_numbers(
                annie_identity(),
                Path("Champions"),
                (),
            )


class WadFailurePolicyTests(unittest.TestCase):
    def test_only_capability_errors_are_unsupported(self) -> None:
        unsupported = UnsupportedWadVersion(
            Path("Annie.wad.client"),
            WadVersion(4, 0),
        )
        corrupt = CorruptWad(
            Path("Annie.wad.client"),
            "fixture corruption",
        )

        self.assertEqual(
            wad_error_category(unsupported),
            "unsupported_wad",
        )
        self.assertEqual(
            wad_error_category(corrupt),
            "incomplete_source",
        )

    def test_wad_change_retries_whole_champion_once(self) -> None:
        changed = WadChangedDuringRead(
            Path("Annie.wad.client"),
            None,
            None,
        )
        sentinel = object()
        with patch(
            "rebaser.maintenance.champion_units._audit_champion_once",
            side_effect=(changed, sentinel),
        ) as audit:
            result = _audit_champion_with_retry(fixture=True)

        self.assertIs(result, sentinel)
        self.assertEqual(audit.call_count, 2)

    def test_persistent_wad_change_fails_after_one_retry(self) -> None:
        changed = WadChangedDuringRead(
            Path("Annie.wad.client"),
            None,
            None,
        )
        with (
            patch(
                "rebaser.maintenance.champion_units._audit_champion_once",
                side_effect=(changed, changed),
            ) as audit,
            self.assertRaises(WadChangedDuringRead),
        ):
            _audit_champion_with_retry(fixture=True)

        self.assertEqual(audit.call_count, 2)


if __name__ == "__main__":
    unittest.main()
