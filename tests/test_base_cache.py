from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from rebaser.base_cache import (
    BaseCacheError,
    BaseRebaseSnapshot,
    ProcessBaseParseCache,
)
from rebaser.persistent_cache import PersistentJsonCache


def cache_context(*, unit: str = "annie", modified_ns: int = 10) -> dict:
    return {
        "championId": 1,
        "sourceWad": {
            "path": "Annie.wad.client",
            "device": 1,
            "inode": 2,
            "size": 100,
            "modifiedNs": modified_ns,
        },
        "wadVersion": "3.4",
        "tocDigest": "a" * 64,
        "unit": unit,
        "basePath": f"data/characters/{unit}/skins/skin0.bin",
        "basePathHash": "0123456789abcdef",
        "chunk": {
            "path": f"data/characters/{unit}/skins/skin0.bin",
            "decompressedSize": 7,
            "checksum": "0000000000000001",
        },
    }


class ProcessBaseParseCacheTests(unittest.TestCase):
    def test_key_reuses_same_identity_and_isolates_unit_and_wad(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            tool = Path(temp_name) / "ritobin.exe"
            tool.write_bytes(b"tool")
            cache = ProcessBaseParseCache(
                tool,
                rebase_schema=2,
                parser_schema=1,
            )

            first = cache.build_key(cache_context(), b"PROPbase")
            same = cache.build_key(cache_context(), b"PROPbase")
            other_unit = cache.build_key(
                cache_context(unit="annietibbers"),
                b"PROPbase",
            )
            other_wad = cache.build_key(
                cache_context(modified_ns=11),
                b"PROPbase",
            )

        self.assertEqual(first, same)
        self.assertNotEqual(first.digest, other_unit.digest)
        self.assertNotEqual(first.digest, other_wad.digest)

    def test_tool_change_misses_existing_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            tool = Path(temp_name) / "ritobin.exe"
            tool.write_bytes(b"old-tool")
            cache = ProcessBaseParseCache(
                tool,
                rebase_schema=2,
                parser_schema=1,
            )
            first = cache.build_key(cache_context(), b"PROPbase")
            old_stat = tool.stat()
            tool.write_bytes(b"new-tool-with-different-content")
            os.utime(
                tool,
                ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000),
            )
            changed = cache.build_key(cache_context(), b"PROPbase")

        self.assertNotEqual(first.digest, changed.digest)

    def test_snapshot_is_immutable_and_cache_rejects_conflicting_value(self) -> None:
        snapshot = BaseRebaseSnapshot.from_values(
            skin_entry_key="skin0",
            champion_skin_name="Skin0",
            resource_resolver={"link": ["base"]},
            resolver_entry_key="resolver0",
        )
        mutable = snapshot.values()
        mutable["resourceResolver"]["link"].append("changed")
        self.assertEqual(
            snapshot.values()["resourceResolver"],
            {"link": ["base"]},
        )

        with tempfile.TemporaryDirectory() as temp_name:
            tool = Path(temp_name) / "ritobin.exe"
            tool.write_bytes(b"tool")
            cache = ProcessBaseParseCache(
                tool,
                rebase_schema=2,
                parser_schema=1,
            )
            key = cache.build_key(cache_context(), b"PROPbase")
            cache.put(key, snapshot)
            self.assertIs(cache.get(key), snapshot)
            conflicting = BaseRebaseSnapshot.from_values(
                skin_entry_key="different",
                champion_skin_name="Skin0",
                resource_resolver={"link": ["base"]},
                resolver_entry_key="resolver0",
            )
            with self.assertRaises(BaseCacheError):
                cache.put(key, conflicting)

    def test_persistent_snapshot_is_reused_by_a_fresh_process_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            tool = root / "ritobin.exe"
            tool.write_bytes(b"tool")
            persistent = PersistentJsonCache(root / "cache")
            first = ProcessBaseParseCache(
                tool,
                rebase_schema=2,
                parser_schema=2,
                persistent_cache=persistent,
            )
            key = first.build_key(cache_context(), b"PROPbase")
            snapshot = BaseRebaseSnapshot.from_values(
                skin_entry_key="skin0",
                champion_skin_name="Skin0",
                resource_resolver={"link": ["base"]},
                resolver_entry_key="resolver0",
            )
            first.put(key, snapshot)

            second = ProcessBaseParseCache(
                tool,
                rebase_schema=2,
                parser_schema=2,
                persistent_cache=PersistentJsonCache(root / "cache"),
            )
            second_key = second.build_key(cache_context(), b"PROPbase")
            loaded, tier = second.get_with_tier(second_key)
            facts = second.fact()

        self.assertEqual(tier, "persistent")
        self.assertEqual(loaded, snapshot)
        self.assertEqual(facts["persistent"]["hits"], 1)

    def test_invalid_persistent_snapshot_is_removed_and_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            tool = root / "ritobin.exe"
            tool.write_bytes(b"tool")
            persistent = PersistentJsonCache(root / "cache")
            cache = ProcessBaseParseCache(
                tool,
                rebase_schema=2,
                parser_schema=2,
                persistent_cache=persistent,
            )
            key = cache.build_key(cache_context(), b"PROPbase")
            persistent_key = persistent.key(
                json.loads(key.manifest.decode("utf-8"))
            )
            persistent.store(
                "base-parse",
                persistent_key,
                {"schemaVersion": 1, "values": {"incomplete": True}},
            )

            loaded, tier = cache.get_with_tier(key)
            facts = cache.fact()

        self.assertIsNone(loaded)
        self.assertEqual(tier, "corrupt")
        self.assertEqual(facts["persistent"]["corruptions"], 1)


if __name__ == "__main__":
    unittest.main()
