from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rebaser.champion_layout import (
    CandidateRegistry,
    CandidateRegistryEntry,
    ChampionIdentity,
    build_champion_layout,
    ensure_required_chunk_identities,
)
from rebaser import hash_skin_index
from tests.helpers.synthetic_wad import SyntheticChunk, write_synthetic_wad
from rebaser.persistent_cache import PersistentJsonCache
from rebaser.wad_access import (
    PreparedChampionWad,
    UnsupportedWadFeature,
    UnsupportedWadVersion,
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


def registry_for(*auxiliary_units: str) -> CandidateRegistry:
    identity = annie_identity()
    return CandidateRegistry(
        entries={
            identity.champion_id: CandidateRegistryEntry(
                champion_id=identity.champion_id,
                alias=identity.alias,
                wad_base=identity.wad_base,
                main_unit=identity.main_unit,
                auxiliary_units=tuple(sorted(auxiliary_units)),
            )
        }
    )


def make_skin(skin_number: int) -> script.LocalSkin:
    display_name = f"Annie Skin {skin_number}"
    return script.LocalSkin(
        champion_name="Annie",
        champion_id=1,
        skin_number=skin_number,
        display_name=display_name,
        base_display_name=display_name,
        internal_name=f"AnnieSkin{skin_number}",
        skinline="",
        parent_skin_number=None,
        is_chroma=False,
        aliases=(),
    )


def write_skin_wad(
    wad_path: Path,
    payloads: dict[str, bytes],
    *,
    checksum: int = 0,
    version_minor: int = 4,
) -> None:
    chunks = [
        SyntheticChunk(
            path_hash=wad_path_hash(path),
            payload=payload,
            compression_type=0,
            checksum=checksum,
        )
        for path, payload in payloads.items()
    ]
    chunks.sort(key=lambda chunk: chunk.path_hash)
    write_synthetic_wad(
        wad_path,
        chunks,
        version_minor=version_minor,
    )


def create_plan(root: Path, wad_path: Path, skin_number: int) -> script.ArchivePlan:
    skin = make_skin(skin_number)
    return script.create_archive_plan(
        skin,
        wad_path,
        skin.display_name,
        "1.0.0",
        ("zip",),
        input_root=root / "input",
        output_root=root / "output",
    )


def hash_index_for(paths: list[str]) -> hash_skin_index.HashSkinIndex:
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


class DirectPrepareTests(unittest.TestCase):
    def test_production_session_uses_dictionary_wad_candidate_intersection(
        self,
    ) -> None:
        payloads = {
            "data/characters/annie/skins/skin0.bin": b"PROP-base",
            "data/characters/annie/skins/skin1.bin": b"PROP-target",
            "data/characters/jade_annie/skins/skin0.bin": b"PROP-jade-base",
            "data/characters/jade_annie/skins/skin1.bin": b"PROP-jade-target",
        }
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            wad_path = root / "Annie.wad.client"
            write_skin_wad(wad_path, payloads)
            index = hash_index_for(list(payloads))
            cache = PersistentJsonCache(root / "cache")
            prepared = PreparedChampionWad(
                wad_path,
                identity=annie_identity(),
            )
            runtime = script.ChampionRuntimeSession(
                identity=annie_identity(),
                source_wad=wad_path,
                source_identity=prepared.file_identity,
                toc_digest=prepared.toc_digest,
                lcu_generation=(),
                requested_mode="direct",
                backend="direct",
                prepared=prepared,
                available_path_hashes=frozenset(prepared.chunks_by_hash),
                hash_skin_index=index,
                persistent_cache=cache,
            )
            pool = Mock()
            pool.session_for_id.return_value = runtime
            pool.hash_skin_index = index
            pool.persistent_cache = cache
            plan = create_plan(root, wad_path, 1)

            with (
                patch.object(
                    script,
                    "load_official_champion_identities",
                    return_value=(annie_identity(),),
                ),
                patch.object(
                    script,
                    "load_candidate_registry",
                    side_effect=AssertionError(
                        "dictionary runtime must not load static shadow data"
                    ),
                ) as static_registry,
                patch.object(
                    script,
                    "build_persistent_champion_layout",
                    side_effect=AssertionError(
                        "unbound Layout cache must be bypassed"
                    ),
                ) as persistent_layout,
            ):
                sessions = script.build_prepare_sessions(
                    [plan],
                    root,
                    wad_mode="direct",
                    session_pool=pool,
                )
            static_registry.assert_not_called()
            persistent_layout.assert_not_called()

        self.assertEqual(
            tuple(item.unit for item in sessions[1].skin_layouts[1].paired),
            ("annie", "jade_annie"),
        )
        assert runtime.hash_skin_candidates is not None
        self.assertEqual(
            runtime.hash_skin_candidates.candidates,
            ("annie", "jade_annie"),
        )
        context = script.build_base_parse_context(
            sessions[1],
            "jade_annie",
        )
        self.assertEqual(
            context["basePath"],
            "data/characters/jade_annie/skins/skin0.bin",
        )
        self.assertEqual(
            context["basePathHash"],
            f"{wad_path_hash(context['basePath']):016x}",
        )
        self.assertIsNotNone(context["chunk"])

    def test_one_read_plan_materializes_all_units_without_wad_extract(self) -> None:
        payloads = {
            "data/characters/annie/skins/skin0.bin": b"PROP-annie-base",
            "data/characters/annie/skins/skin1.bin": b"PROP-annie-one",
            "data/characters/annie/skins/skin2.bin": b"PROP-annie-two",
            "data/characters/annietibbers/skins/skin0.bin": b"PROP-tibbers-base",
            "data/characters/annietibbers/skins/skin1.bin": b"PROP-tibbers-one",
            "data/characters/annietibbers/skins/skin2.bin": b"PROP-tibbers-two",
        }
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            wad_path = root / "Annie.wad.client"
            write_skin_wad(wad_path, payloads)
            session = script.build_direct_prepare_session(
                annie_identity(),
                wad_path,
                (1, 2),
                registry_for("annietibbers"),
            )
            plans = [
                create_plan(root, wad_path, skin_number)
                for skin_number in (1, 2)
            ]
            for plan in plans:
                script.bind_archive_plan_layout(
                    plan,
                    session.layout_fingerprints[plan.skin.skin_number],
                )

            prepared = session.prepared
            assert prepared is not None
            self.assertEqual(prepared.decoded_cache_size, 6)
            read_hashes = Mock(wraps=prepared.read_hashes)
            with (
                patch.object(prepared, "read_hashes", read_hashes),
                patch.object(
                    script,
                    "extract_wad_to_temp_dir",
                    side_effect=AssertionError("wad-extract must not run"),
                ) as extract,
                patch.object(
                    script,
                    "prepare_skins",
                    side_effect=AssertionError("legacy Prepare must not run"),
                ) as legacy,
            ):
                materialized = script.materialize_pending_plans(
                    plans,
                    {1: session},
                    root,
                )

            self.assertEqual(read_hashes.call_count, 1)
            requested = tuple(read_hashes.call_args.args[0])
            self.assertEqual(
                set(requested),
                {wad_path_hash(path) for path in payloads},
            )
            self.assertEqual(len(requested), 6)
            self.assertEqual(prepared.decoded_cache_size, 6)
            extract.assert_not_called()
            legacy.assert_not_called()
            self.assertEqual(set(materialized), {plan.work_dir for plan in plans})
            for plan in plans:
                for unit in ("annie", "annietibbers"):
                    base_path = f"data/characters/{unit}/skins/skin0.bin"
                    target_path = (
                        f"data/characters/{unit}/skins/"
                        f"skin{plan.skin.skin_number}.bin"
                    )
                    self.assertEqual(
                        (plan.work_dir / unit / "skin0.bin").read_bytes(),
                        payloads[base_path],
                    )
                    self.assertEqual(
                        (
                            plan.work_dir
                            / unit
                            / f"skin{plan.skin.skin_number}.bin"
                        ).read_bytes(),
                        payloads[target_path],
                    )

    def test_reliable_checksums_defer_payload_reads_until_materialization(
        self,
    ) -> None:
        payloads = {
            "data/characters/annie/skins/skin0.bin": b"PROP-base",
            "data/characters/annie/skins/skin1.bin": b"PROP-target",
        }
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            wad_path = root / "Annie.wad.client"
            write_skin_wad(wad_path, payloads, checksum=1)
            session = script.build_direct_prepare_session(
                annie_identity(),
                wad_path,
                (1,),
                registry_for(),
            )
            prepared = session.prepared
            assert prepared is not None
            self.assertEqual(prepared.decoded_cache_size, 0)

            plan = create_plan(root, wad_path, 1)
            script.materialize_direct_prepare(session, [plan])

            self.assertEqual(prepared.decoded_cache_size, 2)

    def test_fingerprint_changes_only_when_actual_paired_layout_changes(
        self,
    ) -> None:
        payloads = {
            "data/characters/annie/skins/skin0.bin": b"PROP-base",
            "data/characters/annie/skins/skin1.bin": b"PROP-target",
            "data/characters/pairedpet/skins/skin0.bin": b"PROP-pet-base",
            "data/characters/pairedpet/skins/skin1.bin": b"PROP-pet-target",
        }
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            wad_path = root / "Annie.wad.client"
            write_skin_wad(wad_path, payloads)
            prepared = PreparedChampionWad(wad_path, identity=annie_identity())

            def fingerprint(registry: CandidateRegistry) -> str:
                layout = build_champion_layout(
                    annie_identity(),
                    prepared,
                    (1,),
                    registry,
                )
                required = ensure_required_chunk_identities(layout, prepared)
                return script.build_skin_layout_fingerprint(
                    layout,
                    layout.skins[0],
                    required,
                )

            main_only = fingerprint(registry_for())
            absent_candidate = fingerprint(registry_for("absentpet"))
            paired_candidate = fingerprint(registry_for("pairedpet"))

            self.assertEqual(main_only, absent_candidate)
            self.assertNotEqual(main_only, paired_candidate)

            plan = create_plan(root, wad_path, 1)
            old_fingerprint = plan.info["_Rebaser"]["Fingerprint"]
            script.bind_archive_plan_layout(plan, paired_candidate)
            self.assertEqual(
                plan.info["_Rebaser"]["Schema"],
                script.REBASE_SCHEMA_VERSION,
            )
            self.assertEqual(
                plan.info["_Rebaser"]["LayoutFingerprint"],
                paired_candidate,
            )
            self.assertNotEqual(
                plan.info["_Rebaser"]["Fingerprint"],
                old_fingerprint,
            )

    def test_persistent_layout_reuses_each_skin_in_a_fresh_session(
        self,
    ) -> None:
        payloads = {
            "data/characters/annie/skins/skin0.bin": b"PROP-base",
            "data/characters/annie/skins/skin1.bin": b"PROP-one",
            "data/characters/annie/skins/skin2.bin": b"PROP-two",
        }
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            wad_path = root / "Annie.wad.client"
            write_skin_wad(
                wad_path,
                payloads,
                checksum=1,
            )
            first = script.build_direct_prepare_session(
                annie_identity(),
                wad_path,
                (1, 2),
                registry_for(),
                persistent_cache=PersistentJsonCache(root / "cache"),
            )
            with patch.object(
                script,
                "build_champion_layout",
                side_effect=AssertionError(
                    "persistent Layout must skip fresh discovery"
                ),
            ) as build:
                second = script.build_direct_prepare_session(
                    annie_identity(),
                    wad_path,
                    (1, 2),
                    registry_for(),
                    persistent_cache=PersistentJsonCache(root / "cache"),
                )

            self.assertEqual(
                first.layout_fingerprints,
                second.layout_fingerprints,
            )
            self.assertEqual(
                tuple(second.skin_layouts),
                (1, 2),
            )
            build.assert_not_called()

            with patch.object(
                script,
                "build_champion_layout",
                wraps=script.build_champion_layout,
            ) as changed_build:
                script.build_direct_prepare_session(
                    annie_identity(),
                    wad_path,
                    (1, 2),
                    registry_for("absentpet"),
                    persistent_cache=PersistentJsonCache(
                        root / "cache"
                    ),
                )
            self.assertEqual(changed_build.call_count, 1)

    def test_archive_fingerprint_binds_tool_content_without_schema_bump(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            wad_path = root / "Annie.wad.client"
            wad_path.write_bytes(b"RWsource")
            first_tool = script.ToolIdentity(
                path="ritobin.exe",
                size=10,
                modified_ns=1,
                changed_ns=1,
                sha256="a" * 64,
            )
            second_tool = script.ToolIdentity(
                path="ritobin.exe",
                size=10,
                modified_ns=2,
                changed_ns=2,
                sha256="b" * 64,
            )
            wad_make = script.ToolIdentity(
                path="wad-make.exe",
                size=20,
                modified_ns=1,
                changed_ns=1,
                sha256="c" * 64,
            )
            first = script.create_archive_plan(
                make_skin(1),
                wad_path,
                "Annie Skin 1",
                "1.0.0",
                ("zip",),
                ritobin_identity=first_tool,
                wad_make_identity=wad_make,
            )
            second = script.create_archive_plan(
                make_skin(1),
                wad_path,
                "Annie Skin 1",
                "1.0.0",
                ("zip",),
                ritobin_identity=second_tool,
                wad_make_identity=wad_make,
            )
            script.bind_archive_plan_layout(first, "f" * 64)
            script.bind_archive_plan_layout(second, "f" * 64)

        self.assertEqual(
            first.info["_Rebaser"]["Schema"],
            script.REBASE_SCHEMA_VERSION,
        )
        self.assertNotEqual(
            first.info["_Rebaser"]["Fingerprint"],
            second.info["_Rebaser"]["Fingerprint"],
        )

    def test_direct_materialization_propagates_unsupported_feature(
        self,
    ) -> None:
        payloads = {
            "data/characters/annie/skins/skin0.bin": b"PROP-base",
            "data/characters/annie/skins/skin1.bin": b"PROP-target",
        }
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            wad_path = root / "Annie.wad.client"
            write_skin_wad(wad_path, payloads)
            session = script.build_direct_prepare_session(
                annie_identity(),
                wad_path,
                (1,),
                registry_for(),
            )
            plan = create_plan(root, wad_path, 1)
            legacy = Mock()

            with (
                patch.object(
                    script,
                    "materialize_direct_prepare",
                    side_effect=UnsupportedWadFeature(wad_path, "unsupported"),
                ),
                patch.object(
                    script,
                    "materialize_legacy_prepare",
                    legacy,
                ),
                self.assertRaises(UnsupportedWadFeature),
            ):
                script.materialize_pending_plans(
                    [plan],
                    {1: session},
                    root,
                )

            legacy.assert_not_called()

    def test_explicit_legacy_materialization_uses_extractor(
        self,
    ) -> None:
        payloads = {
            "data/characters/annie/skins/skin0.bin": b"PROP-base",
            "data/characters/annie/skins/skin1.bin": b"PROP-target",
        }
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            wad_path = root / "Annie.wad.client"
            write_skin_wad(wad_path, payloads)
            session = script.build_legacy_prepare_session(
                annie_identity(),
                wad_path,
                (1,),
            )
            plan = create_plan(root, wad_path, 1)
            expected = {plan.work_dir: {"annie"}}
            extractor = Mock(return_value=(expected, "Annie"))

            with (
                patch.object(
                    script,
                    "prepare_skins",
                    extractor,
                ),
            ):
                actual = script.materialize_pending_plans(
                    [plan],
                    {1: session},
                    root,
                )

            self.assertEqual(actual, expected)
            extractor.assert_called_once()

    def test_unsupported_reader_fails_closed_without_automatic_fallback(
        self,
    ) -> None:
        payloads = {
            "data/characters/annie/skins/skin0.bin": b"PROP-base",
            "data/characters/annie/skins/skin1.bin": b"PROP-target",
        }
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            wad_path = root / "Annie.wad.client"
            write_skin_wad(wad_path, payloads, version_minor=5)
            plan = create_plan(root, wad_path, 1)

            with (
                patch.object(
                    script,
                    "load_official_champion_identities",
                    return_value=(annie_identity(),),
                ),
                patch.object(
                    script,
                    "load_candidate_registry",
                    return_value=registry_for(),
                ),
                self.assertRaises(UnsupportedWadVersion),
            ):
                script.build_prepare_sessions(
                    [plan],
                    root,
                    wad_mode="direct",
                )


if __name__ == "__main__":
    unittest.main()
