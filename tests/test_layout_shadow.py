from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

from rebaser.champion_layout import ChunkIdentity, LayoutUnit
from rebaser.wad_access import wad_path_hash
from tools.golden_oracle import GoldenContext


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "layout_shadow.py"
SPEC = importlib.util.spec_from_file_location("layout_shadow", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
layout_shadow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(layout_shadow)


def make_chunk(path: str) -> ChunkIdentity:
    return ChunkIdentity(
        path_hash=wad_path_hash(path),
        compressed_size=10,
        decompressed_size=10,
        compression_type=3,
        subchunk_count=0,
        subchunk_index=0,
        duplicated=False,
        checksum=1,
        checksum_kind="xxh64",
    )


def make_pair(unit: str, skin: int) -> LayoutUnit:
    base_path = f"data/characters/{unit}/skins/skin0.bin"
    target_path = f"data/characters/{unit}/skins/skin{skin}.bin"
    return LayoutUnit(
        unit=unit,
        base_path=base_path,
        target_path=target_path,
        base_chunk=make_chunk(base_path),
        target_chunk=make_chunk(target_path),
    )


class FakeLegacyIndex:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads

    def read(
        self,
        relative_path: str,
        expected_hash: int,
        context: GoldenContext,
    ) -> bytes:
        del context
        if wad_path_hash(relative_path) != expected_hash:
            raise AssertionError("wrong expected hash")
        return self.payloads[relative_path]


class LayoutShadowTests(unittest.TestCase):
    def test_fast_pool_is_an_exact_parent_subset(self) -> None:
        root = MODULE_PATH.parents[1]
        fast = json.loads(
            (root / "benchmarks" / "pools" / "upgrade-v2-fast5.json").read_text(
                encoding="utf-8"
            )
        )
        parent = json.loads(
            (root / "benchmarks" / "pools" / "upgrade-v2.json").read_text(
                encoding="utf-8"
            )
        )
        layout_shadow.validate_pool_subset(fast, parent)

        broken = json.loads(json.dumps(fast))
        broken["champions"][0]["skinCount"] -= 1
        with self.assertRaisesRegex(
            layout_shadow.LayoutShadowError,
            "not an exact parent-pool record",
        ):
            layout_shadow.validate_pool_subset(broken, parent)

    def test_source_pair_map_rejects_duplicate_pairs(self) -> None:
        source_pair = {
            "context": {
                "champion": "Annie",
                "skin_number": 1,
                "unit": "annie",
                "stage": "phase1",
            },
            "basePath": "data/characters/annie/skins/skin0.bin",
            "targetPath": "data/characters/annie/skins/skin1.bin",
            "baseSha256": "0" * 64,
            "targetSha256": "1" * 64,
        }
        with self.assertRaisesRegex(
            layout_shadow.LayoutShadowError,
            "duplicate source pair",
        ):
            layout_shadow.source_pair_map(
                {
                    "status": "success",
                    "pairs": [source_pair, dict(source_pair)],
                }
            )

    def test_legacy_pairs_must_be_a_subset_of_direct_layout(self) -> None:
        source_pair = {
            "context": {
                "champion": "Annie",
                "skin_number": 2,
                "unit": "annietibbers",
                "stage": "phase1",
            },
            "basePath": "data/characters/annietibbers/skins/skin0.bin",
            "targetPath": "data/characters/annietibbers/skins/skin2.bin",
            "baseSha256": "0" * 64,
            "targetSha256": "1" * 64,
        }
        anchors = layout_shadow.source_pair_map(
            {"status": "success", "pairs": [source_pair]}
        )

        with self.assertRaisesRegex(
            layout_shadow.LayoutShadowError,
            "omitted 1 legacy pairs",
        ):
            layout_shadow.compare_pairs(
                champion_name="Annie",
                pairs=[],
                direct_by_path={},
                legacy_index=FakeLegacyIndex({}),
                source_pairs=anchors,
            )

    def test_locke_direct_only_pair_uses_hash_oracle(self) -> None:
        pair = make_pair("locke", 1)
        assert pair.base_path is not None
        assert pair.target_path is not None
        payloads = {
            pair.base_path: b"PROP-base",
            pair.target_path: b"PROP-target",
        }

        records, direct_only = layout_shadow.compare_pairs(
            champion_name="Locke",
            pairs=[(1, pair)],
            direct_by_path=payloads,
            legacy_index=FakeLegacyIndex(payloads),
            source_pairs={},
        )

        self.assertEqual(direct_only, 1)
        self.assertEqual(records[0]["classification"], "direct_only")
        self.assertIs(records[0]["oracleVerified"], True)
        self.assertEqual(
            records[0]["basePathHash"],
            f"{wad_path_hash(pair.base_path):016x}",
        )

    def test_phase1_anchor_sha_must_match_current_bytes(self) -> None:
        pair = make_pair("annie", 1)
        assert pair.base_path is not None
        assert pair.target_path is not None
        payloads = {
            pair.base_path: b"PROP-base",
            pair.target_path: b"PROP-target",
        }
        source_pair = {
            "context": {
                "champion": "Annie",
                "skin_number": 1,
                "unit": "annie",
                "stage": "phase1",
            },
            "basePath": pair.base_path,
            "targetPath": pair.target_path,
            "baseSha256": "0" * 64,
            "targetSha256": "1" * 64,
        }

        with self.assertRaisesRegex(
            layout_shadow.LayoutShadowError,
            "source SHA differs",
        ):
            layout_shadow.compare_pairs(
                champion_name="Annie",
                pairs=[(1, pair)],
                direct_by_path=payloads,
                legacy_index=FakeLegacyIndex(payloads),
                source_pairs=layout_shadow.source_pair_map(
                    {"status": "success", "pairs": [source_pair]}
                ),
            )


if __name__ == "__main__":
    unittest.main()
