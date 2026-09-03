from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from helpers.synthetic_wad import SyntheticChunk, write_synthetic_wad
from rebaser.wad_access import UnsupportedWadVersion


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "wad_corpus_audit.py"
SPEC = importlib.util.spec_from_file_location("wad_corpus_audit", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
wad_corpus_audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wad_corpus_audit)


class WadCorpusAuditTests(unittest.TestCase):
    @staticmethod
    def write_metadata(root: Path) -> None:
        metadata = root / "Game" / "content-metadata.json"
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_text(
            json.dumps({"version": "16.15.7996036+fixture"}),
            encoding="utf-8",
        )

    def test_recursive_inventory_counts_versions_types_and_subchunks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name) / "League"
            self.write_metadata(root)
            game_wad = root / "Game" / "data.wad.client"
            plugin_wad = root / "Plugins" / "plugin.wad"
            game_wad.parent.mkdir(parents=True, exist_ok=True)
            plugin_wad.parent.mkdir(parents=True)
            write_synthetic_wad(
                game_wad,
                [
                    SyntheticChunk(1, b"raw", 0),
                    SyntheticChunk(2, b"gzip" * 10, 1),
                ],
                version_minor=3,
            )
            write_synthetic_wad(
                plugin_wad,
                [
                    SyntheticChunk(
                        3,
                        b"zstd" * 10,
                        3,
                        subchunk_count=1,
                        subchunk_index=2,
                    )
                ],
                version_minor=4,
            )
            ignored = root / "Game" / "not-a-wad.bin"
            ignored.parent.mkdir(parents=True, exist_ok=True)
            ignored.write_bytes(b"ignored")

            result = wad_corpus_audit.audit_corpus(root)

        self.assertEqual(
            result["totals"],
            {
                "wadFiles": 2,
                "plainWadFiles": 1,
                "wadClientFiles": 1,
                "entries": 3,
                "versions": {"3.3": 1, "3.4": 1},
                "compressionTypes": {"0": 1, "1": 1, "3": 1},
                "subchunkedEntries": 1,
            },
        )
        self.assertEqual(
            [item["relativePath"] for item in result["files"]],
            ["Game/data.wad.client", "Plugins/plugin.wad"],
        )
        self.assertTrue(result["discovery"]["stableFileSet"])

    def test_future_version_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name) / "League"
            self.write_metadata(root)
            write_synthetic_wad(
                root / "Game" / "future.wad.client",
                [SyntheticChunk(1, b"data", 0)],
                version_minor=5,
            )

            with self.assertRaisesRegex(
                UnsupportedWadVersion,
                "supported layouts are 3.0 through 3.4",
            ):
                wad_corpus_audit.audit_corpus(root)

    def test_cli_uses_config_and_writes_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            league_root = root / "League"
            self.write_metadata(league_root)
            write_synthetic_wad(
                league_root / "Game" / "one.wad.client",
                [SyntheticChunk(1, b"data", 0)],
                version_minor=4,
            )
            config = root / "config.json"
            config.write_text(
                json.dumps({"lol_path": str(league_root)}),
                encoding="utf-8",
            )
            output = root / "result.json"

            return_code = wad_corpus_audit.main(
                [
                    "--config",
                    str(config),
                    "--output",
                    str(output),
                ]
            )
            result = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(return_code, 0)
        self.assertEqual(result["totals"]["wadFiles"], 1)
        self.assertEqual(result["totals"]["entries"], 1)
        self.assertEqual(result["config"]["path"], str(config.resolve()))


if __name__ == "__main__":
    unittest.main()
