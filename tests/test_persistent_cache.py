from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rebaser.persistent_cache import (
    PersistentCacheKey,
    PersistentCacheValueError,
    PersistentJsonCache,
)


class PersistentJsonCacheTests(unittest.TestCase):
    def test_round_trip_is_bound_to_namespace_and_full_key_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            cache = PersistentJsonCache(Path(temp_name))
            key = cache.key({"schemaVersion": 1, "championId": 1})
            self.assertTrue(
                cache.store("catalog", key, {"skins": [0, 1, 2]})
            )

            hit = cache.lookup("catalog", key)
            other = cache.lookup(
                "catalog",
                cache.key({"schemaVersion": 1, "championId": 2}),
            )

        self.assertTrue(hit.hit)
        self.assertEqual(hit.payload, {"skins": [0, 1, 2]})
        self.assertEqual(other.status, "miss")

    def test_corrupt_entry_is_deleted_and_becomes_a_rebuildable_miss(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            cache = PersistentJsonCache(Path(temp_name))
            key = cache.key({"key": "value"})
            self.assertTrue(cache.store("base-parse", key, {"snapshot": 1}))
            entry = next(Path(temp_name).rglob("*.json"))
            entry.write_text('{"partial":true}', encoding="utf-8")

            lookup = cache.lookup("base-parse", key)
            self.assertEqual(lookup.status, "corrupt")
            self.assertFalse(entry.exists())
            self.assertTrue(
                cache.store("base-parse", key, {"snapshot": 2})
            )
            self.assertEqual(
                cache.lookup("base-parse", key).payload,
                {"snapshot": 2},
            )

    def test_key_rejects_noncanonical_or_mismatched_bytes(self) -> None:
        with self.assertRaises(PersistentCacheValueError):
            PersistentCacheKey.from_canonical_bytes(
                "0" * 64,
                b'{"b":2, "a":1}',
            )
        with self.assertRaises(PersistentCacheValueError):
            PersistentCacheKey.from_canonical_bytes(
                "0" * 64,
                b'{"a":1,"b":2}',
            )

    def test_prune_removes_oldest_entries_to_honor_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            cache = PersistentJsonCache(
                root,
                max_bytes=1500,
                max_entry_bytes=4096,
            )
            first = cache.key({"entry": 1})
            second = cache.key({"entry": 2})
            self.assertTrue(
                cache.store("layout", first, {"data": "a" * 700})
            )
            self.assertTrue(
                cache.store("layout", second, {"data": "b" * 700})
            )

            entries = list(root.rglob("*.json"))
            total = sum(path.stat().st_size for path in entries)

        self.assertLessEqual(total, 1500)
        self.assertEqual(len(entries), 1)
        self.assertGreaterEqual(cache.fact()["prunedFiles"], 1)


if __name__ == "__main__":
    unittest.main()
