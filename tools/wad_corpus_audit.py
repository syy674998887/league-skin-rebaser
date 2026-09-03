"""Produce a stable, read-only inventory of installed League WAD metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from rebaser.wad_access import (  # noqa: E402
    WadFileIdentity,
    capture_wad_file_identity,
    parse_wad_index,
)


DEFAULT_CONFIG = REPO_ROOT / "config.json"
SUPPORTED_SUFFIXES = (".wad", ".wad.client")
SCHEMA_VERSION = 1


def read_stable_json(path: Path) -> tuple[Any, dict[str, object]]:
    resolved = path.resolve(strict=True)
    before = resolved.stat()
    raw = resolved.read_bytes()
    after = resolved.stat()
    if _stat_key(before) != _stat_key(after) or len(raw) != after.st_size:
        raise OSError(f"JSON file changed while reading: {resolved}")
    return json.loads(raw.decode("utf-8")), {
        "path": str(resolved),
        "size": len(raw),
        "modifiedNs": int(after.st_mtime_ns),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def discover_wads(league_root: Path) -> tuple[Path, ...]:
    root = league_root.resolve(strict=True)
    discovered = {
        path.resolve(strict=True)
        for path in root.rglob("*")
        if path.is_file() and path.name.casefold().endswith(SUPPORTED_SUFFIXES)
    }
    return tuple(
        sorted(
            discovered,
            key=lambda path: path.relative_to(root).as_posix().casefold(),
        )
    )


def audit_corpus(league_root: Path) -> dict[str, object]:
    root = league_root.resolve(strict=True)
    metadata_path = root / "Game" / "content-metadata.json"
    metadata, metadata_identity = read_stable_json(metadata_path)
    if not isinstance(metadata, dict):
        raise ValueError("content-metadata.json must be an object")
    game_version = metadata.get("version")
    if not isinstance(game_version, str) or not game_version:
        raise ValueError("content-metadata.json is missing version")
    wad_paths = discover_wads(root)
    if not wad_paths:
        raise ValueError(f"no .wad or .wad.client files found under {root}")

    versions: Counter[str] = Counter()
    compression_types: Counter[int] = Counter()
    total_entries = 0
    total_subchunked = 0
    initial_paths = tuple(path.relative_to(root).as_posix() for path in wad_paths)
    initial_identities: dict[Path, WadFileIdentity] = {}
    files: list[dict[str, object]] = []

    for wad_path in wad_paths:
        index = parse_wad_index(wad_path)
        ending_identity = capture_wad_file_identity(wad_path)
        if ending_identity != index.file_identity:
            raise OSError(f"WAD changed after its index was parsed: {wad_path}")

        per_type = Counter(
            chunk.compression_type for chunk in index.chunks_by_hash.values()
        )
        subchunked = sum(
            chunk.subchunk_count != 0 or chunk.subchunk_index != 0
            for chunk in index.chunks_by_hash.values()
        )
        entries = len(index.chunks_by_hash)
        versions[str(index.version)] += 1
        compression_types.update(per_type)
        total_entries += entries
        total_subchunked += subchunked
        initial_identities[wad_path] = index.file_identity
        files.append(
            {
                "relativePath": wad_path.relative_to(root).as_posix(),
                "size": index.file_identity.size,
                "modifiedNs": index.file_identity.mtime_ns,
                "version": str(index.version),
                "tocDigest": index.toc_digest,
                "entries": entries,
                "compressionTypes": _string_key_counts(per_type),
                "subchunkedEntries": subchunked,
            }
        )

    ending_paths = discover_wads(root)
    ending_relative = tuple(path.relative_to(root).as_posix() for path in ending_paths)
    if ending_relative != initial_paths:
        raise OSError("installed WAD file set changed during corpus audit")
    for wad_path in ending_paths:
        ending_identity = capture_wad_file_identity(wad_path)
        if ending_identity != initial_identities[wad_path]:
            raise OSError(f"WAD changed during corpus audit: {wad_path}")
    _, ending_metadata_identity = read_stable_json(metadata_path)
    if ending_metadata_identity != metadata_identity:
        raise OSError("content-metadata.json changed during corpus audit")

    wad_files = sum(path.name.casefold().endswith(".wad") for path in wad_paths)
    wad_client_files = len(wad_paths) - wad_files
    return {
        "schemaVersion": SCHEMA_VERSION,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "audit": "installed-wad-corpus",
        "leagueRoot": str(root),
        "client": {
            "version": game_version,
            "source": metadata_identity,
        },
        "discovery": {
            "mode": "recursive",
            "suffixes": list(SUPPORTED_SUFFIXES),
            "stableFileSet": True,
        },
        "totals": {
            "wadFiles": len(wad_paths),
            "plainWadFiles": wad_files,
            "wadClientFiles": wad_client_files,
            "entries": total_entries,
            "versions": dict(sorted(versions.items())),
            "compressionTypes": _string_key_counts(compression_types),
            "subchunkedEntries": total_subchunked,
        },
        "files": files,
    }


def write_json_atomically(path: Path, value: object) -> None:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _string_key_counts(values: Counter[int]) -> dict[str, int]:
    return {
        str(key): values[key]
        for key in sorted(values)
    }


def _stat_key(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--league-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_identity: dict[str, object] | None = None
    if args.league_root is not None:
        league_root = args.league_root
    else:
        config, config_identity = read_stable_json(args.config)
        if not isinstance(config, dict):
            raise ValueError("config JSON must be an object")
        configured = config.get("lol_path")
        if not isinstance(configured, str) or not configured:
            raise ValueError("config is missing lol_path")
        league_root = Path(configured)

    result = audit_corpus(league_root)
    if config_identity is not None:
        _, ending_config_identity = read_stable_json(args.config)
        if ending_config_identity != config_identity:
            raise OSError("config JSON changed during corpus audit")
        result["config"] = config_identity
    write_json_atomically(args.output, result)
    totals = result["totals"]
    assert isinstance(totals, dict)
    print(
        f"WAD corpus: files={totals['wadFiles']} entries={totals['entries']} "
        f"versions={totals['versions']} compression={totals['compressionTypes']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
