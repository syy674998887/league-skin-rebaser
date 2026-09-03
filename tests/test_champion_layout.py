from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rebaser.champion_layout import (
    CandidateRegistry,
    CandidateRegistryEntry,
    CandidateRegistryError,
    ChampionIdentity,
    ChampionIdentityError,
    ChampionLayoutError,
    build_champion_layout,
    build_hash_skin_champion_layout,
    candidate_registry_from_hash_candidates,
    candidate_registry_document,
    candidate_units_for,
    derive_hash_skin_candidates,
    ensure_required_chunk_identities,
    find_champion_identity,
    load_candidate_registry,
    parse_official_champion_identities,
    select_main_unit_directory,
    serialize_champion_layout,
)
from rebaser import hash_skin_index
from tests.helpers.synthetic_wad import SyntheticChunk, write_synthetic_wad
from rebaser.wad_access import PreparedChampionWad, wad_path_hash


def identity(
    champion_id: int = 1,
    display_name: str = "Annie",
    alias: str = "Annie",
) -> ChampionIdentity:
    return ChampionIdentity(
        champion_id=champion_id,
        display_name=display_name,
        alias=alias,
        wad_base=alias,
        main_unit=alias.casefold(),
    )


def registry_for(
    champion: ChampionIdentity,
    *auxiliary_units: str,
) -> CandidateRegistry:
    return CandidateRegistry(
        entries={
            champion.champion_id: CandidateRegistryEntry(
                champion_id=champion.champion_id,
                alias=champion.alias,
                wad_base=champion.wad_base,
                main_unit=champion.main_unit,
                auxiliary_units=tuple(sorted(auxiliary_units)),
            )
        }
    )


def write_path_wad(path: Path, paths: list[str]) -> None:
    chunks = [
        SyntheticChunk(
            path_hash=wad_path_hash(item),
            payload=f"PROP:{item}".encode(),
            compression_type=0,
        )
        for item in paths
    ]
    chunks.sort(key=lambda chunk: chunk.path_hash)
    write_synthetic_wad(path, chunks, version_minor=4)


def index_for(
    paths: list[str],
    *,
    source_sha256: str = "1" * 64,
) -> hash_skin_index.HashSkinIndex:
    records = tuple(
        sorted(
            hash_skin_index.HashSkinRecord(
                item.split("/")[2],
                int(item.rsplit("/skin", 1)[1].removesuffix(".bin")),
                wad_path_hash(item),
            )
            for item in paths
        )
    )
    return hash_skin_index.HashSkinIndex(
        source_size=1,
        source_modified_ns=1,
        source_row_count=len(records),
        source_sha256=source_sha256,
        relevant_sha256=hash_skin_index._records_sha256(records),
        records=records,
    )


class OfficialChampionIdentityTests(unittest.TestCase):
    def test_prime_roster_excludes_placeholder_and_jade_records(self) -> None:
        summary = [
            {"id": 0, "name": "None", "alias": "None"},
            {"id": 1, "name": "Annie", "alias": "Annie"},
            {"id": 60001, "name": "Annie", "alias": "Jade_Annie"},
        ]
        skins = {
            "0": {"id": 0, "isBase": True},
            "1000": {"id": 1000, "isBase": True},
        }

        identities = parse_official_champion_identities(summary, skins)

        self.assertEqual(
            identities,
            (identity(),),
        )

    def test_roster_count_is_derived_from_inputs(self) -> None:
        summary = [
            {"id": 1, "name": "Annie", "alias": "Annie"},
            {"id": 2, "name": "Olaf", "alias": "Olaf"},
        ]
        skins = {
            "1000": {"id": 1000, "isBase": True},
            "2000": {"id": 2000, "isBase": True},
        }

        identities = parse_official_champion_identities(summary, skins)

        self.assertEqual([item.champion_id for item in identities], [1, 2])

    def test_invalid_present_base_skin_record_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            ChampionIdentityError,
            "invalid base skin record",
        ):
            parse_official_champion_identities(
                [{"id": 1, "name": "Annie", "alias": "Annie"}],
                {"1000": {"id": 1000, "isBase": False}},
            )

    def test_duplicate_summary_id_fails_even_if_one_record_is_not_prime(self) -> None:
        with self.assertRaisesRegex(
            ChampionIdentityError,
            "duplicate champion-summary id 1",
        ):
            parse_official_champion_identities(
                [
                    {"id": 1, "name": "Annie", "alias": "Annie"},
                    {"id": 1, "name": "Mode Annie", "alias": "Mode_Annie"},
                ],
                {"1000": {"id": 1000, "isBase": True}},
            )

    def test_alias_and_display_name_find_the_same_identity(self) -> None:
        wukong = identity(62, "Wukong", "MonkeyKing")
        nunu = identity(20, "Nunu & Willump", "Nunu")
        renata = identity(888, "Renata Glasc", "Renata")
        identities = (nunu, wukong, renata)

        self.assertEqual(find_champion_identity(identities, "Wukong"), wukong)
        self.assertEqual(
            find_champion_identity(identities, "MonkeyKing"),
            wukong,
        )
        self.assertEqual(
            find_champion_identity(identities, "Nunu & Willump"),
            nunu,
        )
        self.assertEqual(find_champion_identity(identities, "Nunu"), nunu)
        self.assertEqual(
            find_champion_identity(identities, "Renata Glasc"),
            renata,
        )
        self.assertEqual(find_champion_identity(identities, "Renata"), renata)

    def test_alias_that_is_not_a_safe_unit_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ChampionIdentityError,
            "must match",
        ):
            parse_official_champion_identities(
                [{"id": 1, "name": "Unsafe", "alias": "../Unsafe"}],
                {"1000": {"id": 1000, "isBase": True}},
            )


class MainUnitSelectionTests(unittest.TestCase):
    def test_heimerdinger_is_selected_instead_of_shorter_auxiliary(self) -> None:
        heimer = identity(74, "Heimerdinger", "Heimerdinger")
        directories = [
            Path("characters") / "heimertblue",
            Path("characters") / "heimerdinger",
            Path("characters") / "heimertyellow",
        ]

        selected = select_main_unit_directory(directories, heimer)

        self.assertEqual(selected.name, "heimerdinger")

    def test_missing_main_unit_does_not_fall_back_to_shortest(self) -> None:
        heimer = identity(74, "Heimerdinger", "Heimerdinger")
        with self.assertRaisesRegex(
            ChampionIdentityError,
            "found 0",
        ):
            select_main_unit_directory(
                [
                    Path("characters") / "heimertblue",
                    Path("characters") / "heimertyellow",
                ],
                heimer,
            )

    def test_casefold_ambiguity_fails_closed(self) -> None:
        heimer = identity(74, "Heimerdinger", "Heimerdinger")
        with self.assertRaisesRegex(
            ChampionIdentityError,
            "found 2",
        ):
            select_main_unit_directory(
                [
                    Path("characters") / "Heimerdinger",
                    Path("characters") / "heimerdinger",
                ],
                heimer,
            )


class CandidateRegistryTests(unittest.TestCase):
    def test_duplicate_json_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "registry.json"
            path.write_text(
                (
                    '{"schemaVersion":1,"champions":{'
                    '"1":{"alias":"Annie","wadBase":"Annie",'
                    '"mainUnit":"annie","auxiliaryUnits":[]},'
                    '"1":{"alias":"Annie","wadBase":"Annie",'
                    '"mainUnit":"annie","auxiliaryUnits":[]}}}'
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                CandidateRegistryError,
                "duplicate JSON key",
            ):
                load_candidate_registry(path)

    def test_registry_must_match_local_official_identity(self) -> None:
        champion = identity(62, "Wukong", "MonkeyKing")
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "registry.json"
            path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "champions": {
                            "62": {
                                "alias": "Wukong",
                                "wadBase": "Wukong",
                                "mainUnit": "wukong",
                                "auxiliaryUnits": [],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                CandidateRegistryError,
                "differs from local LCU",
            ):
                load_candidate_registry(path, (champion,))

    def test_auxiliary_units_must_be_sorted_and_unique(self) -> None:
        champion = identity()
        document = candidate_registry_document(
            (champion,),
            {1: ("zpet", "apet")},
        )
        self.assertEqual(
            document["champions"]["1"]["auxiliaryUnits"],
            ["apet", "zpet"],
        )

        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "registry.json"
            document["champions"]["1"]["auxiliaryUnits"] = ["zpet", "apet"]
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(
                CandidateRegistryError,
                "must be sorted",
            ):
                load_candidate_registry(path, (champion,))

    def test_schema_version_boolean_is_rejected(self) -> None:
        champion = identity()
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            registry_path = root / "registry.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": True,
                        "champions": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                CandidateRegistryError,
                "unsupported schemaVersion True",
            ):
                load_candidate_registry(registry_path, (champion,))


class DictionaryCandidateTests(unittest.TestCase):
    def test_dictionary_layout_and_preflight_use_only_hash_apis(self) -> None:
        champion = identity()
        paths = [
            "data/characters/annie/skins/skin0.bin",
            "data/characters/annie/skins/skin1.bin",
            "data/characters/jade_annie/skins/skin0.bin",
            "data/characters/jade_annie/skins/skin1.bin",
        ]
        source = index_for(paths)

        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "Annie.wad.client"
            write_path_wad(wad_path, paths)
            prepared = PreparedChampionWad(wad_path, identity=champion)
            candidates = derive_hash_skin_candidates(
                champion,
                prepared.chunks_by_hash,
                source,
            )

            with (
                patch.object(
                    prepared,
                    "inspect_paths",
                    side_effect=AssertionError("path inspection must not run"),
                ),
                patch.object(
                    prepared,
                    "read_many",
                    side_effect=AssertionError("path reads must not run"),
                ),
                patch.object(
                    prepared,
                    "inspect_hashes",
                    wraps=prepared.inspect_hashes,
                ) as inspect_hashes,
                patch.object(
                    prepared,
                    "read_hashes",
                    wraps=prepared.read_hashes,
                ) as read_hashes,
            ):
                layout = build_hash_skin_champion_layout(
                    champion,
                    prepared,
                    (1,),
                    candidates,
                )
                required = ensure_required_chunk_identities(
                    layout,
                    prepared,
                )

        self.assertEqual(
            tuple(state.unit for state in layout.skins[0].paired),
            ("annie", "jade_annie"),
        )
        self.assertEqual(set(required), set(paths))
        self.assertEqual(inspect_hashes.call_count, 2)
        self.assertEqual(read_hashes.call_count, 1)

    def test_dictionary_and_wad_intersection_adds_current_units(self) -> None:
        champion = identity()
        paths = [
            "data/characters/annie/skins/skin0.bin",
            "data/characters/annie/skins/skin1.bin",
            "data/characters/annietibbers/skins/skin0.bin",
            "data/characters/annietibbers/skins/skin1.bin",
            "data/characters/jade_annie/skins/skin0.bin",
            "data/characters/jade_annie/skins/skin1.bin",
            "data/characters/unrelated/skins/skin0.bin",
        ]
        source = index_for(paths)
        available = {
            wad_path_hash(path)
            for path in paths
            if "/unrelated/" not in path
        }

        candidates = derive_hash_skin_candidates(
            champion,
            available,
            source,
        )

        self.assertEqual(
            candidates.candidates,
            ("annie", "annietibbers", "jade_annie"),
        )
        self.assertEqual(len(candidates.matched_records), 6)
        registry = candidate_registry_from_hash_candidates(
            champion,
            candidates,
        )
        self.assertEqual(
            candidate_units_for(champion, registry),
            candidates.candidates,
        )

    def test_candidate_digest_ignores_unrelated_dictionary_records(self) -> None:
        champion = identity()
        relevant = [
            "data/characters/annie/skins/skin0.bin",
            "data/characters/annie/skins/skin1.bin",
        ]
        first = index_for(relevant, source_sha256="1" * 64)
        second = index_for(
            relevant + ["data/characters/olaf/skins/skin0.bin"],
            source_sha256="2" * 64,
        )
        available = {wad_path_hash(path) for path in relevant}

        first_candidates = derive_hash_skin_candidates(
            champion,
            available,
            first,
        )
        second_candidates = derive_hash_skin_candidates(
            champion,
            available,
            second,
        )

        self.assertEqual(first_candidates.digest, second_candidates.digest)
        self.assertNotEqual(
            first_candidates.source_sha256,
            second_candidates.source_sha256,
        )

    def test_base_only_and_target_only_units_remain_layout_diagnostics(self) -> None:
        champion = identity()
        paths = [
            "data/characters/annie/skins/skin0.bin",
            "data/characters/annie/skins/skin1.bin",
            "data/characters/basepet/skins/skin0.bin",
            "data/characters/targetpet/skins/skin1.bin",
        ]
        source = index_for(paths)
        available = {wad_path_hash(path) for path in paths}
        candidates = derive_hash_skin_candidates(
            champion,
            available,
            source,
        )

        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "Annie.wad.client"
            write_path_wad(wad_path, paths)
            prepared = PreparedChampionWad(wad_path, identity=champion)
            layout = build_champion_layout(
                champion,
                prepared,
                (1,),
                candidate_registry_from_hash_candidates(
                    champion,
                    candidates,
                ),
            )

        self.assertEqual(
            tuple(item.unit for item in layout.skins[0].paired),
            ("annie",),
        )
        self.assertEqual(
            tuple(item.unit for item in layout.skins[0].base_only),
            ("basepet",),
        )
        self.assertEqual(
            tuple(item.unit for item in layout.skins[0].target_only),
            ("targetpet",),
        )

    def test_main_skin0_must_be_dictionary_proven(self) -> None:
        champion = identity()
        target_path = "data/characters/annie/skins/skin1.bin"
        target_only = index_for([target_path])
        with self.assertRaisesRegex(
            CandidateRegistryError,
            "jointly prove mainUnit",
        ):
            derive_hash_skin_candidates(
                champion,
                (wad_path_hash(target_path),),
                target_only,
            )


class ChampionLayoutTests(unittest.TestCase):
    def test_layout_classifies_all_four_states_without_cross_pairing(self) -> None:
        champion = identity()
        paths = [
            "data/characters/annie/skins/skin0.bin",
            "data/characters/annie/skins/skin1.bin",
            "data/characters/annietibbers/skins/skin0.bin",
            "data/characters/annietibbers/skins/skin1.bin",
            "data/characters/basepet/skins/skin0.bin",
            "data/characters/targetpet/skins/skin1.bin",
        ]
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "Annie.wad.client"
            write_path_wad(wad_path, paths)
            prepared = PreparedChampionWad(wad_path, identity=champion)
            registry = registry_for(
                champion,
                "absentpet",
                "annietibbers",
                "basepet",
                "targetpet",
            )

            layout = build_champion_layout(
                champion,
                prepared,
                (1,),
                registry,
            )

        skin = layout.skins[0]
        self.assertEqual(
            [state.unit for state in skin.paired],
            ["annie", "annietibbers"],
        )
        self.assertEqual(
            [state.unit for state in skin.base_only],
            ["basepet"],
        )
        self.assertEqual(
            [state.unit for state in skin.target_only],
            ["targetpet"],
        )
        self.assertEqual(skin.absent_candidates, ("absentpet",))
        target_only = skin.target_only[0]
        self.assertIsNone(target_only.base_path)
        self.assertIsNotNone(target_only.target_path)

        serialized = serialize_champion_layout(layout)
        self.assertEqual(serialized["mainUnit"], "annie")
        self.assertEqual(
            serialized["skins"][0]["targetOnly"][0]["unit"],
            "targetpet",
        )

    def test_main_unit_must_be_paired_for_every_target_skin(self) -> None:
        champion = identity()
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "Annie.wad.client"
            write_path_wad(
                wad_path,
                ["data/characters/annie/skins/skin0.bin"],
            )
            prepared = PreparedChampionWad(wad_path)
            with self.assertRaisesRegex(
                ChampionLayoutError,
                "mainUnit 'annie' is not paired",
            ):
                build_champion_layout(
                    champion,
                    prepared,
                    (1,),
                    registry_for(champion),
                )

    def test_locke_does_not_need_a_hash_dictionary_entry(self) -> None:
        locke = identity(805, "Locke", "Locke")
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "Locke.wad.client"
            write_path_wad(
                wad_path,
                [
                    "data/characters/locke/skins/skin0.bin",
                    "data/characters/locke/skins/skin1.bin",
                ],
            )
            layout = build_champion_layout(
                locke,
                PreparedChampionWad(wad_path),
                (1,),
                registry_for(locke),
            )

        self.assertEqual(
            [state.unit for state in layout.skins[0].paired],
            ["locke"],
        )

    def test_wukong_uses_monkeyking_wad_and_main_unit(self) -> None:
        wukong = identity(62, "Wukong", "MonkeyKing")
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "MonkeyKing.wad.client"
            write_path_wad(
                wad_path,
                [
                    "data/characters/monkeyking/skins/skin0.bin",
                    "data/characters/monkeyking/skins/skin1.bin",
                ],
            )
            layout = build_champion_layout(
                wukong,
                PreparedChampionWad(wad_path),
                (1,),
                registry_for(wukong),
            )

        self.assertEqual(layout.identity.display_name, "Wukong")
        self.assertEqual(layout.wad_path.name, "MonkeyKing.wad.client")
        self.assertEqual(layout.skins[0].paired[0].unit, "monkeyking")

    def test_same_unit_name_is_scoped_to_each_champion_wad(self) -> None:
        annie = identity()
        olaf = identity(2, "Olaf", "Olaf")
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            annie_wad = root / "Annie.wad.client"
            olaf_wad = root / "Olaf.wad.client"
            write_path_wad(
                annie_wad,
                [
                    "data/characters/annie/skins/skin0.bin",
                    "data/characters/annie/skins/skin1.bin",
                    "data/characters/sharedpet/skins/skin0.bin",
                    "data/characters/sharedpet/skins/skin1.bin",
                ],
            )
            write_path_wad(
                olaf_wad,
                [
                    "data/characters/olaf/skins/skin0.bin",
                    "data/characters/olaf/skins/skin1.bin",
                    "data/characters/sharedpet/skins/skin0.bin",
                ],
            )
            annie_layout = build_champion_layout(
                annie,
                PreparedChampionWad(annie_wad),
                (1,),
                registry_for(annie, "sharedpet"),
            )
            olaf_layout = build_champion_layout(
                olaf,
                PreparedChampionWad(olaf_wad),
                (1,),
                registry_for(olaf, "sharedpet"),
            )

        self.assertEqual(
            [state.unit for state in annie_layout.skins[0].paired],
            ["annie", "sharedpet"],
        )
        self.assertEqual(
            [state.unit for state in olaf_layout.skins[0].paired],
            ["olaf"],
        )
        self.assertEqual(
            [state.unit for state in olaf_layout.skins[0].base_only],
            ["sharedpet"],
        )

    def test_wad_basename_must_match_official_alias(self) -> None:
        champion = identity()
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "Wrong.wad.client"
            write_path_wad(
                wad_path,
                [
                    "data/characters/annie/skins/skin0.bin",
                    "data/characters/annie/skins/skin1.bin",
                ],
            )
            with self.assertRaisesRegex(
                ChampionIdentityError,
                "expects Annie.wad.client",
            ):
                build_champion_layout(
                    champion,
                    PreparedChampionWad(wad_path),
                    (1,),
                    registry_for(champion),
                )

    def test_skin_numbers_must_be_sorted_and_unique(self) -> None:
        champion = identity()
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "Annie.wad.client"
            write_path_wad(
                wad_path,
                [
                    "data/characters/annie/skins/skin0.bin",
                    "data/characters/annie/skins/skin1.bin",
                    "data/characters/annie/skins/skin2.bin",
                ],
            )
            prepared = PreparedChampionWad(wad_path)
            with self.assertRaisesRegex(
                ChampionLayoutError,
                "sorted",
            ):
                build_champion_layout(
                    champion,
                    prepared,
                    (2, 1),
                    registry_for(champion),
                )


if __name__ == "__main__":
    unittest.main()
