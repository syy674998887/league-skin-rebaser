"""Compact, content-bound index of standard champion skin paths."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .registry_write import (
    commit_atomic_json,
    exclusive_registry_lock,
    prepare_atomic_json,
)
from .wad_access import wad_path_hash


INDEX_SCHEMA_VERSION = 1
MAX_CACHE_BYTES = 16 * 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_HASH_RE = re.compile(r"[0-9a-f]{16}\Z")
_UNIT_RE = re.compile(r"[a-z0-9_]+\Z")
_SKIN_PATH_BYTES_RE = re.compile(
    rb"data/characters/(?P<unit>[a-z0-9_]+)/skins/"
    rb"skin(?P<skin>0|[1-9][0-9]{0,2})\.bin\Z"
)


class HashSkinIndexError(RuntimeError):
    """The compact index cannot be proven against its source dictionary."""


@dataclass(frozen=True, order=True)
class HashSkinRecord:
    """One standard skin BIN path declared by hashes.game."""

    unit: str
    skin_number: int
    path_hash: int

    def __post_init__(self) -> None:
        if not isinstance(self.unit, str) or _UNIT_RE.fullmatch(self.unit) is None:
            raise ValueError(f"invalid champion unit name: {self.unit!r}")
        if (
            isinstance(self.skin_number, bool)
            or not isinstance(self.skin_number, int)
            or not 0 <= self.skin_number <= 999
        ):
            raise ValueError(f"invalid skin number: {self.skin_number!r}")
        if (
            isinstance(self.path_hash, bool)
            or not isinstance(self.path_hash, int)
            or not 0 <= self.path_hash <= 0xFFFFFFFFFFFFFFFF
        ):
            raise ValueError(f"invalid 64-bit path hash: {self.path_hash!r}")

    @property
    def path(self) -> str:
        return (
            f"data/characters/{self.unit}/skins/"
            f"skin{self.skin_number}.bin"
        )


@dataclass(frozen=True)
class HashSkinIndex:
    """Validated skin records plus fast lookups by unit and path hash."""

    source_size: int
    source_modified_ns: int
    source_row_count: int
    source_sha256: str
    relevant_sha256: str
    records: tuple[HashSkinRecord, ...]
    _by_unit: Mapping[str, tuple[HashSkinRecord, ...]] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _by_hash: Mapping[int, HashSkinRecord] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        for label, value in (
            ("source_size", self.source_size),
            ("source_row_count", self.source_row_count),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        if (
            isinstance(self.source_modified_ns, bool)
            or not isinstance(self.source_modified_ns, int)
            or self.source_modified_ns < 0
        ):
            raise ValueError("source_modified_ns must be a non-negative integer")
        if (
            not isinstance(self.source_sha256, str)
            or _SHA256_RE.fullmatch(self.source_sha256) is None
        ):
            raise ValueError("source_sha256 must be lowercase SHA-256")
        if (
            not isinstance(self.relevant_sha256, str)
            or _SHA256_RE.fullmatch(self.relevant_sha256) is None
        ):
            raise ValueError("relevant_sha256 must be lowercase SHA-256")
        if not self.records or self.records != tuple(sorted(self.records)):
            raise ValueError("skin records must be non-empty and sorted")
        if _records_sha256(self.records) != self.relevant_sha256:
            raise ValueError("relevant skin record digest does not match")

        by_unit: dict[str, list[HashSkinRecord]] = {}
        by_hash: dict[int, HashSkinRecord] = {}
        keys: set[tuple[str, int]] = set()
        for record in self.records:
            key = (record.unit, record.skin_number)
            if key in keys:
                raise ValueError(f"duplicate standard skin path: {record.path}")
            keys.add(key)
            previous = by_hash.get(record.path_hash)
            if previous is not None:
                raise ValueError(
                    f"skin path hash collision: {previous.path} and {record.path}"
                )
            by_hash[record.path_hash] = record
            by_unit.setdefault(record.unit, []).append(record)
        object.__setattr__(
            self,
            "_by_unit",
            MappingProxyType(
                {
                    unit: tuple(records)
                    for unit, records in sorted(by_unit.items())
                }
            ),
        )
        object.__setattr__(self, "_by_hash", MappingProxyType(by_hash))

    @property
    def units(self) -> tuple[str, ...]:
        return tuple(self._by_unit)

    def records_for_unit(self, unit: str) -> tuple[HashSkinRecord, ...]:
        if not isinstance(unit, str) or _UNIT_RE.fullmatch(unit) is None:
            raise ValueError(f"invalid champion unit name: {unit!r}")
        return self._by_unit.get(unit, ())

    def record_for(self, unit: str, skin_number: int) -> HashSkinRecord | None:
        if (
            isinstance(skin_number, bool)
            or not isinstance(skin_number, int)
            or not 0 <= skin_number <= 999
        ):
            raise ValueError(f"invalid skin number: {skin_number!r}")
        for record in self.records_for_unit(unit):
            if record.skin_number == skin_number:
                return record
        return None

    def record_for_hash(self, path_hash: int) -> HashSkinRecord | None:
        if (
            isinstance(path_hash, bool)
            or not isinstance(path_hash, int)
            or not 0 <= path_hash <= 0xFFFFFFFFFFFFFFFF
        ):
            raise ValueError(f"invalid 64-bit path hash: {path_hash!r}")
        return self._by_hash.get(path_hash)

    def subset_sha256(self, units: Iterable[str]) -> str:
        selected: list[HashSkinRecord] = []
        seen: set[str] = set()
        for unit in units:
            if unit in seen:
                continue
            seen.add(unit)
            selected.extend(self.records_for_unit(unit))
        return _records_sha256(tuple(sorted(selected)))

    def fact(self) -> dict[str, object]:
        return {
            "schemaVersion": INDEX_SCHEMA_VERSION,
            "sourceSize": self.source_size,
            "sourceModifiedNs": self.source_modified_ns,
            "sourceRows": self.source_row_count,
            "sourceSha256": self.source_sha256,
            "relevantSha256": self.relevant_sha256,
            "records": len(self.records),
            "units": len(self._by_unit),
        }


@dataclass(frozen=True)
class HashSkinIndexResult:
    """Cache action and loaded compact index."""

    action: str
    path: Path
    index: HashSkinIndex

    def fact(self) -> dict[str, object]:
        return {
            "action": self.action,
            "path": str(self.path),
            **self.index.fact(),
        }


def ensure_hash_skin_index(
    source_path: Path | str,
    cache_path: Path | str,
    *,
    expected_source_sha256: str | None = None,
    expected_source_size: int | None = None,
    lock_timeout_seconds: float = 600.0,
) -> HashSkinIndexResult:
    """Load a matching compact index or rebuild it from hashes.game."""

    source = Path(source_path).resolve()
    cache = Path(cache_path).resolve()
    if source == cache:
        raise ValueError("hash dictionary and compact index paths must differ")
    if expected_source_sha256 is not None and (
        not isinstance(expected_source_sha256, str)
        or _SHA256_RE.fullmatch(expected_source_sha256) is None
    ):
        raise ValueError("expected_source_sha256 must be lowercase SHA-256")
    if expected_source_size is not None and (
        isinstance(expected_source_size, bool)
        or not isinstance(expected_source_size, int)
        or expected_source_size < 1
    ):
        raise ValueError("expected_source_size must be a positive integer")
    _require_regular_source(source)
    _require_safe_cache(cache)
    cache.parent.mkdir(parents=True, exist_ok=True)

    source_stat = source.stat()
    _validate_expected_size(source_stat.st_size, expected_source_size)
    cached = _load_matching_cache(
        cache,
        source_stat,
        expected_source_sha256,
    )
    if cached is not None:
        return HashSkinIndexResult("current", cache, cached)

    with exclusive_registry_lock(cache, timeout_seconds=lock_timeout_seconds):
        _require_regular_source(source)
        source_stat = source.stat()
        _validate_expected_size(source_stat.st_size, expected_source_size)
        cached = _load_matching_cache(
            cache,
            source_stat,
            expected_source_sha256,
        )
        if cached is not None:
            return HashSkinIndexResult("current", cache, cached)

        index = _scan_source(
            source,
            expected_source_sha256=expected_source_sha256,
        )
        payload = _index_document(index)
        temp_path, digest = prepare_atomic_json(cache, payload)
        commit_atomic_json(temp_path, cache, digest)
        committed = _load_matching_cache(
            cache,
            source.stat(),
            index.source_sha256,
        )
        if committed is None:
            raise HashSkinIndexError(
                "committed compact hash index failed post-write validation"
            )
        return HashSkinIndexResult("rebuilt", cache, committed)


def _scan_source(
    source: Path,
    *,
    expected_source_sha256: str | None,
) -> HashSkinIndex:
    before = source.stat()
    digest = hashlib.sha256()
    records: list[HashSkinRecord] = []
    rows = 0
    with source.open("rb") as stream:
        for rows, raw_line in enumerate(stream, start=1):
            digest.update(raw_line)
            logical = raw_line.rstrip(b"\r\n")
            if len(logical) < 18 or logical[16:17] != b" ":
                continue
            match = _SKIN_PATH_BYTES_RE.fullmatch(logical[17:])
            if match is None:
                continue
            try:
                declared_hash = int(logical[:16], 16)
                unit = match.group("unit").decode("ascii")
                skin_number = int(match.group("skin"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise HashSkinIndexError(
                    f"invalid standard skin row {rows} in {source}"
                ) from exc
            record = HashSkinRecord(unit, skin_number, declared_hash)
            if wad_path_hash(record.path) != declared_hash:
                raise HashSkinIndexError(
                    f"standard skin row {rows} has a mismatched XXH64: "
                    f"{record.path}"
                )
            records.append(record)
    after = source.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise HashSkinIndexError(
            f"hash dictionary changed while building compact index: {source}"
        )
    source_sha256 = digest.hexdigest()
    if (
        expected_source_sha256 is not None
        and source_sha256 != expected_source_sha256
    ):
        raise HashSkinIndexError(
            "hash dictionary SHA-256 differs from the validated updater result"
        )
    if rows < 1 or not records:
        raise HashSkinIndexError(
            f"hash dictionary contains no standard champion skin paths: {source}"
        )
    sorted_records = tuple(sorted(records))
    try:
        return HashSkinIndex(
            source_size=after.st_size,
            source_modified_ns=after.st_mtime_ns,
            source_row_count=rows,
            source_sha256=source_sha256,
            relevant_sha256=_records_sha256(sorted_records),
            records=sorted_records,
        )
    except ValueError as exc:
        raise HashSkinIndexError(
            f"invalid standard skin records in {source}: {exc}"
        ) from exc


def _load_matching_cache(
    cache: Path,
    source_stat: os.stat_result,
    expected_source_sha256: str | None,
) -> HashSkinIndex | None:
    if not cache.is_file() or cache.is_symlink():
        return None
    try:
        if cache.stat().st_size > MAX_CACHE_BYTES:
            return None
        raw = cache.read_bytes()
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
        if not isinstance(payload, dict):
            return None
        index = _parse_index_document(payload)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        HashSkinIndexError,
        TypeError,
        ValueError,
    ):
        return None
    if index.source_size != source_stat.st_size:
        return None
    if index.source_modified_ns != source_stat.st_mtime_ns:
        return None
    if expected_source_sha256 is not None:
        if index.source_sha256 != expected_source_sha256:
            return None
    return index


def _index_document(index: HashSkinIndex) -> dict[str, object]:
    return {
        "schemaVersion": INDEX_SCHEMA_VERSION,
        "source": {
            "size": index.source_size,
            "modifiedNs": index.source_modified_ns,
            "rows": index.source_row_count,
            "sha256": index.source_sha256,
        },
        "recordCount": len(index.records),
        "relevantSha256": index.relevant_sha256,
        "records": [
            [record.unit, record.skin_number, f"{record.path_hash:016x}"]
            for record in index.records
        ],
    }


def _parse_index_document(payload: Mapping[str, Any]) -> HashSkinIndex:
    if set(payload) != {
        "schemaVersion",
        "source",
        "recordCount",
        "relevantSha256",
        "records",
    } or payload.get("schemaVersion") != INDEX_SCHEMA_VERSION:
        raise HashSkinIndexError("unsupported compact hash index document")
    source = payload.get("source")
    raw_records = payload.get("records")
    if not isinstance(source, dict) or set(source) != {
        "size",
        "modifiedNs",
        "rows",
        "sha256",
    }:
        raise HashSkinIndexError("compact hash index has invalid source identity")
    if not isinstance(raw_records, list) or not raw_records:
        raise HashSkinIndexError("compact hash index has no records")
    records: list[HashSkinRecord] = []
    for raw_record in raw_records:
        if (
            not isinstance(raw_record, list)
            or len(raw_record) != 3
            or not isinstance(raw_record[0], str)
            or isinstance(raw_record[1], bool)
            or not isinstance(raw_record[1], int)
            or not isinstance(raw_record[2], str)
            or _HASH_RE.fullmatch(raw_record[2]) is None
        ):
            raise HashSkinIndexError("compact hash index has an invalid record")
        records.append(
            HashSkinRecord(
                raw_record[0],
                raw_record[1],
                int(raw_record[2], 16),
            )
        )
    if payload.get("recordCount") != len(records):
        raise HashSkinIndexError("compact hash index record count differs")
    try:
        return HashSkinIndex(
            source_size=source["size"],
            source_modified_ns=source["modifiedNs"],
            source_row_count=source["rows"],
            source_sha256=source["sha256"],
            relevant_sha256=payload["relevantSha256"],
            records=tuple(records),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HashSkinIndexError("compact hash index identity is invalid") from exc


def _records_sha256(records: tuple[HashSkinRecord, ...]) -> str:
    canonical = json.dumps(
        [
            [record.unit, record.skin_number, f"{record.path_hash:016x}"]
            for record in records
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HashSkinIndexError(f"duplicate compact index key: {key!r}")
        result[key] = value
    return result


def _require_regular_source(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise HashSkinIndexError(
            f"hash dictionary must be a regular file: {path}"
        )


def _require_safe_cache(path: Path) -> None:
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise HashSkinIndexError(
            f"compact hash index must be a regular file: {path}"
        )
    if path.parent.exists() and (
        path.parent.is_symlink() or not path.parent.is_dir()
    ):
        raise HashSkinIndexError(
            f"compact hash index parent must be a directory: {path.parent}"
        )


def _validate_expected_size(actual: int, expected: int | None) -> None:
    if expected is not None and actual != expected:
        raise HashSkinIndexError(
            f"hash dictionary size differs from updater result: "
            f"{actual} != {expected}"
        )
