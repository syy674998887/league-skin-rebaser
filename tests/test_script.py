from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from rebaser.champion_layout import ChampionIdentity

import script


class ConfigurationTests(unittest.TestCase):
    def test_configured_cache_root_owns_all_runtime_cache_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            env = dict(os.environ)
            env["LEAGUE_SKIN_REBASER_CACHE_ROOT"] = temp_name
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import json, script; "
                        "print(json.dumps([str(script.CACHE_ROOT), "
                        "str(script.HASH_UPDATE_STATE_PATH.parent), "
                        "str(script.HASH_SKIN_INDEX_PATH.parent), "
                        "str(script.DERIVED_CACHE_ROOT.parent)]))"
                    ),
                ],
                cwd=script.SCRIPT_DIR,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )

        roots = json.loads(result.stdout)
        self.assertEqual(roots, [temp_name] * 4)
        self.assertEqual(script.DERIVED_CACHE_ROOT.name, "derived")

    def test_archive_tool_identity_reports_missing_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            ritobin = root / "bin" / "ritobin_cli.exe"
            wad_make = root / "cslol-tools" / "wad-make.exe"
            ritobin.parent.mkdir()
            wad_make.parent.mkdir()

            for missing, present, expected in (
                (ritobin, wad_make, "ritobin_cli.exe"),
                (wad_make, ritobin, "wad-make.exe"),
            ):
                with self.subTest(tool=expected):
                    missing.unlink(missing_ok=True)
                    present.write_bytes(b"tool")
                    with (
                        patch.object(script, "RITOBIN_CLI", ritobin),
                        patch.object(script, "WAD_MAKE", wad_make),
                        self.assertRaises(SystemExit) as raised,
                    ):
                        script.capture_archive_tool_identities()

                    self.assertIn(expected, str(raised.exception))
                    self.assertIn("run setup.bat", str(raised.exception))


def make_skin(*, chroma: bool = False) -> script.LocalSkin:
    return script.LocalSkin(
        champion_name="Annie",
        champion_id=1,
        skin_number=2 if chroma else 1,
        display_name="Goth Annie (Ruby)" if chroma else "Goth Annie",
        base_display_name="Goth Annie",
        internal_name="AnnieGothChroma" if chroma else "AnnieGoth",
        skinline="goth",
        parent_skin_number=1 if chroma else None,
        is_chroma=chroma,
        aliases=(),
    )


def make_plan(
    root: Path,
    archive_format: str,
    *,
    chroma: bool = False,
) -> script.ArchivePlan:
    source_wad = root / "source" / "Annie.wad.client"
    source_wad.parent.mkdir(parents=True, exist_ok=True)
    if not source_wad.exists():
        source_wad.write_bytes(b"RWsource-wad")
    skin = make_skin(chroma=chroma)
    return script.create_archive_plan(
        skin,
        source_wad,
        skin.display_name,
        "1.0.0",
        script.archive_extensions(archive_format),
        input_root=root / "input",
        output_root=root / "output",
    )


def write_plan_archive(
    root: Path,
    plan: script.ArchivePlan,
    archive_format: str,
) -> list[Path]:
    step4 = root / f"stage-{archive_format}"
    wad_dir = step4 / "WAD"
    meta_dir = step4 / "META"
    wad_dir.mkdir(parents=True)
    meta_dir.mkdir()
    (wad_dir / plan.wad_name).write_bytes(b"RWfake-wad")
    (meta_dir / "info.json").write_text(
        json.dumps(plan.info),
        encoding="utf-8",
    )
    return script.write_mod_archives(
        step4,
        wad_dir,
        meta_dir,
        plan.output_dir,
        plan.disk_name,
        plan.wad_name,
        archive_format,
    )


def json_entry(name: str, key: str, fields: dict[str, object]) -> dict[str, object]:
    return {
        "key": key,
        "value": {
            "name": name,
            "items": [
                {"key": field_name, "value": value}
                for field_name, value in fields.items()
            ],
        },
    }


def rebase_fixture(prefix: str) -> dict[str, object]:
    return {
        "entries": {
            "value": {
                "items": [
                    json_entry(
                        "SkinCharacterDataProperties",
                        f"Characters/Annie/Skins/{prefix}",
                        {
                            "ChampionSkinName": prefix,
                            "mResourceResolver": f"resolver-{prefix}",
                            "Unrelated": f"keep-{prefix}",
                        },
                    ),
                    json_entry(
                        "ResourceResolver",
                        f"Characters/Annie/Skins/{prefix}/Resources",
                        {"UnrelatedResolverField": prefix},
                    ),
                ]
            }
        }
    }


def write_test_wad(
    path: Path,
    path_hash: int,
    payload: bytes,
    compression_type: int,
    *,
    version_minor: int = 0,
    declared_size: int | None = None,
) -> None:
    if compression_type == 1:
        compressed = gzip.compress(payload)
    elif compression_type == 3:
        if script.zstd is None:
            raise RuntimeError("zstandard is required for this test fixture")
        compressed = script.zstd.ZstdCompressor().compress(payload)
    else:
        compressed = payload
    header_size = 2 + 1 + 1 + 256 + 8 + 4
    entry_size = 8 + 12 + 1 + 1 + 2 + 8
    offset = header_size + entry_size
    data = bytearray()
    data.extend(b"RW")
    data.extend(bytes((3, version_minor)))
    data.extend(bytes(256))
    data.extend(bytes(8))
    data.extend(struct.pack("<I", 1))
    data.extend(struct.pack("<Q", path_hash))
    data.extend(
        struct.pack(
            "<III",
            offset,
            len(compressed),
            len(payload) if declared_size is None else declared_size,
        )
    )
    data.extend(bytes((compression_type,)))
    data.extend(bytes(3))
    data.extend(bytes(8))
    data.extend(compressed)
    path.write_bytes(data)


class LcuSourceIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        script._WAD_INDEX_CACHE.clear()
        script._LCU_JSON_CACHE.clear()
        script._CHAMPION_IDENTITIES_CACHE.clear()
        script._OFFICIAL_NAME_CACHE.clear()
        script._OFFICIAL_SKIN_INDEX_CACHE.clear()

    def make_tree(self, root: Path) -> tuple[Path, Path]:
        champions_dir = (
            root / "Game" / "DATA" / "FINAL" / "Champions"
        )
        champions_dir.mkdir(parents=True)
        lcu_dir = root / "Plugins" / "rcp-be-lol-game-data"
        lcu_dir.mkdir(parents=True)
        return champions_dir, lcu_dir

    def write_lcu_json(
        self,
        wad_path: Path,
        rel_path: str,
        document: object,
    ) -> bytes:
        raw = json.dumps(document).encode()
        write_test_wad(
            wad_path,
            script.lcu_path_hash(rel_path),
            raw,
            0,
            version_minor=4,
        )
        return raw

    def test_lcu_json_records_unique_source_chunk_and_raw_sha(self) -> None:
        rel_path = "plugins/example/value.json"
        with tempfile.TemporaryDirectory() as temp_name:
            champions_dir, lcu_dir = self.make_tree(Path(temp_name))
            raw = self.write_lcu_json(
                lcu_dir / "main.wad",
                rel_path,
                {"value": 1},
            )

            record = script.load_lcu_json_with_identity(
                champions_dir,
                rel_path,
            )

        self.assertEqual(record.data, {"value": 1})
        self.assertEqual(
            record.source.raw_sha256,
            hashlib.sha256(raw).hexdigest(),
        )
        self.assertEqual(
            record.source.path_hash,
            script.lcu_path_hash(rel_path),
        )
        self.assertEqual(record.source.source_wad.path.name, "main.wad")

    def test_lcu_json_rejects_duplicate_wad_sources(self) -> None:
        rel_path = "plugins/example/value.json"
        with tempfile.TemporaryDirectory() as temp_name:
            champions_dir, lcu_dir = self.make_tree(Path(temp_name))
            for name in ("a.wad", "b.wad"):
                self.write_lcu_json(
                    lcu_dir / name,
                    rel_path,
                    {"value": name},
                )

            with self.assertRaisesRegex(
                script.LcuDataError,
                "expected exactly one WAD source; found 2",
            ):
                script.load_lcu_json_with_identity(
                    champions_dir,
                    rel_path,
                )

    def test_lcu_json_cache_hit_checks_generation_without_rereading(self) -> None:
        rel_path = "plugins/example/value.json"
        with tempfile.TemporaryDirectory() as temp_name:
            champions_dir, lcu_dir = self.make_tree(Path(temp_name))
            self.write_lcu_json(
                lcu_dir / "main.wad",
                rel_path,
                {"value": 1},
            )
            generation = script.capture_lcu_wad_generation(
                champions_dir
            )
            cached = script.load_lcu_json_with_identity(
                champions_dir,
                rel_path,
                expected_generation=generation,
            )

            with patch.object(
                script,
                "read_lcu_game_data_record",
                side_effect=AssertionError("cache hit must not reread JSON"),
            ) as reader:
                hit = script.load_lcu_json_with_identity(
                    champions_dir,
                    rel_path,
                    expected_generation=generation,
                )
            reader.assert_not_called()
            self.assertIs(hit, cached)

            with (
                patch.object(
                    script,
                    "capture_lcu_wad_generation",
                    return_value=(),
                ),
                self.assertRaisesRegex(
                    script.LcuDataError,
                    "differs from the pinned",
                ),
            ):
                script.load_lcu_json_with_identity(
                    champions_dir,
                    rel_path,
                    expected_generation=generation,
                )

    def test_lcu_json_cache_misses_when_wad_generation_changes(self) -> None:
        rel_path = "plugins/example/value.json"
        with tempfile.TemporaryDirectory() as temp_name:
            champions_dir, lcu_dir = self.make_tree(Path(temp_name))
            wad_path = lcu_dir / "main.wad"
            self.write_lcu_json(
                wad_path,
                rel_path,
                {"value": 1},
            )
            first = script.load_lcu_json_with_identity(
                champions_dir,
                rel_path,
            )
            self.write_lcu_json(
                wad_path,
                rel_path,
                {"value": 200},
            )
            second = script.load_lcu_json_with_identity(
                champions_dir,
                rel_path,
            )

        self.assertEqual(first.data, {"value": 1})
        self.assertEqual(second.data, {"value": 200})
        self.assertIsNot(first, second)


class OfficialWadIdentityTests(unittest.TestCase):
    def test_official_query_uses_exact_wad_base(self) -> None:
        wukong = ChampionIdentity(
            champion_id=62,
            display_name="Wukong",
            alias="MonkeyKing",
            wad_base="MonkeyKing",
            main_unit="monkeyking",
        )
        with tempfile.TemporaryDirectory() as temp_name:
            champions_dir = Path(temp_name)
            expected = champions_dir / "MonkeyKing.wad.client"
            for name in (
                expected.name,
                "Wukong.wad.client",
                "MonkeyKing.en_US.wad.client",
                "Ruby_MonkeyKing.wad.client",
            ):
                (champions_dir / name).write_bytes(b"fixture")
            with patch.object(
                script,
                "load_champion_identity",
                return_value=wukong,
            ):
                selected_name, selected_path = script.find_champion_wad(
                    "Wukong",
                    champions_dir,
                )

        self.assertEqual(selected_name, "MonkeyKing")
        self.assertEqual(selected_path, expected)

    def test_official_query_never_falls_back_to_display_name_wad(self) -> None:
        wukong = ChampionIdentity(
            champion_id=62,
            display_name="Wukong",
            alias="MonkeyKing",
            wad_base="MonkeyKing",
            main_unit="monkeyking",
        )
        with tempfile.TemporaryDirectory() as temp_name:
            champions_dir = Path(temp_name)
            (champions_dir / "Wukong.wad.client").write_bytes(b"fixture")
            with (
                patch.object(
                    script,
                    "load_champion_identity",
                    return_value=wukong,
                ),
                self.assertRaisesRegex(
                    SystemExit,
                    "MonkeyKing.wad.client; found 0",
                ),
            ):
                script.find_champion_wad("Wukong", champions_dir)

    def test_archive_plan_reuses_identity_bound_wukong_wad(self) -> None:
        identity = ChampionIdentity(
            champion_id=62,
            display_name="Wukong",
            alias="MonkeyKing",
            wad_base="MonkeyKing",
            main_unit="monkeyking",
        )
        skin = script.LocalSkin(
            champion_name="Wukong",
            champion_id=62,
            skin_number=1,
            display_name="Volcanic Wukong",
            base_display_name="Volcanic Wukong",
            internal_name="MonkeyKingVolcanic",
            skinline="volcanic",
            parent_skin_number=None,
            is_chroma=False,
            aliases=(),
        )
        with tempfile.TemporaryDirectory() as temp_name:
            champions_dir = Path(temp_name)
            expected = champions_dir / "MonkeyKing.wad.client"
            expected.write_bytes(b"fixture")
            runtime = Mock(identity=identity, source_wad=expected)
            session_pool = Mock()
            session_pool.session_for_id.return_value = runtime
            with patch.object(
                script,
                "find_source_wad",
                side_effect=AssertionError("display-name lookup used"),
            ):
                actual = script.resolve_archive_source_wad(
                    skin,
                    champions_dir,
                    session_pool,
                )

        self.assertEqual(actual, expected)
        session_pool.session_for_id.assert_called_once_with(62)

    def test_archive_plan_uses_identity_bound_wad_name(self) -> None:
        skin = script.LocalSkin(
            champion_name="Wukong",
            champion_id=62,
            skin_number=1,
            display_name="Volcanic Wukong",
            base_display_name="Volcanic Wukong",
            internal_name="MonkeyKingVolcanic",
            skinline="volcanic",
            parent_skin_number=None,
            is_chroma=False,
            aliases=(),
        )
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source_wad = root / "MonkeyKing.wad.client"
            source_wad.write_bytes(b"fixture")
            plan = script.create_archive_plan(
                skin,
                source_wad,
                skin.display_name,
                "1.0.0",
                ("zip",),
                input_root=root / "input",
                output_root=root / "output",
            )

        self.assertEqual(plan.wad_name, "MonkeyKing.wad.client")


class ExactSkinSelectionTests(unittest.TestCase):
    def test_names_and_ids_select_only_exact_entries(self) -> None:
        base = script.LocalSkin(
            champion_name="Yasuo",
            champion_id=157,
            skin_number=9,
            display_name="Nightbringer Yasuo",
            base_display_name="Nightbringer Yasuo",
            internal_name="YasuoSkin09",
            skinline="nightbringer",
            parent_skin_number=None,
            is_chroma=False,
            aliases=(),
        )
        base = replace(base, aliases=script.make_skin_aliases(base))
        chroma = script.LocalSkin(
            champion_name="Yasuo",
            champion_id=157,
            skin_number=34,
            display_name="Nightbringer Yasuo (Pariah)",
            base_display_name="Nightbringer Yasuo",
            internal_name="YasuoSkin34",
            skinline="nightbringer",
            parent_skin_number=9,
            is_chroma=True,
            aliases=(),
        )
        chroma = replace(chroma, aliases=script.make_skin_aliases(chroma))
        catalog = script.LocalCatalog(
            champion_name="Yasuo",
            wad_path=Path("Yasuo.wad.client"),
            identity=Mock(),
            main_unit="yasuo",
            skins=(base, chroma),
        )
        official_index = {
            script.normalize_display_name(base.display_name): script.OfficialSkinRef(
                157,
                9,
                base.display_name,
            ),
        }
        official_catalog = script.OfficialNameCatalog(
            champion_id=157,
            names_by_skin_number={
                9: base.display_name,
                34: chroma.display_name,
            },
        )

        with (
            patch.object(
                script,
                "load_official_skin_index",
                return_value=official_index,
            ),
            patch.object(
                script,
                "load_official_name_catalog",
                return_value=official_catalog,
            ),
            patch.object(
                script,
                "find_official_champion_wad",
                return_value=("Yasuo", catalog.wad_path),
            ),
            patch.object(
                script,
                "infer_champion_from_skin_name",
                return_value=("Yasuo", catalog.wad_path),
            ),
            patch.object(
                script,
                "candidate_official_champion_ids_for_skin_name",
                return_value=[],
            ),
            patch.object(script, "get_runtime_catalog", return_value=catalog),
        ):
            selected_base = script.resolve_local_skin_name(
                base.display_name,
                Path("Champions"),
            )
            selected_chroma = script.resolve_local_skin_name(
                chroma.display_name,
                Path("Champions"),
            )
            selected_base_id = script.resolve_local_skin_name(
                "skin9 Yasuo",
                Path("Champions"),
            )
            selected_chroma_id = script.resolve_local_skin_name(
                "skin34 Yasuo",
                Path("Champions"),
            )
            selected_base_full_id = script.resolve_local_skin_name(
                "157009",
                Path("Champions"),
            )
            selected_chroma_full_id = script.resolve_local_skin_name(
                "157034",
                Path("Champions"),
            )
            with self.assertRaisesRegex(SystemExit, "canonical positive decimal"):
                script.resolve_local_skin_name("0157009", Path("Champions"))
            with self.assertRaisesRegex(
                SystemExit,
                "official full skin ID not found",
            ):
                script.resolve_local_skin_name("157099", Path("Champions"))

        self.assertEqual([skin.skin_number for skin in selected_base], [9])
        self.assertEqual([skin.skin_number for skin in selected_chroma], [34])
        self.assertEqual([skin.skin_number for skin in selected_base_id], [9])
        self.assertEqual([skin.skin_number for skin in selected_chroma_id], [34])
        self.assertEqual([skin.skin_number for skin in selected_base_full_id], [9])
        self.assertEqual([skin.skin_number for skin in selected_chroma_full_id], [34])


class StableWadSnapshotTests(unittest.TestCase):
    def test_extractor_copy_is_bound_to_prepared_identity_and_toc(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "Annie.wad.client"
            work = root / "work"
            work.mkdir()
            write_test_wad(
                source,
                script.wad_path_hash(
                    "data/characters/annie/skins/skin0.bin"
                ),
                b"PROPfixture",
                0,
                version_minor=4,
            )
            prepared = script.PreparedChampionWad(source)

            def fake_extract(
                _command: list[str],
                **kwargs: object,
            ) -> Mock:
                cwd = Path(str(kwargs["cwd"]))
                (cwd / "Annie.wad").mkdir()
                return Mock()

            with patch.object(
                script,
                "run_external_process",
                side_effect=fake_extract,
            ):
                extracted = script.extract_wad_to_temp_dir(
                    source,
                    work,
                    expected_wad_identity=prepared.file_identity,
                    expected_toc_digest=prepared.toc_digest,
                )

            copied = script.parse_wad_index_core(
                work / "Annie.wad.client"
            )

        self.assertEqual(extracted.name, "Annie.wad")
        self.assertEqual(copied.toc_digest, prepared.toc_digest)

    def test_extractor_copy_rejects_a_different_expected_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "Annie.wad.client"
            other = root / "Other.wad.client"
            work = root / "work"
            work.mkdir()
            for path, payload in (
                (source, b"PROPsource"),
                (other, b"PROPother"),
            ):
                write_test_wad(
                    path,
                    script.wad_path_hash(
                        "data/characters/annie/skins/skin0.bin"
                    ),
                    payload,
                    0,
                    version_minor=4,
                )
            other_identity = script.capture_wad_file_identity(other)

            with self.assertRaises(script.WadChangedDuringRead):
                script.extract_wad_to_temp_dir(
                    source,
                    work,
                    expected_wad_identity=other_identity,
                )


class QuietTestCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        log_patcher = patch.object(script, "log")
        log_patcher.start()
        self.addCleanup(log_patcher.stop)


class SkinIdentityTests(QuietTestCase):
    def test_full_skin_ids_include_champion_and_skin_numbers(self) -> None:
        base = make_skin()
        chroma = make_skin(chroma=True)

        self.assertEqual(base.full_skin_id, 1001)
        self.assertIsNone(base.parent_full_skin_id)
        self.assertEqual(chroma.full_skin_id, 1002)
        self.assertEqual(chroma.parent_full_skin_id, 1001)

    def test_official_name_catalog_includes_quest_skin_tiers(self) -> None:
        catalog = script.parse_official_name_catalog(
            145,
            {
                "id": 145,
                "skins": [
                    {
                        "id": 145070,
                        "name": "Risen Legend Kai'Sa",
                        "questSkinInfo": {
                            "tiers": [
                                {"id": 145070, "name": "Risen Legend Kai'Sa"},
                                {
                                    "id": 145071,
                                    "name": "Immortalized Legend Kai'Sa",
                                },
                            ]
                        },
                    }
                ]
            },
        )

        self.assertIsNotNone(catalog)
        assert catalog is not None
        self.assertEqual(
            catalog.names_by_skin_number[71],
            "Immortalized Legend Kai'Sa",
        )

    def test_official_name_catalog_rejects_invalid_ids(self) -> None:
        invalid_documents = (
            {
                "id": True,
                "skins": [],
            },
            {
                "id": 1,
                "skins": [{"id": True, "name": "Bad"}],
            },
            {
                "id": 1,
                "skins": [{"id": -1, "name": "Bad"}],
            },
            {
                "id": 1,
                "skins": [{"id": 2001, "name": "Wrong champion"}],
            },
            {
                "id": 1,
                "skins": [
                    {
                        "id": 1001,
                        "name": "Valid",
                        "chromas": [
                            {"id": 2002, "name": "Wrong champion"},
                        ],
                    }
                ],
            },
        )
        for document in invalid_documents:
            with (
                self.subTest(document=document),
                self.assertRaisesRegex(
                    script.ChampionIdentityError,
                    "invalid skin id|does not match",
                ),
            ):
                script.parse_official_name_catalog(1, document)

    def test_official_name_catalog_rejects_non_quest_duplicates(self) -> None:
        documents = (
            {
                "id": 1,
                "skins": [
                    {"id": 1001, "name": "One"},
                    {"id": 1001, "name": "One"},
                ],
            },
            {
                "id": 1,
                "skins": [
                    {
                        "id": 1001,
                        "name": "One",
                        "chromas": [{"id": 1001, "name": "One"}],
                    }
                ],
            },
        )
        for document in documents:
            with (
                self.subTest(document=document),
                self.assertRaisesRegex(
                    script.ChampionIdentityError,
                    "duplicate official skin number",
                ),
            ):
                script.parse_official_name_catalog(1, document)


class TimingRecorderTests(QuietTestCase):
    def test_measure_records_exact_duration(self) -> None:
        ticks = iter([100, 350])
        recorder = script.TimingRecorder(clock=lambda: next(ticks))

        with recorder.measure("wad.extract"):
            pass

        self.assertEqual(len(recorder.samples), 1)
        sample = recorder.samples[0]
        self.assertEqual(sample.phase, "wad.extract")
        self.assertEqual(sample.elapsed_ns, 250)
        self.assertIsNone(sample.error)

    def test_measure_records_failure_and_reraises(self) -> None:
        ticks = iter([10, 25])
        recorder = script.TimingRecorder(clock=lambda: next(ticks))

        with self.assertRaises(RuntimeError):
            with recorder.measure("failing.phase"):
                raise RuntimeError("boom")

        self.assertEqual(recorder.samples[0].elapsed_ns, 15)
        self.assertEqual(recorder.samples[0].error, "RuntimeError")

    def test_context_timing_aggregates_repeated_phases(self) -> None:
        ticks = iter([0, 10, 20, 50])
        recorder = script.TimingRecorder(clock=lambda: next(ticks))
        with script.use_timings(recorder):
            with script.timed_phase("phase"):
                pass
            with script.timed_phase("phase"):
                pass

        summary = recorder.summary()["phase"]
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["total_ms"], 40 / 1_000_000)
        self.assertEqual(summary["max_ms"], 30 / 1_000_000)


class ArchivePreflightTests(QuietTestCase):
    def test_base_and_chroma_paths_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            base = make_plan(root, "zip")
            chroma = make_plan(root, "zip", chroma=True)

            self.assertEqual(
                base.path_for("zip"),
                root / "output" / "Annie" / "Goth Annie" / "Goth Annie.zip",
            )
            self.assertEqual(
                chroma.path_for("zip"),
                root
                / "output"
                / "Annie"
                / "Goth Annie"
                / "Goth Annie (Ruby)"
                / "Goth Annie (Ruby).zip",
            )

    def test_current_archive_is_cache_hit_and_force_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            plan = make_plan(root, "zip")
            write_plan_archive(root, plan, "zip")

            cached = script.preflight_archive_plans([plan], force=False)
            forced = script.preflight_archive_plans([plan], force=True)

            self.assertEqual(cached.cache_hits, [plan])
            self.assertEqual(cached.pending, [])
            self.assertEqual(forced.pending, [plan])

    def test_both_materializes_missing_sibling_without_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            plan = make_plan(root, "both")
            write_plan_archive(root, plan, "zip")

            result = script.preflight_archive_plans([plan], force=False)

            self.assertEqual(result.materialized, [plan])
            self.assertEqual(result.pending, [])
            self.assertTrue(plan.path_for("fantome").is_file())
            self.assertEqual(
                plan.path_for("zip").read_bytes(),
                plan.path_for("fantome").read_bytes(),
            )

    def test_source_wad_change_invalidates_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            original = make_plan(root, "zip")
            write_plan_archive(root, original, "zip")

            source_stat = original.source_wad.stat()
            os.utime(
                original.source_wad,
                ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns + 1_000_000),
            )
            changed = make_plan(root, "zip")
            result = script.preflight_archive_plans([changed], force=False)

            self.assertEqual(result.pending, [changed])
            self.assertEqual(result.cache_hits, [])

    def test_archive_formats_and_force_argument(self) -> None:
        self.assertEqual(script.archive_extensions("zip"), ("zip",))
        self.assertEqual(script.archive_extensions("fantome"), ("fantome",))
        self.assertEqual(script.archive_extensions("both"), ("zip", "fantome"))
        with self.assertRaises(ValueError):
            script.archive_extensions("rar")

        args = script.parse_args(["--format", "both", "--force"])
        self.assertEqual(args.archive_format, "both")
        self.assertTrue(args.force)
        self.assertEqual(args.wad_mode, "direct")
        self.assertEqual(args.hash_update, "never")
        self.assertEqual(
            script.parse_args(["--wad-mode", "legacy"]).wad_mode,
            "legacy",
        )
        self.assertEqual(
            script.parse_args(["--hash-update", "auto"]).hash_update,
            "auto",
        )
        self.assertEqual(
            script.parse_args(["--hash-update", "force"]).hash_update,
            "force",
        )


class RebaseTests(QuietTestCase):
    def test_modify_json_replaces_only_expected_identity_fields(self) -> None:
        base = rebase_fixture("Skin0")
        target = rebase_fixture("Skin1")
        original_base = copy.deepcopy(base)

        snapshot = script.extract_base_rebase_snapshot(base)
        result = script.apply_base_rebase_snapshot(snapshot, target)
        items = result["entries"]["value"]["items"]
        skin_entry = items[0]
        resolver_entry = items[1]
        fields = {item["key"]: item["value"] for item in skin_entry["value"]["items"]}

        self.assertIs(result, target)
        self.assertEqual(skin_entry["key"], "Characters/Annie/Skins/Skin0")
        self.assertEqual(fields["ChampionSkinName"], "Skin0")
        self.assertEqual(fields["mResourceResolver"], "resolver-Skin0")
        self.assertEqual(fields["Unrelated"], "keep-Skin1")
        self.assertEqual(
            resolver_entry["key"],
            "Characters/Annie/Skins/Skin0/Resources",
        )
        self.assertEqual(base, original_base)


class WadReaderTests(QuietTestCase):
    def setUp(self) -> None:
        super().setUp()
        script._WAD_INDEX_CACHE.clear()

    def test_lcu_hash_is_computed_directly(self) -> None:
        vectors = {
            799: 0x9E8B1E81708A175A,
            800: 0xD05A3499232A7AA1,
            804: 0xC9A279F1A1F27C1B,
            805: 0x2FAF82E4A7CC0C6A,
            893: 0x4BA8C384F980E232,
            904: 0x011464916EB036A1,
        }
        for champion_id, expected in vectors.items():
            path = (
                "plugins/rcp-be-lol-game-data/global/default/v1/"
                f"champions/{champion_id}.json"
            )
            self.assertEqual(script.lcu_path_hash(path), expected)

    def test_lcu_hash_uses_canonical_wad_path_normalization(self) -> None:
        canonical = "data/characters/annie/skins/skin0.bin"
        self.assertEqual(script.lcu_path_hash(canonical), 0x599C1DD4B0FE6EF4)
        self.assertEqual(
            script.lcu_path_hash(r"\DATA\Characters\Annie\Skins\Skin0.bin"),
            0x599C1DD4B0FE6EF4,
        )

    def test_raw_and_gzip_chunks_are_read_by_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            for compression_type in (0, 1):
                with self.subTest(compression_type=compression_type):
                    wad_path = root / f"test-{compression_type}.wad"
                    path_hash = 0x1234567890ABCDEF + compression_type
                    payload = f"payload-{compression_type}".encode()
                    write_test_wad(wad_path, path_hash, payload, compression_type)

                    self.assertEqual(
                        script.read_wad_chunk(wad_path, path_hash),
                        payload,
                    )
                    self.assertIsNone(script.read_wad_chunk(wad_path, path_hash + 100))

    def test_v3_3_and_v3_4_entry_layouts_are_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            for minor in (3, 4):
                with self.subTest(minor=minor):
                    wad_path = root / f"v3-{minor}.wad"
                    path_hash = 0x1000000000000000 + minor
                    write_test_wad(
                        wad_path,
                        path_hash,
                        b"payload",
                        0,
                        version_minor=minor,
                    )

                    chunk = script.parse_wad_index(wad_path)[path_hash]
                    self.assertEqual(chunk.compression_type, 0)
                    self.assertEqual(script.read_wad_chunk(wad_path, path_hash), b"payload")

    @unittest.skipIf(script.zstd is None, "zstandard is not installed")
    def test_zstd_chunks_are_fully_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)

            zstd_path = root / "zstd.wad"
            zstd_hash = 0x2000000000000003
            zstd_payload = b"ordinary-zstd" * 100
            write_test_wad(zstd_path, zstd_hash, zstd_payload, 3, version_minor=4)
            self.assertEqual(script.read_wad_chunk(zstd_path, zstd_hash), zstd_payload)

    def test_redirected_and_subchunked_chunks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            cases = ((2, "Satellite"), (4, "ZstdMulti"))
            for compression_type, message in cases:
                with self.subTest(compression_type=compression_type):
                    wad_path = root / f"unsupported-{compression_type}.wad"
                    path_hash = 0x2000000000000000 + compression_type
                    write_test_wad(
                        wad_path,
                        path_hash,
                        b"payload",
                        compression_type,
                        version_minor=4,
                    )
                    with self.assertRaisesRegex(
                        script.UnsupportedWadFeature,
                        message,
                    ):
                        script.read_wad_chunk(wad_path, path_hash)

    def test_decompressed_size_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "wrong-size.wad"
            path_hash = 0x3000000000000000
            write_test_wad(
                wad_path,
                path_hash,
                b"short",
                0,
                version_minor=4,
                declared_size=100,
            )
            with self.assertRaisesRegex(ValueError, "expected 100"):
                script.read_wad_chunk(wad_path, path_hash)

    def test_index_cache_is_invalidated_when_wad_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "changing.wad"
            first_hash = 0x4000000000000001
            second_hash = 0x4000000000000002
            write_test_wad(wad_path, first_hash, b"first", 0, version_minor=4)
            self.assertIn(first_hash, script.parse_wad_index(wad_path))

            write_test_wad(
                wad_path,
                second_hash,
                b"second-payload-is-a-different-size",
                0,
                version_minor=4,
            )
            changed = script.parse_wad_index(wad_path)
            self.assertIn(second_hash, changed)
            self.assertNotIn(first_hash, changed)

    def test_source_change_after_index_lookup_cannot_misreport_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "changing-after-index.wad"
            old_hash = 0x4000000000000003
            new_hash = 0x4000000000000004
            write_test_wad(wad_path, old_hash, b"old", 0, version_minor=4)
            real_parse = script.parse_wad_index
            replaced = False

            def replace_after_first_parse(path: Path) -> Mapping[int, script.WadChunk]:
                nonlocal replaced
                chunks = real_parse(path)
                if not replaced:
                    replaced = True
                    write_test_wad(
                        wad_path,
                        new_hash,
                        b"new-payload-is-a-different-size",
                        0,
                        version_minor=4,
                    )
                return chunks

            with patch.object(
                script,
                "parse_wad_index",
                side_effect=replace_after_first_parse,
            ):
                self.assertEqual(
                    script.read_wad_chunk(wad_path, new_hash),
                    b"new-payload-is-a-different-size",
                )

    def test_index_cache_hit_reuses_the_core_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "cached.wad"
            write_test_wad(
                wad_path,
                0x4000000000000010,
                b"payload",
                0,
                version_minor=4,
            )

            first = script.parse_wad_index(wad_path)
            second = script.parse_wad_index(wad_path)

        self.assertIs(first, second)

    def test_unsupported_major_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "v2.wad"
            write_test_wad(wad_path, 1, b"payload", 0)
            data = bytearray(wad_path.read_bytes())
            data[2] = 2
            wad_path.write_bytes(data)
            with self.assertRaisesRegex(
                script.UnsupportedWadVersion,
                "supported layouts are 3.0 through 3.4",
            ):
                script.parse_wad_index(wad_path)

    def test_invalid_signature_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "bad.wad"
            wad_path.write_bytes(b"NO")
            with self.assertRaises(ValueError):
                script.parse_wad_index(wad_path)


class BatchedConversionTests(QuietTestCase):
    def test_shared_base_is_coalesced_and_outputs_are_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            plans = [
                make_plan(root, "zip"),
                make_plan(root, "zip", chroma=True),
            ]
            prepared_map: dict[Path, set[str]] = {}
            for plan in plans:
                unit_dir = plan.work_dir / "annie"
                unit_dir.mkdir(parents=True)
                (unit_dir / "skin0.bin").write_bytes(b"PROPbase")
                (
                    unit_dir
                    / f"skin{plan.skin.skin_number}.bin"
                ).write_bytes(b"PROPtarget")
                prepared_map[plan.work_dir] = {"annie"}

            tool = root / "ritobin.exe"
            tool.write_bytes(b"tool")
            cache = script.ProcessBaseParseCache(
                tool,
                rebase_schema=script.REBASE_SCHEMA_VERSION,
                parser_schema=script.BASE_PARSE_PARSER_SCHEMA_VERSION,
            )
            operations = script.OperationRecorder()

            def fake_batches(
                items: list[script.RitobinBatchItem],
                *,
                in_fmt: str,
                out_fmt: str,
            ) -> None:
                for item in items:
                    item.destination.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )
                    if out_fmt == "json":
                        prefix = (
                            "Skin0"
                            if item.destination.stem == "skin0"
                            else item.destination.stem.title()
                        )
                        item.destination.write_text(
                            json.dumps(rebase_fixture(prefix)),
                            encoding="utf-8",
                        )
                    else:
                        item.destination.write_bytes(b"PROPbatched")

            with (
                script.use_operations(operations),
                patch.object(
                    script,
                    "build_base_parse_context",
                    return_value={"unit": "annie"},
                ),
                patch.object(
                    script,
                    "run_ritobin_batches",
                    side_effect=fake_batches,
                ),
                patch.object(
                    cache,
                    "build_key",
                    wraps=cache.build_key,
                ) as build_key,
            ):
                base_work, unit_work = script.prepare_batched_unit_work(
                    plans,
                    prepared_map,
                    {1: Mock()},
                    cache,
                )
                script.convert_batched_unit_work(
                    base_work,
                    unit_work,
                    cache,
                )

            totals = {
                name: sum(
                    value
                    for (record_name, _labels), value
                    in operations.counts.items()
                    if record_name == name
                )
                for name in {
                    "cache.base_parse.misses",
                    "cache.base_parse.hits",
                    "cache.base_parse.coalesced",
                    "cache.base_parse.key_builds",
                    "cache.base_parse.key_coalesced",
                    "cache.base_parse.base_payload_reads",
                    "cache.base_parse.stores",
                    "ritobin.batch.base_files",
                    "ritobin.batch.target_files",
                    "ritobin.batch.output_files",
                }
            }

            self.assertEqual(len(base_work), 1)
            self.assertEqual(len(unit_work), 2)
            self.assertEqual(cache.entry_count, 1)
            self.assertEqual(totals["cache.base_parse.misses"], 1)
            self.assertEqual(totals["cache.base_parse.hits"], 1)
            self.assertEqual(totals["cache.base_parse.coalesced"], 1)
            self.assertEqual(totals["cache.base_parse.key_builds"], 1)
            self.assertEqual(
                totals["cache.base_parse.key_coalesced"],
                1,
            )
            self.assertEqual(
                totals["cache.base_parse.base_payload_reads"],
                1,
            )
            self.assertEqual(totals["cache.base_parse.stores"], 1)
            self.assertEqual(totals["ritobin.batch.base_files"], 1)
            self.assertEqual(totals["ritobin.batch.target_files"], 2)
            self.assertEqual(totals["ritobin.batch.output_files"], 2)
            self.assertEqual(build_key.call_count, 1)
            for item in unit_work:
                self.assertEqual(
                    item.final_bin.read_bytes(),
                    b"PROPbatched",
                )
                modified = json.loads(
                    item.modified_json.read_text(encoding="utf-8")
                )
                entries = modified["entries"]["value"]["items"]
                self.assertEqual(
                    entries[0]["key"],
                    "Characters/Annie/Skins/Skin0",
                )
                self.assertEqual(
                    entries[1]["key"],
                    "Characters/Annie/Skins/Skin0/Resources",
                )


class LegacyCatalogIdentityTests(QuietTestCase):
    def test_catalog_uses_exact_heimerdinger_main_unit(self) -> None:
        heimer = ChampionIdentity(
            champion_id=74,
            display_name="Heimerdinger",
            alias="Heimerdinger",
            wad_base="Heimerdinger",
            main_unit="heimerdinger",
        )
        selected_sources: list[Path] = []

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            wad_path = root / "Heimerdinger.wad.client"
            write_test_wad(
                wad_path,
                script.wad_path_hash(
                    "data/characters/heimerdinger/skins/skin0.bin"
                ),
                b"PROPmain",
                0,
                version_minor=4,
            )

            def fake_extract(
                _wad_path: Path,
                temp_dir: Path,
                **_: object,
            ) -> Path:
                extracted = temp_dir / "Heimerdinger.wad"
                for unit in (
                    "heimertblue",
                    "heimerdinger",
                    "heimertyellow",
                ):
                    skins_dir = extracted / "data" / "characters" / unit / "skins"
                    skins_dir.mkdir(parents=True)
                    (skins_dir / "skin0.bin").write_bytes(b"PROPfixture")
                return extracted

            def stop_after_selection(
                src_dir: Path,
                _dst_dir: Path,
                _in_fmt: str,
                _out_fmt: str,
            ) -> None:
                selected_sources.append(src_dir)
                raise RuntimeError("selection captured")

            with (
                patch.object(
                    script,
                    "capture_lcu_wad_generation",
                    return_value=(),
                ),
                patch.object(
                    script,
                    "load_champion_identity",
                    return_value=heimer,
                ),
                patch.object(
                    script,
                    "extract_wad_to_temp_dir",
                    side_effect=fake_extract,
                ),
                patch.object(
                    script,
                    "run_ritobin_dir_quiet",
                    side_effect=stop_after_selection,
                ),
                self.assertRaisesRegex(RuntimeError, "selection captured"),
            ):
                script.build_local_catalog("Heimerdinger", wad_path)

        self.assertEqual(
            [path.parent.name for path in selected_sources],
            ["heimerdinger"],
        )

    def test_catalog_never_falls_back_when_official_main_unit_is_missing(
        self,
    ) -> None:
        heimer = ChampionIdentity(
            champion_id=74,
            display_name="Heimerdinger",
            alias="Heimerdinger",
            wad_base="Heimerdinger",
            main_unit="heimerdinger",
        )
        ritobin = Mock(side_effect=AssertionError("Ritobin must not run"))

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            wad_path = root / "Heimerdinger.wad.client"
            write_test_wad(
                wad_path,
                script.wad_path_hash(
                    "data/characters/heimerdinger/skins/skin0.bin"
                ),
                b"PROPmain",
                0,
                version_minor=4,
            )

            def fake_extract(
                _wad_path: Path,
                temp_dir: Path,
                **_: object,
            ) -> Path:
                extracted = temp_dir / "Heimerdinger.wad"
                for unit in ("heimertblue", "heimertyellow"):
                    skins_dir = extracted / "data" / "characters" / unit / "skins"
                    skins_dir.mkdir(parents=True)
                    (skins_dir / "skin0.bin").write_bytes(b"PROPfixture")
                return extracted

            with (
                patch.object(
                    script,
                    "capture_lcu_wad_generation",
                    return_value=(),
                ),
                patch.object(
                    script,
                    "load_champion_identity",
                    return_value=heimer,
                ),
                patch.object(
                    script,
                    "extract_wad_to_temp_dir",
                    side_effect=fake_extract,
                ),
                patch.object(script, "run_ritobin_dir_quiet", ritobin),
                self.assertRaisesRegex(
                    SystemExit,
                    "official mainUnit 'heimerdinger'.*found 0",
                ),
            ):
                script.build_local_catalog("Heimerdinger", wad_path)

        ritobin.assert_not_called()


class RunEarlySkipTests(QuietTestCase):
    def test_run_discards_session_and_restarts_once_when_wad_changes(
        self,
    ) -> None:
        changed = script.WadChangedDuringRead(
            Path("Annie.wad.client"),
            None,
            None,
        )
        skin = make_skin()
        args = script.argparse.Namespace(
            archive_format="zip",
            force=False,
            wad_mode="direct",
            champion="Annie",
        )
        execute = Mock()
        with tempfile.TemporaryDirectory() as temp_name:
            champions_dir = Path(temp_name)
            with (
                patch.object(
                    script,
                    "ensure_lol_path",
                    return_value=champions_dir,
                ),
                patch.object(
                    script,
                    "resolve_all_champion_skins",
                    side_effect=(changed, [skin]),
                ) as resolve,
                patch.object(
                    script,
                    "execute_selections",
                    execute,
                ),
            ):
                script.run(args)

        self.assertEqual(resolve.call_count, 2)
        execute.assert_called_once()

    def test_cache_hit_never_calls_prepare_skins(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            input_root = root / "input"
            output_root = root / "output"
            input_root.mkdir()
            source_wad = root / "champions" / "Annie.wad.client"
            source_wad.parent.mkdir()
            source_wad.write_bytes(b"RWsource-wad")
            skin = make_skin()
            ritobin_identity, wad_make_identity = (
                script.capture_archive_tool_identities()
            )
            plan = script.create_archive_plan(
                skin,
                source_wad,
                skin.display_name,
                "",
                ("zip",),
                input_root=input_root,
                output_root=output_root,
                ritobin_identity=ritobin_identity,
                wad_make_identity=wad_make_identity,
            )
            layout_fingerprint = "f" * 64
            script.bind_archive_plan_layout(plan, layout_fingerprint)
            write_plan_archive(root, plan, "zip")
            prepare = Mock(side_effect=AssertionError("prepare_skins must not run"))
            materialize = Mock(
                side_effect=AssertionError("Prepare materialization must not run")
            )
            args = script.argparse.Namespace(archive_format="zip", force=False)
            runtime = Mock(
                identity=ChampionIdentity(
                    champion_id=1,
                    display_name="Annie",
                    alias="Annie",
                    wad_base="Annie",
                    main_unit="annie",
                ),
                source_wad=source_wad,
            )

            def bind_runtime_plans(
                plans: list[script.ArchivePlan],
                _champions_dir: Path,
                *,
                wad_mode: str,
                session_pool: script.ChampionSessionPool | None = None,
            ) -> dict[int, script.ChampionPrepareSession]:
                self.assertEqual(wad_mode, "direct")
                self.assertIsNotNone(session_pool)
                for runtime_plan in plans:
                    script.bind_archive_plan_layout(
                        runtime_plan,
                        layout_fingerprint,
                    )
                return {}

            with (
                patch.object(script, "INPUT_ROOT", input_root),
                patch.object(script, "OUTPUT_ROOT", output_root),
                patch.object(script, "ensure_lol_path", return_value=source_wad.parent),
                patch.object(script, "prompt_skin_names", return_value=[skin]),
                patch.object(
                    script.ChampionSessionPool,
                    "session_for_id",
                    return_value=runtime,
                ),
                patch.object(
                    script,
                    "build_prepare_sessions",
                    side_effect=bind_runtime_plans,
                ),
                patch.object(
                    script,
                    "materialize_pending_plans",
                    materialize,
                ),
                patch.object(script, "prepare_skins", prepare),
            ):
                script.run(args)

            prepare.assert_not_called()
            materialize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
