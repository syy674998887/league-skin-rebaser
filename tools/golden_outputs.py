"""Verify app-cold-build ZIP outputs and record their rebased BIN semantics."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import xxhash

try:
    import zstandard as zstd
except ImportError:
    zstd = None


REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_RITOBIN = REPO_ROOT / "bin" / "ritobin_cli.exe"
BUNDLED_RITOBIN_SUPPORT_RELATIVE_PATHS = (
    Path("bin") / "hashes" / "hashes.binentries.txt",
    Path("bin") / "hashes" / "hashes.binfields.txt",
    Path("bin") / "hashes" / "hashes.binhashes.txt",
    Path("bin") / "hashes" / "hashes.bintypes.txt",
    Path("bin") / "hashes" / "hashes.game.txt.0",
    Path("bin") / "hashes" / "hashes.game.txt.1",
    Path("bin") / "hashes" / "hashes.lcu.txt",
)
BUNDLED_HASHES_GAME = REPO_ROOT / "cslol-tools" / "hashes.game.txt"
DEFAULT_WORK_ROOT = REPO_ROOT / ".benchmarks"
RESULT_SCHEMA_VERSION = 1
_SKIN_PATH_RE = re.compile(
    r"^data/characters/(?P<unit>[a-z0-9_]+)/skins/skin(?P<skin>\d+)\.bin$",
    re.IGNORECASE,
)
_WINDOWS_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*]')
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class OutputContext:
    champion: str
    skin: int | str
    unit: str
    stage: str

    def describe(self) -> str:
        return (
            f"champion={self.champion}, skin={self.skin}, "
            f"unit={self.unit}, stage={self.stage}"
        )


class OutputGoldenError(ValueError):
    def __init__(self, context: OutputContext, message: str):
        super().__init__(f"{context.describe()}: {message}")
        self.context = context


@dataclass(frozen=True)
class PairExpectation:
    unit: str
    base_path: str
    target_path: str
    base_sha256: str
    target_sha256: str


@dataclass(frozen=True)
class ChampionExpectation:
    champion_id: int
    champion: str
    wad: dict[str, Any]
    units_by_skin: dict[int, dict[str, PairExpectation]]


@dataclass(frozen=True)
class WadEntry:
    path_hash: int
    offset: int
    compressed_size: int
    decompressed_size: int
    compression_type: int
    subchunk_count: int


@dataclass(frozen=True)
class CollectedUnit:
    context: OutputContext
    expectation: PairExpectation
    path_hash: int
    bin_data: bytes


@dataclass(frozen=True)
class CollectedArchive:
    context: OutputContext
    relative_path: str
    display_name: str
    archive_data: bytes
    meta_data: bytes
    info: dict[str, Any]
    wad_data: bytes
    wad_version: str
    path_hashes: tuple[int, ...]
    units: tuple[CollectedUnit, ...]


@dataclass(frozen=True)
class ConversionInput:
    key: str
    context: OutputContext
    bin_data: bytes


def global_context(stage: str) -> OutputContext:
    return OutputContext("<global>", "*", "*", stage)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path, context: OutputContext) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OutputGoldenError(context, f"failed reading JSON {path}: {exc}") from exc


def sha256_file(path: Path, context: OutputContext) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise OutputGoldenError(context, f"failed hashing {path}: {exc}") from exc
    return digest.hexdigest()


def file_identity(path: Path, context: OutputContext) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise OutputGoldenError(context, f"required file is unavailable: {path}: {exc}") from exc
    try:
        label = path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        label = str(path.resolve())
    return {
        "path": label,
        "size": stat.st_size,
        "modifiedNs": stat.st_mtime_ns,
        "sha256": sha256_file(path, context),
    }


def _is_link_or_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def resolve_output_file(
    output_root: Path,
    path: Path,
    context: OutputContext,
) -> Path:
    lexical_root = output_root.absolute()
    lexical_path = path.absolute()
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError:
        raise OutputGoldenError(
            context,
            f"archive is outside champion output root: {path}",
        ) from None

    current = lexical_root
    for part in (None, *relative.parts):
        if part is not None:
            current = current / part
        if _is_link_or_reparse_point(current):
            raise OutputGoldenError(
                context,
                f"archive path contains a symlink or reparse point: {current}",
            )

    try:
        resolved_root = lexical_root.resolve(strict=True)
        resolved_path = lexical_path.resolve(strict=True)
        resolved_path.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise OutputGoldenError(
            context,
            f"archive is unavailable or escapes champion output root: {path}",
        ) from exc
    if not resolved_path.is_file():
        raise OutputGoldenError(context, f"archive is not a regular file: {path}")
    return resolved_path


def discover_output_zips(
    output_root: Path,
    context: OutputContext,
) -> list[Path]:
    root = output_root.absolute()
    if _is_link_or_reparse_point(root):
        raise OutputGoldenError(
            context,
            f"champion output root is a symlink or reparse point: {root}",
        )
    if not root.is_dir():
        raise OutputGoldenError(context, f"champion output root is unavailable: {root}")

    found: list[Path] = []
    try:
        for current_name, directories, files in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            current = Path(current_name)
            if _is_link_or_reparse_point(current):
                raise OutputGoldenError(
                    context,
                    f"output tree contains a symlink or reparse point: {current}",
                )
            for name in directories:
                child = current / name
                if _is_link_or_reparse_point(child):
                    raise OutputGoldenError(
                        context,
                        f"output tree contains a symlink or reparse point: {child}",
                    )
            for name in files:
                child = current / name
                if _is_link_or_reparse_point(child):
                    raise OutputGoldenError(
                        context,
                        f"output tree contains a symlink or reparse point: {child}",
                    )
                if child.suffix.casefold() == ".zip":
                    found.append(resolve_output_file(root, child, context))
    except OutputGoldenError:
        raise
    except OSError as exc:
        raise OutputGoldenError(context, f"failed scanning output: {exc}") from exc
    return sorted(found, key=lambda path: str(path).casefold())


def parse_hash_dictionary(
    lines: list[str],
    wanted_paths: set[str],
    context: OutputContext,
    *,
    require_complete: bool = True,
) -> dict[str, int]:
    """Resolve only wanted paths from the fixed legacy hash dictionary."""
    normalized_wanted = {
        path.replace("\\", "/").lstrip("/").lower()
        for path in wanted_paths
    }
    found: dict[str, int] = {}
    hashes: dict[int, str] = {}
    for line in lines:
        parts = line.rstrip("\r\n").split(maxsplit=1)
        if len(parts) != 2:
            continue
        path = parts[1].replace("\\", "/").lstrip("/").lower()
        if path not in normalized_wanted:
            continue
        try:
            path_hash = int(parts[0], 16)
        except ValueError:
            continue
        previous_hash = found.get(path)
        if previous_hash is not None and previous_hash != path_hash:
            raise OutputGoldenError(
                context,
                f"fixed dictionary maps {path!r} to multiple hashes",
            )
        previous_path = hashes.get(path_hash)
        if previous_path is not None and previous_path != path:
            raise OutputGoldenError(
                context,
                f"fixed dictionary hash collision for {path_hash:016x}",
            )
        found[path] = path_hash
        hashes[path_hash] = path
    missing = sorted(normalized_wanted - set(found))
    if missing and require_complete:
        raise OutputGoldenError(
            context,
            f"fixed dictionary is missing expected source paths: {missing}",
        )
    return found


def load_fixed_path_hashes(
    source: dict[str, Any],
    expectations: dict[int, ChampionExpectation],
) -> tuple[dict[str, int], dict[str, Any]]:
    context = global_context("path-hash-oracle")
    identity = file_identity(BUNDLED_HASHES_GAME, context)
    source_identity = source.get("hashSource")
    if not isinstance(source_identity, dict):
        raise OutputGoldenError(context, "source Golden has no hashSource identity")
    try:
        source_path = Path(str(source_identity["path"])).resolve()
    except KeyError:
        raise OutputGoldenError(context, "source Golden hashSource path is missing") from None
    if source_path != BUNDLED_HASHES_GAME.resolve():
        raise OutputGoldenError(context, "source Golden did not use bundled hashes.game.txt")
    if any(
        source_identity.get(field) != identity[field]
        for field in ("size", "modifiedNs", "sha256")
    ):
        raise OutputGoldenError(
            context,
            "bundled hashes.game.txt differs from the source Golden identity",
        )
    wanted = {
        pair.base_path
        for expectation in expectations.values()
        for units in expectation.units_by_skin.values()
        for pair in units.values()
    }
    try:
        lines = BUNDLED_HASHES_GAME.read_text(
            encoding="utf-8",
            errors="strict",
        ).splitlines()
    except (OSError, UnicodeError) as exc:
        raise OutputGoldenError(context, f"failed reading fixed dictionary: {exc}") from exc
    resolved = parse_hash_dictionary(
        lines,
        wanted,
        context,
        require_complete=False,
    )
    missing_wanted = wanted - set(resolved)
    declared: dict[str, int] = {}
    for champion in source["champions"]:
        if not isinstance(champion, dict) or champion.get("status") != "success":
            continue
        pairs = champion.get("pairs")
        if not isinstance(pairs, list):
            raise OutputGoldenError(context, "successful source champion has no pairs")
        for pair in pairs:
            if not isinstance(pair, dict):
                raise OutputGoldenError(context, "source pair is not an object")
            for path_field, hash_field in (
                ("basePath", "basePathHash"),
                ("targetPath", "targetPathHash"),
            ):
                path = (
                    str(pair.get(path_field, ""))
                    .replace("\\", "/")
                    .lstrip("/")
                    .lower()
                )
                if path not in missing_wanted:
                    continue
                raw_hash = pair.get(hash_field)
                try:
                    path_hash = (
                        raw_hash
                        if isinstance(raw_hash, int)
                        else int(str(raw_hash), 16)
                    )
                except (TypeError, ValueError) as exc:
                    raise OutputGoldenError(
                        context,
                        f"source pair has invalid {hash_field}",
                    ) from exc
                previous = declared.get(path)
                if previous is not None and previous != path_hash:
                    raise OutputGoldenError(
                        context,
                        f"source pairs disagree on the hash for {path!r}",
                    )
                declared[path] = path_hash

    paths_by_hash = {path_hash: path for path, path_hash in resolved.items()}
    computed_paths: list[str] = []
    for path in sorted(missing_wanted):
        declared_hash = declared.get(path)
        if declared_hash is None:
            raise OutputGoldenError(
                context,
                f"source Golden has no declared hash for {path!r}",
            )
        computed_hash = xxhash.xxh64(path.encode("utf-8"), seed=0).intdigest()
        if declared_hash != computed_hash:
            raise OutputGoldenError(
                context,
                f"source hash for {path!r} is {declared_hash:016x}; "
                f"computed {computed_hash:016x}",
            )
        previous_path = paths_by_hash.get(computed_hash)
        if previous_path is not None and previous_path != path:
            raise OutputGoldenError(
                context,
                f"computed hash collision for {computed_hash:016x}: "
                f"{previous_path!r} and {path!r}",
            )
        resolved[path] = computed_hash
        paths_by_hash[computed_hash] = path
        computed_paths.append(path)
    identity = {
        **identity,
        "dictionaryResolvedPathCount": len(resolved) - len(computed_paths),
        "computedDirectPathCount": len(computed_paths),
        "computedDirectPathsSha256": hashlib.sha256(
            "\n".join(computed_paths).encode("utf-8")
        ).hexdigest(),
    }
    return resolved, identity


def parse_wad_index(
    wad_data: bytes,
    context: OutputContext,
) -> tuple[str, dict[int, WadEntry]]:
    header_size = 2 + 1 + 1 + 256 + 8 + 4
    if len(wad_data) < header_size:
        raise OutputGoldenError(context, "embedded WAD is shorter than its header")
    if wad_data[:2] != b"RW":
        raise OutputGoldenError(context, "embedded WAD has an invalid signature")
    major = wad_data[2]
    minor = wad_data[3]
    if major != 3:
        raise OutputGoldenError(context, f"unsupported embedded WAD version {major}.{minor}")
    chunk_count = struct.unpack_from("<I", wad_data, header_size - 4)[0]
    toc_end = header_size + chunk_count * 32
    if toc_end > len(wad_data):
        raise OutputGoldenError(context, "embedded WAD chunk table exceeds file size")

    entries: dict[int, WadEntry] = {}
    for index in range(chunk_count):
        cursor = header_size + index * 32
        path_hash, offset, compressed_size, decompressed_size = struct.unpack_from(
            "<QIII",
            wad_data,
            cursor,
        )
        flags = wad_data[cursor + 20]
        if path_hash in entries:
            raise OutputGoldenError(context, f"duplicate WAD path hash {path_hash:016x}")
        if (
            offset < toc_end
            or offset > len(wad_data)
            or compressed_size > len(wad_data) - offset
        ):
            raise OutputGoldenError(context, f"WAD chunk {path_hash:016x} has invalid bounds")
        entries[path_hash] = WadEntry(
            path_hash=path_hash,
            offset=offset,
            compressed_size=compressed_size,
            decompressed_size=decompressed_size,
            compression_type=flags & 0x0F,
            subchunk_count=flags >> 4,
        )
    return f"{major}.{minor}", entries


def read_wad_entry(
    wad_data: bytes,
    entry: WadEntry,
    context: OutputContext,
) -> bytes:
    if entry.subchunk_count:
        raise OutputGoldenError(context, "subchunked rebased BIN is unsupported")
    raw = wad_data[entry.offset : entry.offset + entry.compressed_size]
    try:
        if entry.compression_type == 0:
            data = raw
        elif entry.compression_type == 1:
            data = gzip.decompress(raw)
        elif entry.compression_type == 3:
            if zstd is None:
                raise OutputGoldenError(context, "zstandard is required for rebased BIN")
            data = zstd.ZstdDecompressor().decompress(
                raw,
                max_output_size=entry.decompressed_size,
                allow_extra_data=False,
            )
        else:
            raise OutputGoldenError(
                context,
                f"unsupported rebased BIN compression type {entry.compression_type}",
            )
    except OutputGoldenError:
        raise
    except Exception as exc:
        raise OutputGoldenError(context, f"failed decompressing rebased BIN: {exc}") from exc
    if len(data) != entry.decompressed_size:
        raise OutputGoldenError(
            context,
            f"rebased BIN size is {len(data)}, expected {entry.decompressed_size}",
        )
    return data


def sanitize_for_windows(name: str) -> str:
    return re.sub(r"\s+", " ", _WINDOWS_FORBIDDEN_RE.sub("", name)).strip()


def _safe_zip_name(name: str) -> bool:
    pure = PurePosixPath(name)
    return (
        bool(name)
        and "\\" not in name
        and not name.startswith("/")
        and not pure.is_absolute()
        and ".." not in pure.parts
    )


def inspect_archive_bytes(
    archive_data: bytes,
    *,
    relative_path: str,
    champion: str,
    skin_number: int,
    display_name: str,
    expected_wad_name: str,
    expected_units: dict[str, PairExpectation],
    expected_path_hashes: dict[str, int],
) -> CollectedArchive:
    archive_context = OutputContext(champion, skin_number, "<archive>", "archive-structure")
    expected_member = f"WAD/{expected_wad_name}"
    try:
        with zipfile.ZipFile(io.BytesIO(archive_data), "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise OutputGoldenError(archive_context, "archive contains duplicate members")
            unsafe = [name for name in names if not _safe_zip_name(name)]
            if unsafe:
                raise OutputGoldenError(
                    archive_context,
                    f"archive contains unsafe members: {unsafe}",
                )
            actual_files = {
                info.filename
                for info in archive.infolist()
                if not info.is_dir()
            }
            required = {"META/info.json", expected_member}
            if actual_files != required:
                raise OutputGoldenError(
                    archive_context,
                    f"archive members differ: expected {sorted(required)}, "
                    f"got {sorted(actual_files)}",
                )
            corrupt = archive.testzip()
            if corrupt is not None:
                raise OutputGoldenError(archive_context, f"archive member failed CRC: {corrupt}")
            meta_data = archive.read("META/info.json")
            wad_data = archive.read(expected_member)
    except OutputGoldenError:
        raise
    except (OSError, KeyError, zipfile.BadZipFile, RuntimeError) as exc:
        raise OutputGoldenError(archive_context, f"invalid ZIP: {exc}") from exc

    try:
        info = json.loads(meta_data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OutputGoldenError(archive_context, f"invalid META/info.json: {exc}") from exc
    if not isinstance(info, dict):
        raise OutputGoldenError(archive_context, "META/info.json must be an object")
    for field in ("Name", "Author", "Version", "Description"):
        if not isinstance(info.get(field), str):
            raise OutputGoldenError(
                archive_context,
                f"META/info.json field {field} is not a string",
            )
    if info["Name"] != display_name:
        raise OutputGoldenError(
            archive_context,
            f"META Name is {info['Name']!r}, expected {display_name!r}",
        )

    wad_context = OutputContext(champion, skin_number, "<wad>", "output-wad-index")
    wad_version, entries = parse_wad_index(wad_data, wad_context)
    expected_hashes: dict[int, PairExpectation] = {}
    for unit, expectation in expected_units.items():
        path_hash = expected_path_hashes.get(expectation.base_path)
        if path_hash is None:
            raise OutputGoldenError(
                OutputContext(champion, skin_number, unit, "path-hash-oracle"),
                f"fixed dictionary has no hash for {expectation.base_path}",
            )
        if path_hash in expected_hashes:
            raise OutputGoldenError(
                OutputContext(champion, skin_number, unit, "source-golden"),
                f"expected output path hash collision at {path_hash:016x}",
            )
        expected_hashes[path_hash] = expectation
    if set(entries) != set(expected_hashes):
        raise OutputGoldenError(
            wad_context,
            "WAD path-hash set differs: "
            f"expected {[f'{item:016x}' for item in sorted(expected_hashes)]}, "
            f"got {[f'{item:016x}' for item in sorted(entries)]}",
        )

    units: list[CollectedUnit] = []
    for path_hash in sorted(expected_hashes):
        expectation = expected_hashes[path_hash]
        unit_context = OutputContext(
            champion,
            skin_number,
            expectation.unit,
            "output-wad-chunk",
        )
        units.append(
            CollectedUnit(
                context=unit_context,
                expectation=expectation,
                path_hash=path_hash,
                bin_data=read_wad_entry(wad_data, entries[path_hash], unit_context),
            )
        )
    return CollectedArchive(
        context=archive_context,
        relative_path=relative_path,
        display_name=display_name,
        archive_data=archive_data,
        meta_data=meta_data,
        info=info,
        wad_data=wad_data,
        wad_version=wad_version,
        path_hashes=tuple(sorted(entries)),
        units=tuple(units),
    )


def _entry_items(payload: dict[str, Any], context: OutputContext) -> list[dict[str, Any]]:
    entries = payload.get("entries")
    value = entries.get("value") if isinstance(entries, dict) else None
    items = value.get("items") if isinstance(value, dict) else None
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise OutputGoldenError(context, "Ritobin JSON has no entries.value.items list")
    return items


def extract_identity_fields(payload: dict[str, Any], context: OutputContext) -> dict[str, Any]:
    items = _entry_items(payload, context)

    def entry(name: str) -> dict[str, Any]:
        matches = [
            item
            for item in items
            if isinstance(item.get("value"), dict)
            and item["value"].get("name") == name
        ]
        if len(matches) != 1:
            raise OutputGoldenError(context, f"expected 1 {name} entry, got {len(matches)}")
        return matches[0]

    skin_entry = entry("SkinCharacterDataProperties")
    resolver_entry = entry("ResourceResolver")
    skin_value = skin_entry.get("value")
    fields = skin_value.get("items") if isinstance(skin_value, dict) else None
    if not isinstance(fields, list):
        raise OutputGoldenError(context, "SkinCharacterDataProperties has no field list")

    def field(name: str) -> Any:
        matches = [
            item
            for item in fields
            if isinstance(item, dict) and item.get("key") == name
        ]
        if len(matches) != 1 or "value" not in matches[0]:
            raise OutputGoldenError(context, f"expected 1 field {name}, got {len(matches)}")
        return matches[0]["value"]

    values = {
        "skinCharacterDataPropertiesEntryKey": skin_entry.get("key"),
        "championSkinName": field("ChampionSkinName"),
        "mResourceResolver": field("mResourceResolver"),
        "resourceResolverEntryKey": resolver_entry.get("key"),
    }
    if values["skinCharacterDataPropertiesEntryKey"] is None:
        raise OutputGoldenError(context, "SkinCharacterDataProperties entry key is missing")
    if not isinstance(values["championSkinName"], str) or not values["championSkinName"]:
        raise OutputGoldenError(context, "ChampionSkinName is not a non-empty string")
    if values["mResourceResolver"] is None:
        raise OutputGoldenError(context, "mResourceResolver is missing")
    if values["resourceResolverEntryKey"] is None:
        raise OutputGoldenError(context, "ResourceResolver entry key is missing")
    return values


def build_source_expectations(source: dict[str, Any]) -> dict[int, ChampionExpectation]:
    context = global_context("source-golden")
    schema_version = source.get("schemaVersion")
    if schema_version != 2 or not isinstance(source.get("champions"), list):
        raise OutputGoldenError(context, "unsupported source Golden schema")
    result: dict[int, ChampionExpectation] = {}
    for champion in source["champions"]:
        if not isinstance(champion, dict) or champion.get("status") != "success":
            continue
        champion_id = champion.get("championId")
        champion_name = champion.get("champion")
        if not isinstance(champion_id, int) or not isinstance(champion_name, str):
            raise OutputGoldenError(context, "source Golden champion identity is invalid")
        champion_context = OutputContext(champion_name, "*", "*", "source-golden")
        if champion_id in result:
            raise OutputGoldenError(champion_context, f"duplicate championId {champion_id}")
        pairs = champion.get("pairs")
        wad = champion.get("wad")
        if not isinstance(pairs, list) or not isinstance(wad, dict):
            raise OutputGoldenError(champion_context, "source Golden pairs/WAD identity is invalid")
        if _SHA256_RE.fullmatch(str(wad.get("sha256", ""))) is None:
            raise OutputGoldenError(champion_context, "source WAD SHA-256 is invalid")
        units_by_skin: dict[int, dict[str, PairExpectation]] = {}
        for pair in pairs:
            pair_context = pair.get("context") if isinstance(pair, dict) else None
            if not isinstance(pair_context, dict):
                raise OutputGoldenError(champion_context, "source pair has no context")
            skin_number = pair_context.get("skin_number")
            unit = pair_context.get("unit")
            pair_ctx = OutputContext(champion_name, skin_number or "*", str(unit), "source-golden")
            if not isinstance(skin_number, int) or skin_number <= 0:
                raise OutputGoldenError(pair_ctx, "source pair skin number is invalid")
            if not isinstance(unit, str) or not unit:
                raise OutputGoldenError(pair_ctx, "source pair unit is invalid")
            base_path = str(pair.get("basePath", "")).replace("\\", "/").lower()
            target_path = str(pair.get("targetPath", "")).replace("\\", "/").lower()
            base_match = _SKIN_PATH_RE.fullmatch(base_path)
            target_match = _SKIN_PATH_RE.fullmatch(target_path)
            if (
                base_match is None
                or target_match is None
                or base_match.group("unit").lower() != unit.lower()
                or target_match.group("unit").lower() != unit.lower()
                or int(base_match.group("skin")) != 0
                or int(target_match.group("skin")) != skin_number
            ):
                raise OutputGoldenError(pair_ctx, "source pair paths disagree with its context")
            base_sha256 = str(pair.get("baseSha256", ""))
            target_sha256 = str(pair.get("targetSha256", ""))
            if (
                _SHA256_RE.fullmatch(base_sha256) is None
                or _SHA256_RE.fullmatch(target_sha256) is None
            ):
                raise OutputGoldenError(pair_ctx, "source pair SHA-256 is invalid")
            per_skin = units_by_skin.setdefault(skin_number, {})
            if unit in per_skin:
                raise OutputGoldenError(pair_ctx, "duplicate source unit for skin")
            per_skin[unit] = PairExpectation(
                unit=unit,
                base_path=base_path,
                target_path=target_path,
                base_sha256=base_sha256,
                target_sha256=target_sha256,
            )
        if champion.get("pairedCount") != sum(
            len(units)
            for units in units_by_skin.values()
        ):
            raise OutputGoldenError(champion_context, "source Golden pairCount is inconsistent")
        if champion.get("skinSet") != sorted(units_by_skin):
            raise OutputGoldenError(champion_context, "source Golden skinSet is inconsistent")
        result[champion_id] = ChampionExpectation(
            champion_id=champion_id,
            champion=champion_name,
            wad=wad,
            units_by_skin=units_by_skin,
        )
    return result


def _portable_basename(value: object) -> str:
    return str(value).replace("\\", "/").rsplit("/", 1)[-1]


def validate_source_pool_bindings(
    source: dict[str, Any],
    benchmark_pool: dict[str, Any],
) -> list[dict[str, Any]]:
    context = global_context("source-pool-binding")
    source_champions = source.get("champions")
    pool_champions = benchmark_pool.get("champions")
    if not isinstance(source_champions, list) or not isinstance(pool_champions, list):
        raise OutputGoldenError(context, "source/benchmark champion lists are invalid")

    source_by_id: dict[int, dict[str, Any]] = {}
    for record in source_champions:
        if not isinstance(record, dict):
            raise OutputGoldenError(context, "source Golden champion record is invalid")
        champion_id = record.get("championId")
        if not isinstance(champion_id, int) or isinstance(champion_id, bool):
            raise OutputGoldenError(context, "source Golden championId is invalid")
        if champion_id in source_by_id:
            raise OutputGoldenError(context, f"duplicate source championId {champion_id}")
        source_by_id[champion_id] = record

    pool_by_id: dict[int, dict[str, Any]] = {}
    for record in pool_champions:
        if not isinstance(record, dict):
            raise OutputGoldenError(context, "benchmark pool champion record is invalid")
        champion_id = record.get("championId")
        if not isinstance(champion_id, int) or isinstance(champion_id, bool):
            raise OutputGoldenError(context, "benchmark pool championId is invalid")
        if champion_id in pool_by_id:
            raise OutputGoldenError(context, f"duplicate pool championId {champion_id}")
        pool_by_id[champion_id] = record

    if set(source_by_id) != set(pool_by_id):
        raise OutputGoldenError(
            context,
            "source Golden champion IDs differ from the fixed benchmark pool",
        )

    unsupported: list[dict[str, Any]] = []
    for champion_id, pool_champion in pool_by_id.items():
        source_champion = source_by_id[champion_id]
        champion = pool_champion.get("query")
        champion_context = OutputContext(
            str(champion),
            "*",
            "*",
            "source-pool-binding",
        )
        if not isinstance(champion, str) or not champion:
            raise OutputGoldenError(champion_context, "pool champion query is invalid")
        if source_champion.get("champion") != champion:
            raise OutputGoldenError(
                champion_context,
                "source Golden champion query differs from benchmark pool",
            )
        expectation = pool_champion.get("legacyExpectation")
        direct_expectation = pool_champion.get(
            "directExpectation",
            expectation,
        )
        if expectation == "success":
            if source_champion.get("status") != "success":
                raise OutputGoldenError(
                    champion_context,
                    "successful pool champion is not successful in source Golden",
                )
            source_wad = source_champion.get("wad")
            wad_name = pool_champion.get("wadName")
            if (
                not isinstance(source_wad, dict)
                or not isinstance(wad_name, str)
                or _portable_basename(source_wad.get("path")).casefold()
                != wad_name.casefold()
            ):
                raise OutputGoldenError(
                    champion_context,
                    "source Golden WAD name differs from benchmark pool wadName",
                )
            continue
        if expectation != "unsupported":
            raise OutputGoldenError(
                champion_context,
                f"unknown legacyExpectation {expectation!r}",
            )
        failure = source_champion.get("legacyFailure")
        if (
            not isinstance(failure, dict)
            or failure.get("validated") is not True
            or failure.get("type") != pool_champion.get("legacyFailureType")
            or failure.get("message") != pool_champion.get("legacyFailureMessage")
        ):
            raise OutputGoldenError(
                champion_context,
                "source Golden unsupported failure contract is not validated",
            )
        if direct_expectation == "success":
            direct_support = source_champion.get("directSupport")
            if (
                source_champion.get("status") != "success"
                or not isinstance(direct_support, dict)
                or direct_support.get("status") != "success"
                or direct_support.get("oracleVerified") is not True
            ):
                raise OutputGoldenError(
                    champion_context,
                    "Direct-supported legacy failure has no verified source Golden",
                )
            source_wad = source_champion.get("wad")
            wad_name = pool_champion.get("wadName")
            if (
                not isinstance(source_wad, dict)
                or not isinstance(wad_name, str)
                or _portable_basename(source_wad.get("path")).casefold()
                != wad_name.casefold()
            ):
                raise OutputGoldenError(
                    champion_context,
                    "source Golden WAD name differs from benchmark pool wadName",
                )
            unsupported_status = "direct_supported_legacy_unsupported"
        elif direct_expectation == "unsupported":
            if source_champion.get("status") != "expected_unsupported":
                raise OutputGoldenError(
                    champion_context,
                    "unsupported pool champion is not expected_unsupported in source Golden",
                )
            unsupported_status = "expected_unsupported"
        else:
            raise OutputGoldenError(
                champion_context,
                f"unknown directExpectation {direct_expectation!r}",
            )
        unsupported.append(
            {
                "championId": champion_id,
                "champion": champion,
                "status": unsupported_status,
                "legacyFailure": {
                    "type": failure["type"],
                    "message": failure["message"],
                    "validated": True,
                },
            }
        )
    return sorted(unsupported, key=lambda item: int(item["championId"]))


def successful_cold_runs(
    benchmark: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    context = global_context("benchmark-result")
    pool = benchmark.get("pool")
    runs = benchmark.get("runs")
    if benchmark.get("schemaVersion") != 2 or not isinstance(pool, dict):
        raise OutputGoldenError(context, "unsupported benchmark result schema")
    current_input_gate = benchmark.get("currentInputStability")
    if (
        not isinstance(current_input_gate, dict)
        or current_input_gate.get("status") != "passed"
    ):
        raise OutputGoldenError(
            context,
            "benchmark currentInputStability did not pass",
        )
    operation_gate = benchmark.get("operationBaselineGate")
    if (
        not isinstance(operation_gate, dict)
        or operation_gate.get("status") not in {"not_requested", "passed"}
    ):
        raise OutputGoldenError(
            context,
            "benchmark operationBaselineGate did not pass or opt out",
        )
    champions = pool.get("champions")
    if not isinstance(champions, list) or not isinstance(runs, list):
        raise OutputGoldenError(context, "benchmark pool/runs are invalid")
    by_id = {item.get("championId"): item for item in champions if isinstance(item, dict)}
    selected_ids = benchmark.get("selectedChampionIds")
    if (
        not isinstance(selected_ids, list)
        or not selected_ids
        or len(selected_ids) != len(set(selected_ids))
        or any(champion_id not in by_id for champion_id in selected_ids)
    ):
        raise OutputGoldenError(context, "selectedChampionIds is invalid")
    cold: dict[int, dict[str, Any]] = {}
    for run in runs:
        if not isinstance(run, dict) or run.get("scenario") != "app-cold-build":
            continue
        champion_id = run.get("championId")
        if champion_id not in by_id:
            raise OutputGoldenError(context, f"cold run has unknown championId {champion_id}")
        if champion_id in cold:
            raise OutputGoldenError(context, f"duplicate app-cold-build run for {champion_id}")
        cold[champion_id] = run
    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for champion_id in selected_ids:
        champion = by_id[champion_id]
        run = cold.get(champion_id)
        direct_expectation = champion.get(
            "directExpectation",
            champion.get("legacyExpectation"),
        )
        expected_status = (
            "success"
            if direct_expectation == "success"
            else "expected_unsupported"
        )
        if run is None or run.get("status") != expected_status:
            actual = None if run is None else run.get("status")
            raise OutputGoldenError(
                OutputContext(
                    str(champion.get("query")),
                    "*",
                    "*",
                    "benchmark-completeness",
                ),
                f"app-cold-build status is {actual!r}, expected {expected_status!r}",
            )
        if expected_status == "success":
            selected.append((champion, run))
    if not selected:
        raise OutputGoldenError(context, "benchmark has no successful app-cold-build runs")
    return selected


def validate_selection(
    run: dict[str, Any],
    metrics: dict[str, Any],
    pool_champion: dict[str, Any],
    expectation: ChampionExpectation,
    expected_output_root: Path,
) -> list[dict[str, Any]]:
    champion = str(pool_champion["query"])
    context = OutputContext(champion, "*", "*", "raw-metrics")
    if metrics.get("status") != "success":
        raise OutputGoldenError(context, "raw metrics status is not success")
    facts = metrics.get("facts")
    selection = facts.get("selection") if isinstance(facts, dict) else None
    run_fact = facts.get("run") if isinstance(facts, dict) else None
    if not isinstance(selection, list) or not isinstance(run_fact, dict):
        raise OutputGoldenError(context, "raw metrics selection/run facts are missing")
    try:
        recorded_output = Path(run_fact["outputRoot"]).resolve()
    except (KeyError, TypeError):
        raise OutputGoldenError(context, "raw metrics outputRoot is invalid") from None
    if recorded_output != expected_output_root.resolve():
        raise OutputGoldenError(
            context,
            f"raw metrics outputRoot is {recorded_output}, "
            f"expected {expected_output_root.resolve()}",
        )

    by_skin: dict[int, dict[str, Any]] = {}
    full_skin_ids: list[int] = []
    for item in selection:
        if not isinstance(item, dict):
            raise OutputGoldenError(context, "raw metrics selection item is invalid")
        skin_number = item.get("skinNumber")
        item_context = OutputContext(champion, skin_number or "*", "*", "raw-metrics")
        if item.get("championId") != expectation.champion_id:
            raise OutputGoldenError(item_context, "selection championId is inconsistent")
        if not isinstance(skin_number, int) or skin_number <= 0 or skin_number in by_skin:
            raise OutputGoldenError(item_context, "selection skinNumber is invalid or duplicated")
        if not isinstance(item.get("displayName"), str) or not item["displayName"]:
            raise OutputGoldenError(item_context, "selection displayName is invalid")
        expected_full_id = expectation.champion_id * 1000 + skin_number
        if item.get("fullSkinId") != expected_full_id:
            raise OutputGoldenError(
                item_context,
                f"selection fullSkinId is not {expected_full_id}",
            )
        by_skin[skin_number] = item
        full_skin_ids.append(expected_full_id)

    expected_skins = set(expectation.units_by_skin)
    if set(by_skin) != expected_skins:
        raise OutputGoldenError(
            context,
            f"selection skins differ: expected {sorted(expected_skins)}, "
            f"got {sorted(by_skin)}",
        )
    compact_metrics = run.get("metrics")
    compact_skins = (
        compact_metrics.get("skinNumbers")
        if isinstance(compact_metrics, dict)
        else None
    )
    compact_full_ids = (
        compact_metrics.get("fullSkinIds")
        if isinstance(compact_metrics, dict)
        else None
    )
    if (
        not isinstance(compact_skins, list)
        or len(compact_skins) != len(expected_skins)
        or set(compact_skins) != expected_skins
        or compact_full_ids != sorted(full_skin_ids)
    ):
        raise OutputGoldenError(context, "compact result and raw selection skins disagree")
    canonical = json.dumps(sorted(full_skin_ids), separators=(",", ":")).encode("ascii")
    skin_set_sha256 = hashlib.sha256(canonical).hexdigest()
    if (
        run.get("skinSetSha256") != skin_set_sha256
        or run.get("expectedSkinSetSha256") != skin_set_sha256
        or run.get("validationErrors") != []
    ):
        raise OutputGoldenError(context, "benchmark skin-set digest/validation is inconsistent")
    expected_count = len(expected_skins)
    if (
        run.get("skinCount") != expected_count
        or run.get("expectedSkinCount") != expected_count
        or pool_champion.get("skinCount") != expected_count
    ):
        raise OutputGoldenError(context, "benchmark skin counts disagree with source Golden")
    return [by_skin[skin] for skin in sorted(by_skin)]


def require_same_wad_identity(
    benchmark: dict[str, Any],
    expectation: ChampionExpectation,
    pool_champion: dict[str, Any],
) -> dict[str, Any]:
    context = OutputContext(expectation.champion, "*", "*", "source-identity")
    identity = benchmark.get("identity")
    client = identity.get("client") if isinstance(identity, dict) else None
    wads = client.get("wads") if isinstance(client, dict) else None
    matches = [
        item
        for item in wads or []
        if isinstance(item, dict) and item.get("championId") == expectation.champion_id
    ]
    if len(matches) != 1:
        raise OutputGoldenError(context, f"expected 1 benchmark WAD identity, got {len(matches)}")
    benchmark_wad = matches[0]
    for field in ("size", "modifiedNs", "sha256"):
        if benchmark_wad.get(field) != expectation.wad.get(field):
            raise OutputGoldenError(context, f"source Golden WAD {field} differs from benchmark")
    try:
        source_path = Path(str(expectation.wad["path"])).resolve()
        benchmark_path = Path(str(benchmark_wad["path"])).resolve()
    except KeyError:
        raise OutputGoldenError(context, "source/benchmark WAD path is missing") from None
    if source_path != benchmark_path:
        raise OutputGoldenError(context, "source Golden WAD path differs from benchmark")
    wad_name = pool_champion.get("wadName")
    if (
        not isinstance(wad_name, str)
        or _portable_basename(source_path).casefold() != wad_name.casefold()
        or _portable_basename(benchmark_path).casefold() != wad_name.casefold()
    ):
        raise OutputGoldenError(context, "source/benchmark WAD name differs from pool wadName")
    return benchmark_wad


def require_raw_source_wad_identity(
    metrics: dict[str, Any],
    benchmark_wad: dict[str, Any],
    champion: str,
) -> None:
    context = OutputContext(champion, "*", "*", "raw-source-identity")
    facts = metrics.get("facts")
    source_wads = facts.get("sourceWads") if isinstance(facts, dict) else None
    if not isinstance(source_wads, list) or len(source_wads) != 1:
        raise OutputGoldenError(context, "raw metrics must contain exactly one source WAD")
    source_wad = source_wads[0]
    if not isinstance(source_wad, dict):
        raise OutputGoldenError(context, "raw source WAD identity is invalid")
    try:
        raw_path = Path(str(source_wad["path"])).resolve()
        benchmark_path = Path(str(benchmark_wad["path"])).resolve()
    except KeyError:
        raise OutputGoldenError(context, "raw source WAD path is missing") from None
    if raw_path != benchmark_path or any(
        source_wad.get(field) != benchmark_wad.get(field)
        for field in ("size", "modifiedNs")
    ):
        raise OutputGoldenError(
            context,
            "raw metrics source WAD differs from benchmark/source Golden identity",
        )


def require_bundled_ritobin_identity(
    benchmark: dict[str, Any],
    actual_identity: dict[str, Any],
) -> dict[str, Any]:
    context = global_context("tool-identity")
    identity = benchmark.get("identity")
    tools = identity.get("tools") if isinstance(identity, dict) else None
    matches = [
        item
        for item in tools or []
        if isinstance(item, dict)
        and str(item.get("path", "")).replace("\\", "/").lower()
        == "bin/ritobin_cli.exe"
    ]
    if len(matches) != 1:
        raise OutputGoldenError(
            context,
            f"expected 1 benchmark Ritobin identity, got {len(matches)}",
        )
    for field in ("size", "sha256"):
        if matches[0].get(field) != actual_identity.get(field):
            raise OutputGoldenError(context, f"bundled Ritobin {field} differs from benchmark")
    return actual_identity


def require_bundled_ritobin_support_identities(
    benchmark: dict[str, Any],
) -> list[dict[str, Any]]:
    context = global_context("tool-support-identity")
    identity = benchmark.get("identity")
    tools = identity.get("tools") if isinstance(identity, dict) else None
    if not isinstance(tools, list):
        raise OutputGoldenError(context, "benchmark tool identities are missing")

    actual_identities: list[dict[str, Any]] = []
    for relative_path in BUNDLED_RITOBIN_SUPPORT_RELATIVE_PATHS:
        label = relative_path.as_posix().casefold()
        matches = [
            item
            for item in tools
            if isinstance(item, dict)
            and str(item.get("path", "")).replace("\\", "/").casefold()
            == label
        ]
        if len(matches) != 1:
            raise OutputGoldenError(
                context,
                f"expected 1 benchmark identity for {relative_path.as_posix()}, "
                f"got {len(matches)}",
            )
        actual = file_identity(REPO_ROOT / relative_path, context)
        for field in ("size", "sha256"):
            if matches[0].get(field) != actual.get(field):
                raise OutputGoldenError(
                    context,
                    f"bundled support file {relative_path.as_posix()} "
                    f"{field} differs from benchmark",
                )
        actual_identities.append(actual)
    return actual_identities


def collect_archives(
    output_root: Path,
    work_root: Path,
    selections: list[dict[str, Any]],
    pool_champion: dict[str, Any],
    expectation: ChampionExpectation,
    expected_path_hashes: dict[str, int],
) -> list[CollectedArchive]:
    champion = str(pool_champion["query"])
    discovery_context = OutputContext(champion, "*", "*", "archive-discovery")
    zip_paths = discover_output_zips(output_root, discovery_context)
    expected_names = {
        f"{sanitize_for_windows(str(item['displayName']))}.zip": item
        for item in selections
    }
    if len(expected_names) != len(selections):
        raise OutputGoldenError(discovery_context, "selected skins collide after sanitizing")
    actual_by_name: dict[str, list[Path]] = {}
    for path in zip_paths:
        actual_by_name.setdefault(path.name.casefold(), []).append(path)
    expected_keys = {name.casefold() for name in expected_names}
    extras = [path for path in zip_paths if path.name.casefold() not in expected_keys]
    if extras:
        raise OutputGoldenError(discovery_context, f"unexpected ZIP outputs: {extras}")

    collected: list[CollectedArchive] = []
    for filename, selection in expected_names.items():
        skin_number = int(selection["skinNumber"])
        context = OutputContext(champion, skin_number, "<archive>", "archive-discovery")
        matches = actual_by_name.get(filename.casefold(), [])
        if len(matches) != 1:
            raise OutputGoldenError(context, f"expected 1 {filename}, got {len(matches)}")
        path = resolve_output_file(output_root, matches[0], context)
        try:
            archive_data = path.read_bytes()
        except OSError as exc:
            raise OutputGoldenError(context, f"failed reading {path}: {exc}") from exc
        try:
            relative_path = path.resolve().relative_to(work_root.resolve()).as_posix()
        except ValueError:
            raise OutputGoldenError(context, f"archive is outside work root: {path}") from None
        collected.append(
            inspect_archive_bytes(
                archive_data,
                relative_path=relative_path,
                champion=champion,
                skin_number=skin_number,
                display_name=str(selection["displayName"]),
                expected_wad_name=str(pool_champion["wadName"]),
                expected_units=expectation.units_by_skin[skin_number],
                expected_path_hashes=expected_path_hashes,
            )
        )
    return sorted(collected, key=lambda item: int(item.context.skin))


def collect_source_base_bins(
    wad_path: Path,
    expectation: ChampionExpectation,
    expected_path_hashes: dict[str, int],
) -> dict[tuple[str, str], bytes]:
    context = OutputContext(expectation.champion, "*", "*", "source-base-wad")
    try:
        wad_data = wad_path.read_bytes()
    except OSError as exc:
        raise OutputGoldenError(context, f"failed reading source WAD: {exc}") from exc
    if sha256_bytes(wad_data) != expectation.wad.get("sha256"):
        raise OutputGoldenError(context, "source WAD bytes no longer match benchmark identity")
    _, entries = parse_wad_index(wad_data, context)
    result: dict[tuple[str, str], bytes] = {}
    for skin_number in sorted(expectation.units_by_skin):
        for unit, pair in sorted(expectation.units_by_skin[skin_number].items()):
            key = (unit, pair.base_sha256)
            if key in result:
                continue
            unit_context = OutputContext(
                expectation.champion,
                skin_number,
                unit,
                "source-base-chunk",
            )
            path_hash = expected_path_hashes.get(pair.base_path)
            if path_hash is None:
                raise OutputGoldenError(
                    unit_context,
                    f"fixed dictionary has no hash for {pair.base_path}",
                )
            entry = entries.get(path_hash)
            if entry is None:
                raise OutputGoldenError(
                    unit_context,
                    f"source base path hash {path_hash:016x} is missing",
                )
            data = read_wad_entry(wad_data, entry, unit_context)
            if sha256_bytes(data) != pair.base_sha256:
                raise OutputGoldenError(unit_context, "source base BIN SHA-256 differs from Golden")
            result[key] = data
    return result


def build_conversion_inputs(
    archives: list[CollectedArchive],
    source_bases: dict[tuple[str, str], bytes],
) -> tuple[list[ConversionInput], dict[tuple[int, str], str], dict[tuple[str, str], str]]:
    inputs: list[ConversionInput] = []
    output_keys: dict[tuple[int, str], str] = {}
    base_keys: dict[tuple[str, str], str] = {}
    for archive in archives:
        skin_number = int(archive.context.skin)
        for unit in archive.units:
            key = f"output:{skin_number}:{unit.expectation.unit}"
            output_keys[(skin_number, unit.expectation.unit)] = key
            inputs.append(
                ConversionInput(
                    key=key,
                    context=OutputContext(
                        unit.context.champion,
                        skin_number,
                        unit.expectation.unit,
                        "ritobin-output-conversion",
                    ),
                    bin_data=unit.bin_data,
                )
            )
    champion = archives[0].context.champion if archives else "<global>"
    for index, ((unit, base_sha256), data) in enumerate(sorted(source_bases.items())):
        key = f"base:{index}:{unit}:{base_sha256}"
        base_keys[(unit, base_sha256)] = key
        inputs.append(
            ConversionInput(
                key=key,
                context=OutputContext(champion, "*", unit, "ritobin-base-conversion"),
                bin_data=data,
            )
        )
    return inputs, output_keys, base_keys


def run_bundled_ritobin(
    items: list[ConversionInput],
    temp_root: Path,
) -> dict[str, tuple[dict[str, Any], bytes]]:
    if not items:
        raise OutputGoldenError(global_context("ritobin-conversion"), "no BIN inputs")
    input_root = temp_root / "input"
    output_root = temp_root / "output"
    input_root.mkdir(parents=True)
    output_root.mkdir()
    paths: dict[str, tuple[Path, OutputContext]] = {}
    for index, item in enumerate(items):
        stem = f"{index:06d}"
        input_path = input_root / f"{stem}.bin"
        try:
            input_path.write_bytes(item.bin_data)
        except OSError as exc:
            raise OutputGoldenError(item.context, f"failed staging BIN: {exc}") from exc
        paths[item.key] = (output_root / f"{stem}.json", item.context)
    command = [
        str(BUNDLED_RITOBIN),
        "-r",
        "-i",
        "bin",
        "-o",
        "json",
        str(input_root),
        str(output_root),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True)
    except OSError as exc:
        raise OutputGoldenError(
            items[0].context,
            f"failed starting bundled Ritobin: {exc}",
        ) from exc
    missing = [
        (key, path, context)
        for key, (path, context) in paths.items()
        if not path.is_file()
    ]
    if completed.returncode != 0 or missing:
        context = missing[0][2] if missing else items[0].context
        tail = "\n".join((completed.stdout + completed.stderr).splitlines()[-20:])
        raise OutputGoldenError(
            context,
            f"bundled Ritobin exited {completed.returncode}; "
            f"missing={len(missing)}; {tail}",
        )
    converted: dict[str, tuple[dict[str, Any], bytes]] = {}
    for key, (path, context) in paths.items():
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise OutputGoldenError(context, f"invalid Ritobin JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise OutputGoldenError(context, "Ritobin JSON root is not an object")
        converted[key] = (payload, raw)
    return converted


def archive_record(
    archive: CollectedArchive,
    converted: dict[str, tuple[dict[str, Any], bytes]],
    output_keys: dict[tuple[int, str], str],
    base_keys: dict[tuple[str, str], str],
) -> dict[str, Any]:
    skin_number = int(archive.context.skin)
    units: list[dict[str, Any]] = []
    for unit in archive.units:
        identity_context = OutputContext(
            unit.context.champion,
            skin_number,
            unit.expectation.unit,
            "rebased-bin-semantics",
        )
        output_key = output_keys.get((skin_number, unit.expectation.unit))
        base_key = base_keys.get((unit.expectation.unit, unit.expectation.base_sha256))
        if output_key not in converted or base_key not in converted:
            raise OutputGoldenError(identity_context, "converted JSON result is missing")
        output_payload, output_json = converted[output_key]
        base_payload, base_json = converted[base_key]
        output_identity = extract_identity_fields(output_payload, identity_context)
        base_identity = extract_identity_fields(
            base_payload,
            OutputContext(
                unit.context.champion,
                skin_number,
                unit.expectation.unit,
                "source-base-semantics",
            ),
        )
        if output_identity != base_identity:
            raise OutputGoldenError(
                identity_context,
                f"identity/link fields differ from source base: "
                f"output={output_identity}, base={base_identity}",
            )
        units.append(
            {
                "unit": unit.expectation.unit,
                "path": unit.expectation.base_path,
                "pathHash": f"{unit.path_hash:016x}",
                "binSize": len(unit.bin_data),
                "binSha256": sha256_bytes(unit.bin_data),
                "jsonSha256": sha256_bytes(output_json),
                "identity": output_identity,
                "source": {
                    "baseSha256": unit.expectation.base_sha256,
                    "baseJsonSha256": sha256_bytes(base_json),
                    "targetSha256": unit.expectation.target_sha256,
                },
            }
        )
    return {
        "skinNumber": skin_number,
        "displayName": archive.display_name,
        "path": archive.relative_path,
        "size": len(archive.archive_data),
        "sha256": sha256_bytes(archive.archive_data),
        "metaSha256": sha256_bytes(archive.meta_data),
        "info": archive.info,
        "wad": {
            "version": archive.wad_version,
            "size": len(archive.wad_data),
            "sha256": sha256_bytes(archive.wad_data),
            "pathHashes": [f"{item:016x}" for item in archive.path_hashes],
        },
        "units": units,
    }


def write_json_atomically(path: Path, payload: Any) -> None:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    )
    temp_path = Path(temp_file.name)
    try:
        with temp_file:
            json.dump(payload, temp_file, ensure_ascii=False, indent=2)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


def running_result() -> dict[str, Any]:
    return {
        "schemaVersion": RESULT_SCHEMA_VERSION,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "complete": False,
        "champions": [],
    }


def failure_result(error: OutputGoldenError) -> dict[str, Any]:
    return {
        "schemaVersion": RESULT_SCHEMA_VERSION,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "status": "failure",
        "complete": True,
        "error": str(error),
        "champions": [],
    }


def write_completed_result(
    path: Path,
    result: dict[str, Any],
    *,
    expected_count: int,
    failed: bool,
) -> None:
    processed_count = len(result.get("champions", []))
    if processed_count != expected_count:
        raise OutputGoldenError(
            global_context("result-completeness"),
            f"processed {processed_count} champions, expected {expected_count}",
        )
    result["expectedChampionCount"] = expected_count
    result["processedChampionCount"] = processed_count
    result["status"] = "failure" if failed else "success"
    result["complete"] = True
    write_json_atomically(path, result)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-result", type=Path, required=True)
    parser.add_argument("--source-golden", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--champion", action="append", default=[])
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    benchmark_path = args.benchmark_result.resolve()
    source_path = args.source_golden.resolve()
    work_root = args.work_root.resolve()
    benchmark = read_json(benchmark_path, global_context("benchmark-result"))
    source = read_json(source_path, global_context("source-golden"))
    expectations = build_source_expectations(source)
    expected_path_hashes, hash_dictionary_identity = load_fixed_path_hashes(
        source,
        expectations,
    )
    cold_runs = successful_cold_runs(benchmark)
    if args.champion:
        wanted = {str(item).casefold() for item in args.champion}
        cold_runs = [
            pair
            for pair in cold_runs
            if str(pair[0]["query"]).casefold() in wanted
        ]
        found = {str(pair[0]["query"]).casefold() for pair in cold_runs}
        if found != wanted:
            raise OutputGoldenError(
                global_context("arguments"),
                f"requested champions are not successful cold runs: "
                f"{sorted(wanted - found)}",
            )

    benchmark_pool = benchmark["pool"]
    if (
        source.get("poolId") != benchmark_pool.get("poolId")
        or source.get("gameVersion") != benchmark_pool.get("gameVersion")
    ):
        raise OutputGoldenError(
            global_context("source-identity"),
            "source Golden pool/game version differs from benchmark",
        )
    unsupported_champions = validate_source_pool_bindings(
        source,
        benchmark_pool,
    )
    ritobin_identity = require_bundled_ritobin_identity(
        benchmark,
        file_identity(BUNDLED_RITOBIN, global_context("tool-identity")),
    )
    ritobin_support_identities = (
        require_bundled_ritobin_support_identities(benchmark)
    )
    starting_tool_inputs = {
        "ritobin": ritobin_identity,
        "supportFiles": ritobin_support_identities,
    }
    result: dict[str, Any] = {
        "schemaVersion": RESULT_SCHEMA_VERSION,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "complete": False,
        "phase": benchmark.get("phase"),
        "poolId": benchmark_pool.get("poolId"),
        "gameVersion": benchmark_pool.get("gameVersion"),
        "expectedChampionCount": len(cold_runs),
        "processedChampionCount": 0,
        "inputs": {
            "benchmarkResult": file_identity(
                benchmark_path,
                global_context("benchmark-result"),
            ),
            "sourceGolden": file_identity(
                source_path,
                global_context("source-golden"),
            ),
            "workRoot": str(work_root),
        },
        "tools": starting_tool_inputs,
        "inputStability": {
            "status": "pending",
            "startingTools": starting_tool_inputs,
        },
        "oracles": {
            "pathHashes": {
                "kind": "fixed-dictionary",
                "identity": hash_dictionary_identity,
            }
        },
        "unsupportedChampions": unsupported_champions,
        "champions": [],
    }
    failed = False
    phase = str(benchmark.get("phase"))
    for pool_champion, cold_run in cold_runs:
        champion_id = int(pool_champion["championId"])
        champion_name = str(pool_champion["query"])
        try:
            expectation = expectations.get(champion_id)
            if expectation is None:
                raise OutputGoldenError(
                    OutputContext(champion_name, "*", "*", "source-golden"),
                    "successful benchmark champion has no successful source Golden",
                )
            benchmark_wad = require_same_wad_identity(
                benchmark,
                expectation,
                pool_champion,
            )
            raw_path = (
                work_root
                / "raw"
                / phase
                / str(champion_id)
                / "app-cold-build.metrics.json"
            )
            referenced_raw = Path(str(cold_run.get("rawMetrics", ""))).resolve()
            if referenced_raw != raw_path.resolve():
                raise OutputGoldenError(
                    OutputContext(champion_name, "*", "*", "raw-metrics"),
                    f"compact rawMetrics reference is {referenced_raw}, "
                    f"expected {raw_path.resolve()}",
                )
            metrics = read_json(
                raw_path,
                OutputContext(champion_name, "*", "*", "raw-metrics"),
            )
            output_root = (
                work_root
                / "work"
                / phase
                / str(champion_id)
                / "output"
            )
            selections = validate_selection(
                cold_run,
                metrics,
                pool_champion,
                expectation,
                output_root,
            )
            require_raw_source_wad_identity(
                metrics,
                benchmark_wad,
                champion_name,
            )
            archives = collect_archives(
                output_root,
                work_root,
                selections,
                pool_champion,
                expectation,
                expected_path_hashes,
            )
            source_wad = Path(str(benchmark_wad["path"])).resolve()
            source_bases = collect_source_base_bins(
                source_wad,
                expectation,
                expected_path_hashes,
            )
            conversion_inputs, output_keys, base_keys = build_conversion_inputs(
                archives,
                source_bases,
            )
            try:
                with tempfile.TemporaryDirectory(
                    prefix=f".golden-output-{champion_id}-",
                    dir=work_root,
                ) as temp_name:
                    converted = run_bundled_ritobin(
                        conversion_inputs,
                        Path(temp_name),
                    )
            except OSError as exc:
                raise OutputGoldenError(
                    OutputContext(champion_name, "*", "*", "ritobin-conversion"),
                    f"failed creating conversion workspace: {exc}",
                ) from exc
            archive_records = [
                archive_record(
                    archive,
                    converted,
                    output_keys,
                    base_keys,
                )
                for archive in archives
            ]
            result["champions"].append(
                {
                    "championId": champion_id,
                    "champion": champion_name,
                    "status": "success",
                    "sourceWad": benchmark_wad,
                    "rawMetrics": file_identity(
                        raw_path,
                        OutputContext(champion_name, "*", "*", "raw-metrics"),
                    ),
                    "skinCount": len(archives),
                    "unitCount": sum(len(item.units) for item in archives),
                    "archives": archive_records,
                }
            )
        except OutputGoldenError as exc:
            failed = True
            result["champions"].append(
                {
                    "championId": champion_id,
                    "champion": champion_name,
                    "status": "failure",
                    "error": str(exc),
                }
            )
    try:
        ending_tool_inputs = {
            "ritobin": require_bundled_ritobin_identity(
                benchmark,
                file_identity(
                    BUNDLED_RITOBIN,
                    global_context("tool-identity"),
                ),
            ),
            "supportFiles": require_bundled_ritobin_support_identities(
                benchmark
            ),
        }
    except OutputGoldenError as exc:
        failed = True
        result["inputStability"] = {
            "status": "failed",
            "startingTools": starting_tool_inputs,
            "error": str(exc),
        }
    else:
        changed = ending_tool_inputs != starting_tool_inputs
        failed = failed or changed
        result["inputStability"] = {
            "status": "failed" if changed else "passed",
            "startingTools": starting_tool_inputs,
            "endingTools": ending_tool_inputs,
            "changedSections": ["tools"] if changed else [],
        }
    write_completed_result(
        args.output,
        result,
        expected_count=len(cold_runs),
        failed=failed,
    )
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        write_json_atomically(args.output, running_result())
    except OSError as exc:
        print(f"failed writing running result: {exc}", file=sys.stderr, flush=True)
        return 1
    try:
        return run(args)
    except OutputGoldenError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        try:
            write_json_atomically(args.output, failure_result(exc))
        except OSError as write_exc:
            print(
                f"failed writing failure result: {write_exc}",
                file=sys.stderr,
                flush=True,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
