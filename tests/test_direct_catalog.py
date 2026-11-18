from __future__ import annotations

from collections.abc import Iterable
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rebaser.champion_layout import (
    CandidateRegistry,
    CandidateRegistryEntry,
    ChampionIdentity,
)
from tests.helpers.synthetic_wad import SyntheticChunk, write_synthetic_wad
from tests.helpers.synthetic_prop import prop
from rebaser.skin_bin import SKIN_PROPERTIES
from rebaser.persistent_cache import PersistentJsonCache
from rebaser.wad_access import PreparedChampionWad, wad_path_hash

from rebaser import hash_skin_index
import script


def identity() -> ChampionIdentity:
    return ChampionIdentity(
        champion_id=1,
        display_name="Annie",
        alias="Annie",
        wad_base="Annie",
        main_unit="annie",
    )


def hash_index_for(
    paths: Iterable[str],
) -> hash_skin_index.HashSkinIndex:
    records = tuple(
        sorted(
            hash_skin_index.HashSkinRecord(
                path.split("/")[2],
                int(path.rsplit("/skin", 1)[1].removesuffix(".bin")),
                wad_path_hash(path),
            )
            for path in paths
        )
    )
    return hash_skin_index.HashSkinIndex(
        source_size=1,
        source_modified_ns=1,
        source_row_count=len(records),
        source_sha256="1" * 64,
        relevant_sha256=hash_skin_index._records_sha256(records),
        records=records,
    )


def runtime_session(
    wad_path: Path,
    paths: Iterable[str],
) -> script.ChampionRuntimeSession:
    champion = identity()
    prepared = PreparedChampionWad(wad_path, identity=champion)
    return script.ChampionRuntimeSession(
        identity=champion,
        source_wad=wad_path,
        source_identity=prepared.file_identity,
        toc_digest=prepared.toc_digest,
        lcu_generation=(),
        requested_mode="direct",
        backend="direct",
        prepared=prepared,
        available_path_hashes=frozenset(prepared.chunks_by_hash),
        hash_skin_index=hash_index_for(paths),
    )


def registry() -> CandidateRegistry:
    champion = identity()
    return CandidateRegistry(
        entries={
            champion.champion_id: CandidateRegistryEntry(
                champion_id=champion.champion_id,
                alias=champion.alias,
                wad_base=champion.wad_base,
                main_unit=champion.main_unit,
                auxiliary_units=(),
            )
        }
    )


class DirectCatalogTests(unittest.TestCase):
    def test_successful_converter_exit_with_missing_json_rejects_the_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            skins = root / "skins"
            skins.mkdir()
            for number in (0, 7):
                (skins / f"skin{number}.bin").touch()
            with (
                patch.object(script, "run_ritobin_dir_quiet"),
                patch.object(script, "parse_skin_metadata", side_effect=[{"skin_number": 0}, None]),
                self.assertRaisesRegex(script.ChampionLayoutError, "skin7.bin"),
            ):
                script.collect_catalog_rows(skins, root / "json")

    def test_cached_exact_selection_does_not_hide_missing_installed_skins(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            wad_path = root / "Annie.wad.client"
            paths = [f"data/characters/annie/skins/skin{n}.bin" for n in (0, 1, 7)]
            payload = prop([(SKIN_PROPERTIES, 1, 0, b"")])
            chunks = [SyntheticChunk(wad_path_hash(path), payload, 0) for path in paths]
            write_synthetic_wad(wad_path, sorted(chunks, key=lambda chunk: chunk.path_hash), version_minor=4)
            session = runtime_session(wad_path, paths[:2])
            session.persistent_cache = PersistentJsonCache(root / "cache")
            tool = root / "ritobin.exe"
            tool.touch()
            official = script.OfficialNameCatalog(1, {0: "Annie", 1: "Skin One", 7: "Skin Seven"})

            def read_staged(skins_dir: Path, _json_dir: Path) -> list[dict[str, int]]:
                return [{"skin_number": int(path.stem[4:])} for path in skins_dir.glob("*.bin")]

            with (
                patch.object(script, "RITOBIN_CLI", tool),
                patch.object(script, "capture_lcu_wad_generation", return_value=()),
                patch.object(script, "catalog_lcu_source_documents", return_value=[]),
                patch.object(script, "create_runtime_champion_session", return_value=session),
                patch.object(script, "collect_catalog_rows", side_effect=read_staged),
                patch.object(script, "load_official_name_catalog", return_value=official),
                script.ChampionSessionPool(root, "direct") as pool,
            ):
                scoped = pool.get_catalog("Annie", wad_path, frozenset({1}))
                self.assertEqual([skin.skin_number for skin in scoped.skins], [0, 1])
                self.assertIs(scoped, pool.get_catalog("Annie", wad_path, frozenset({1})))
                for requested in (None, frozenset({7})):
                    with self.subTest(requested=requested), self.assertRaisesRegex(
                        script.ChampionLayoutError, r"\[7\].*--hash-update auto"
                    ):
                        pool.get_catalog("Annie", wad_path, requested)

    def test_official_hierarchy_takes_precedence_over_shared_models(self) -> None:
        official = script.parse_official_name_catalog(1, {"id": 1, "skins": [
            {"id": 1001, "name": "Skin A", "chromas": [{"id": 1002, "name": "Ruby"}]},
            {"id": 1003, "name": "Variant"},
        ]})
        rows = [
            {"skin_number": 1, "skeleton": "shared"},
            {"skin_number": 2, "skeleton": "other"},
            {"skin_number": 3, "skeleton": "shared", "parent": 1},
            {"skin_number": 4, "parent": 1},
        ]
        with patch.object(script, "load_official_name_catalog", return_value=official):
            catalog = script.build_catalog_from_metadata_rows("Annie", Path("Annie.wad.client"), identity(), rows)
        self.assertEqual([s.parent_skin_number for s in catalog.skins], [None, 1, None, 1])
        self.assertEqual(catalog.skins[1].base_display_name, "Skin A")
        self.assertEqual(catalog.skins[2].base_display_name, "Variant")

    def test_fails_closed_without_validated_hash_index(self) -> None:
        path = "data/characters/annie/skins/skin0.bin"
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "Annie.wad.client"
            write_synthetic_wad(
                wad_path,
                [
                    SyntheticChunk(
                        path_hash=wad_path_hash(path),
                        payload=b"PROP-base",
                        compression_type=0,
                    )
                ],
                version_minor=4,
            )
            session = runtime_session(wad_path, (path,))
            session.hash_skin_index = None

            with (
                patch.object(
                    script,
                    "capture_lcu_wad_generation",
                    return_value=(),
                ),
                self.assertRaisesRegex(
                    script.ChampionLayoutError,
                    "no validated HashSkinIndex",
                ),
            ):
                script.build_direct_catalog(session)

    def test_exact_selection_stages_only_requested_main_skin_views(
        self,
    ) -> None:
        base = (SKIN_PROPERTIES, 1, 0, b"")
        target = (SKIN_PROPERTIES, 2, 0, b"")
        opaque = (1234, 3, 0, b"")
        payloads = {
            "data/characters/annie/skins/skin0.bin": prop([base, opaque]),
            "data/characters/annie/skins/skin7.bin": prop([target, opaque]),
            "data/characters/annie/skins/skin8.bin": b"PROP-truncated-unrelated",
            "data/characters/annietibbers/skins/skin0.bin": b"PROP-pet",
        }
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            wad_path = root / "Annie.wad.client"
            chunks = [
                SyntheticChunk(
                    path_hash=wad_path_hash(path),
                    payload=payload,
                    compression_type=0,
                )
                for path, payload in payloads.items()
            ]
            chunks.sort(key=lambda chunk: chunk.path_hash)
            write_synthetic_wad(wad_path, chunks, version_minor=4)
            session = runtime_session(wad_path, payloads)
            observed_staged: dict[str, bytes] = {}

            def collect(
                skins_dir: Path,
                _json_dir: Path,
            ) -> list[dict[str, object]]:
                for path in skins_dir.iterdir():
                    observed_staged[path.name] = path.read_bytes()
                return [
                    {
                        "skin_number": 0,
                        "classification": 0,
                        "internal_name": "Annie",
                        "parent": None,
                        "skinline": "",
                        "skeleton": "annie",
                        "simple_skin": "annie",
                    },
                    {
                        "skin_number": 7,
                        "classification": 0,
                        "internal_name": "AnnieSkin07",
                        "parent": None,
                        "skinline": "",
                        "skeleton": "annie7",
                        "simple_skin": "annie7",
                    },
                ]

            official = script.OfficialNameCatalog(
                champion_id=1,
                names_by_skin_number={
                    0: "Annie",
                    7: "Fixture Annie",
                },
            )
            prepared = session.prepared
            assert prepared is not None
            with (
                patch.object(
                    script,
                    "capture_lcu_wad_generation",
                    return_value=(),
                ),
                patch.object(
                    script,
                    "collect_catalog_rows",
                    side_effect=collect,
                ),
                patch.object(
                    script,
                    "load_official_name_catalog",
                    return_value=official,
                ),
                patch.object(
                    script,
                    "extract_wad_to_temp_dir",
                    side_effect=AssertionError(
                        "Direct Catalog must not extract the WAD"
                    ),
                ),
                patch.object(
                    prepared,
                    "inspect_paths",
                    side_effect=AssertionError(
                        "Direct Catalog must not inspect computed paths"
                    ),
                ),
                patch.object(
                    prepared,
                    "read_many",
                    side_effect=AssertionError(
                        "Direct Catalog must not read computed paths"
                    ),
                ),
                patch.object(
                    prepared,
                    "read_hashes",
                    wraps=prepared.read_hashes,
                ) as read_hashes,
            ):
                catalog = script.build_direct_catalog(session, frozenset({7}))

            self.assertEqual(
                tuple(
                    record.path
                    for record in session.main_skin_records
                ),
                (
                    "data/characters/annie/skins/skin0.bin",
                    "data/characters/annie/skins/skin7.bin",
                ),
            )
            self.assertEqual(
                observed_staged,
                {
                    "skin0.bin": prop([base]),
                    "skin7.bin": prop([target]),
                },
            )
            self.assertEqual(
                [skin.skin_number for skin in catalog.skins],
                [0, 7],
            )
            self.assertEqual(read_hashes.call_count, 1)
            self.assertEqual(
                set(read_hashes.call_args.args[0]),
                {
                    wad_path_hash(
                        "data/characters/annie/skins/skin0.bin"
                    ),
                    wad_path_hash(
                        "data/characters/annie/skins/skin7.bin"
                    ),
                },
            )
            self.assertEqual(prepared.decoded_cache_size, 2)

    def test_prepare_reuses_the_catalog_prepared_and_main_payload_cache(
        self,
    ) -> None:
        payloads = {
            "data/characters/annie/skins/skin0.bin": b"PROP-base",
            "data/characters/annie/skins/skin1.bin": b"PROP-target",
        }
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            wad_path = root / "Annie.wad.client"
            chunks = [
                SyntheticChunk(
                    path_hash=wad_path_hash(path),
                    payload=payload,
                    compression_type=0,
                )
                for path, payload in payloads.items()
            ]
            chunks.sort(key=lambda chunk: chunk.path_hash)
            write_synthetic_wad(wad_path, chunks, version_minor=4)
            session = runtime_session(wad_path, payloads)
            prepared = session.prepared
            assert prepared is not None
            prepared.read_many(payloads, validate_bin=True)

            with patch.object(
                script,
                "PreparedChampionWad",
                side_effect=AssertionError(
                    "Prepare must reuse the Catalog mount"
                ),
            ):
                prepare = script.build_direct_prepare_session(
                    identity(),
                    wad_path,
                    (1,),
                    registry(),
                    prepared=prepared,
                    runtime_session=session,
                )

            self.assertIs(prepare.prepared, prepared)
            self.assertEqual(prepared.decoded_cache_size, 2)

    def test_persistent_catalog_skips_ritobin_in_a_fresh_session(self) -> None:
        payloads = {
            "data/characters/annie/skins/skin0.bin": prop([(SKIN_PROPERTIES, 1, 0, b"")]),
            "data/characters/annie/skins/skin7.bin": prop([(SKIN_PROPERTIES, 2, 0, b"")]),
        }
        rows = [
            {
                "skin_number": 0,
                "classification": 0,
                "internal_name": "Annie",
                "parent": None,
                "skinline": "",
                "skeleton": "annie",
                "simple_skin": "annie",
            },
            {
                "skin_number": 7,
                "classification": 0,
                "internal_name": "AnnieSkin07",
                "parent": None,
                "skinline": "",
                "skeleton": "annie7",
                "simple_skin": "annie7",
            },
        ]
        official = script.OfficialNameCatalog(
            champion_id=1,
            names_by_skin_number={
                0: "Annie",
                7: "Fixture Annie",
            },
        )
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            wad_path = root / "Annie.wad.client"
            chunks = [
                SyntheticChunk(
                    path_hash=wad_path_hash(path),
                    payload=payload,
                    compression_type=0,
                    checksum=1,
                )
                for path, payload in payloads.items()
            ]
            chunks.sort(key=lambda chunk: chunk.path_hash)
            write_synthetic_wad(wad_path, chunks, version_minor=4)
            tool = root / "ritobin.exe"
            tool.write_bytes(b"ritobin")
            first_session = runtime_session(wad_path, payloads)
            first_session.persistent_cache = PersistentJsonCache(
                root / "cache"
            )

            with (
                patch.object(script, "RITOBIN_CLI", tool),
                patch.object(
                    script,
                    "capture_lcu_wad_generation",
                    return_value=(),
                ),
                patch.object(
                    script,
                    "catalog_lcu_source_documents",
                    return_value=[{"rawSha256": "a" * 64}],
                ),
                patch.object(
                    script,
                    "collect_catalog_rows",
                    return_value=rows,
                ) as collect,
                patch.object(
                    script,
                    "load_official_name_catalog",
                    return_value=official,
                ),
            ):
                first = script.build_direct_catalog(first_session)
            self.assertEqual(collect.call_count, 1)
            partial = script.serialize_local_catalog(first)
            partial["skins"].pop()
            with self.assertRaisesRegex(script.ChampionLayoutError, "differs from the main skin paths"):
                script.deserialize_local_catalog(partial, first_session)

            second_session = runtime_session(wad_path, payloads)
            second_session.persistent_cache = PersistentJsonCache(
                root / "cache"
            )
            with (
                patch.object(script, "RITOBIN_CLI", tool),
                patch.object(
                    script,
                    "capture_lcu_wad_generation",
                    return_value=(),
                ),
                patch.object(
                    script,
                    "catalog_lcu_source_documents",
                    return_value=[{"rawSha256": "a" * 64}],
                ),
                patch.object(
                    script,
                    "collect_catalog_rows",
                    side_effect=AssertionError(
                        "persistent Catalog must skip Ritobin parsing"
                    ),
                ) as collect_again,
            ):
                second = script.build_direct_catalog(second_session)

            prepared = second_session.prepared
            assert prepared is not None
            self.assertEqual(first.skins, second.skins)
            self.assertEqual(prepared.decoded_cache_size, 0)
            collect_again.assert_not_called()

            third_session = runtime_session(wad_path, payloads)
            third_session.persistent_cache = PersistentJsonCache(
                root / "cache"
            )
            with (
                patch.object(script, "RITOBIN_CLI", tool),
                patch.object(
                    script,
                    "capture_lcu_wad_generation",
                    return_value=(),
                ),
                patch.object(
                    script,
                    "catalog_lcu_source_documents",
                    return_value=[{"rawSha256": "b" * 64}],
                ),
                patch.object(
                    script,
                    "collect_catalog_rows",
                    return_value=rows,
                ) as changed_collect,
                patch.object(
                    script,
                    "load_official_name_catalog",
                    return_value=official,
                ),
            ):
                script.build_direct_catalog(third_session)
            self.assertEqual(changed_collect.call_count, 1)

    def test_catalog_key_content_hashes_unreliable_zero_checksum_chunks(
        self,
    ) -> None:
        path = "data/characters/annie/skins/skin0.bin"
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            wad_path = root / "Annie.wad.client"
            tool = root / "ritobin.exe"
            tool.write_bytes(b"ritobin")

            def write(payload: bytes) -> None:
                write_synthetic_wad(
                    wad_path,
                    [
                        SyntheticChunk(
                            path_hash=wad_path_hash(path),
                            payload=payload,
                            compression_type=0,
                            checksum=0,
                        )
                    ],
                    version_minor=4,
                )

            write(b"PROP-one")
            first_session = runtime_session(wad_path, (path,))
            first_session.persistent_cache = PersistentJsonCache(
                root / "cache"
            )
            with (
                patch.object(script, "RITOBIN_CLI", tool),
                patch.object(
                    script,
                    "catalog_lcu_source_documents",
                    return_value=[],
                ),
            ):
                first = script.build_direct_catalog_cache_key(
                    first_session,
                    first_session.hash_skin_index.records_for_unit("annie"),
                )
            first_toc = first_session.toc_digest

            write(b"PROP-two")
            second_session = runtime_session(wad_path, (path,))
            second_session.persistent_cache = PersistentJsonCache(
                root / "cache"
            )
            with (
                patch.object(script, "RITOBIN_CLI", tool),
                patch.object(
                    script,
                    "catalog_lcu_source_documents",
                    return_value=[],
                ),
            ):
                second = script.build_direct_catalog_cache_key(
                    second_session,
                    second_session.hash_skin_index.records_for_unit("annie"),
                )

        self.assertEqual(first_toc, second_session.toc_digest)
        self.assertNotEqual(first.digest, second.digest)


if __name__ == "__main__":
    unittest.main()
