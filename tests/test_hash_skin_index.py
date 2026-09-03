from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from rebaser import hash_skin_index
from rebaser.wad_access import wad_path_hash


ANNIE0 = "data/characters/annie/skins/skin0.bin"
ANNIE1 = "data/characters/annie/skins/skin1.bin"
TIBBERS0 = "data/characters/annietibbers/skins/skin0.bin"
UNRELATED = "assets/example.bin"


def dictionary_line(path: str, *, path_hash: int | None = None) -> bytes:
    value = wad_path_hash(path) if path_hash is None else path_hash
    return f"{value:016x} {path}\n".encode("ascii")


def source_bytes() -> bytes:
    return b"".join(
        (
            dictionary_line(UNRELATED),
            dictionary_line(ANNIE1),
            dictionary_line(TIBBERS0),
            dictionary_line(ANNIE0),
        )
    )


class HashSkinIndexTests(unittest.TestCase):
    def test_rebuild_then_load_compact_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "hashes.game.txt"
            cache = root / "cache" / "skin-index.json"
            raw = source_bytes()
            source.write_bytes(raw)
            source_sha256 = hashlib.sha256(raw).hexdigest()

            first = hash_skin_index.ensure_hash_skin_index(
                source,
                cache,
                expected_source_sha256=source_sha256,
                expected_source_size=len(raw),
            )

            self.assertEqual(first.action, "rebuilt")
            self.assertEqual(first.index.units, ("annie", "annietibbers"))
            self.assertEqual(len(first.index.records), 3)
            self.assertEqual(
                first.index.record_for("annie", 1),
                hash_skin_index.HashSkinRecord(
                    "annie",
                    1,
                    wad_path_hash(ANNIE1),
                ),
            )
            self.assertEqual(
                first.index.record_for_hash(wad_path_hash(TIBBERS0)).path,
                TIBBERS0,
            )
            self.assertIsNone(first.index.record_for("annie", 999))
            self.assertTrue(cache.is_file())

            second = hash_skin_index.ensure_hash_skin_index(
                source,
                cache,
                expected_source_sha256=source_sha256,
                expected_source_size=len(raw),
            )

            self.assertEqual(second.action, "current")
            self.assertEqual(second.index, first.index)
            self.assertEqual(
                second.fact()["sourceSha256"],
                source_sha256,
            )

    def test_cache_without_expected_digest_is_bound_to_source_stat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "hashes.game.txt"
            cache = root / "skin-index.json"
            first_raw = dictionary_line(ANNIE0)
            source.write_bytes(first_raw)
            first = hash_skin_index.ensure_hash_skin_index(source, cache)
            self.assertEqual(first.action, "rebuilt")

            second_raw = first_raw + dictionary_line(ANNIE1)
            source.write_bytes(second_raw)
            stat_result = source.stat()
            os.utime(
                source,
                ns=(
                    stat_result.st_atime_ns,
                    stat_result.st_mtime_ns + 1_000_000,
                ),
            )
            second = hash_skin_index.ensure_hash_skin_index(source, cache)

            self.assertEqual(second.action, "rebuilt")
            self.assertEqual(len(second.index.records), 2)
            self.assertNotEqual(
                first.index.source_sha256,
                second.index.source_sha256,
            )

    def test_corrupt_cache_is_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "hashes.game.txt"
            cache = root / "skin-index.json"
            source.write_bytes(dictionary_line(ANNIE0))
            hash_skin_index.ensure_hash_skin_index(source, cache)
            cache.write_text('{"schemaVersion":1}', encoding="utf-8")

            result = hash_skin_index.ensure_hash_skin_index(source, cache)

            self.assertEqual(result.action, "rebuilt")
            parsed = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(parsed["recordCount"], 1)

    def test_source_digest_and_relevant_hash_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "hashes.game.txt"
            cache = root / "skin-index.json"
            source.write_bytes(dictionary_line(ANNIE0))

            with self.assertRaisesRegex(
                hash_skin_index.HashSkinIndexError,
                "SHA-256 differs",
            ):
                hash_skin_index.ensure_hash_skin_index(
                    source,
                    cache,
                    expected_source_sha256="0" * 64,
                )
            self.assertFalse(cache.exists())

            source.write_bytes(dictionary_line(ANNIE0, path_hash=1))
            with self.assertRaisesRegex(
                hash_skin_index.HashSkinIndexError,
                "mismatched XXH64",
            ):
                hash_skin_index.ensure_hash_skin_index(source, cache)

    def test_duplicate_paths_and_hash_collisions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "hashes.game.txt"
            cache = root / "skin-index.json"
            source.write_bytes(dictionary_line(ANNIE0) * 2)

            with self.assertRaisesRegex(
                hash_skin_index.HashSkinIndexError,
                "duplicate standard skin path",
            ):
                hash_skin_index.ensure_hash_skin_index(source, cache)

            first = hash_skin_index.HashSkinRecord("annie", 0, 1)
            second = hash_skin_index.HashSkinRecord("tibbers", 0, 1)
            records = (first, second)
            relevant = hash_skin_index._records_sha256(records)
            with self.assertRaisesRegex(ValueError, "collision"):
                hash_skin_index.HashSkinIndex(
                    source_size=1,
                    source_modified_ns=1,
                    source_row_count=1,
                    source_sha256="0" * 64,
                    relevant_sha256=relevant,
                    records=records,
                )

    def test_subset_digest_is_sorted_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "hashes.game.txt"
            source.write_bytes(source_bytes())
            index = hash_skin_index.ensure_hash_skin_index(
                source,
                root / "skin-index.json",
            ).index

            self.assertEqual(
                index.subset_sha256(("annietibbers", "annie", "annie")),
                index.subset_sha256(("annie", "annietibbers")),
            )
            self.assertNotEqual(
                index.subset_sha256(("annie",)),
                index.subset_sha256(("annietibbers",)),
            )

    def test_invalid_lookup_inputs_are_rejected(self) -> None:
        record = hash_skin_index.HashSkinRecord("annie", 0, 1)
        relevant = hash_skin_index._records_sha256((record,))
        index = hash_skin_index.HashSkinIndex(
            source_size=1,
            source_modified_ns=1,
            source_row_count=1,
            source_sha256="0" * 64,
            relevant_sha256=relevant,
            records=(record,),
        )

        for invalid in (-1, 1 << 64, True, "1"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    index.record_for_hash(invalid)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            index.record_for("Annie", 0)
        with self.assertRaises(ValueError):
            index.record_for("annie", 1000)


if __name__ == "__main__":
    unittest.main()
