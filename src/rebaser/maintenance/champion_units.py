"""Audit or atomically update the champion-unit candidate registry.

The command is offline. It derives the prime official champion roster from
local LCU data, streams ``hashes.game.txt`` while computing its SHA-256,
and intersects standard skin BIN paths with each official champion WAD. It
never deletes candidates merely because they were not observed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .. import app as script
from ..registry_write import (
    RegistryWriteError,
    commit_atomic_json,
    exclusive_registry_lock,
    prepare_atomic_json,
)
from ..champion_layout import (
    SKIN_PATH_RE,
    CandidateRegistry,
    CandidateRegistryEntry,
    CandidateRegistryError,
    ChampionDataError,
    ChampionIdentity,
    ChampionIdentityError,
    ChampionLayoutError,
    build_champion_layout,
    candidate_registry_entries_document,
    champion_skin_path,
    find_champion_identity,
    load_candidate_registry,
    parse_official_champion_identities,
    validate_identity_wad,
)
from ..wad_access import (
    PreparedChampionWad,
    UnsupportedWadFeature,
    UnsupportedWadVersion,
    WadChangedDuringRead,
    WadError,
    WadFileIdentity,
    WadReadLimitExceeded,
    normalize_wad_path,
    parse_wad_index,
    wad_path_hash,
)


from ..paths import DATA_ROOT, PROJECT_ROOT


SCRIPT_DIR = PROJECT_ROOT
DEFAULT_HASHES_GAME = SCRIPT_DIR / "cslol-tools" / "hashes.game.txt"
DEFAULT_REGISTRY = DATA_ROOT / "champion-units.generated.json"
DEFAULT_CONFIG = SCRIPT_DIR / "config.json"
IMPLEMENTATION_SOURCES = (
    Path(__file__).resolve(),
    PROJECT_ROOT / "src" / "rebaser" / "champion_layout.py",
    PROJECT_ROOT / "src" / "rebaser" / "app.py",
    PROJECT_ROOT / "src" / "rebaser" / "wad_access.py",
    PROJECT_ROOT / "src" / "rebaser" / "registry_write.py",
)
REPORT_SCHEMA_VERSION = 1
HASH_RE = re.compile(r"[0-9a-fA-F]{16}\Z")


class UnitUpdaterError(RuntimeError):
    """The updater cannot produce a trustworthy check result."""


class HashSourceError(UnitUpdaterError):
    """The hash source is malformed, unstable, or internally inconsistent."""


class MissingOfficialWad(UnitUpdaterError):
    """The exact official champion WAD is absent."""


class AmbiguousOfficialWad(UnitUpdaterError):
    """More than one filesystem entry matches the official WAD identity."""


@dataclass(frozen=True)
class HashPathRecord:
    path_hash: int
    path: str
    unit: str
    skin_number: int


@dataclass(frozen=True)
class HashSourceScan:
    identity: Mapping[str, Any]
    records: Mapping[int, HashPathRecord]
    lines: int
    relevant_lines: int
    duplicate_lines: int
    ambiguous_hashes: tuple[Mapping[str, Any], ...]


def _stat_key(stat_result: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
    )


def stable_file_identity(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    before = resolved.stat()
    digest = hashlib.sha256()
    size = 0
    with resolved.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if _stat_key(opened) != _stat_key(before):
            raise UnitUpdaterError(f"file changed before hashing: {resolved}")
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
        ending_handle = os.fstat(handle.fileno())
    after = resolved.stat()
    if (
        _stat_key(opened) != _stat_key(ending_handle)
        or _stat_key(before) != _stat_key(after)
        or size != after.st_size
    ):
        raise UnitUpdaterError(f"file changed while hashing: {resolved}")
    return {
        "path": str(resolved),
        "size": after.st_size,
        "modifiedNs": after.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


def read_stable_file_bytes(
    path: Path,
) -> tuple[bytes, dict[str, Any]]:
    resolved = path.resolve(strict=True)
    before = resolved.stat()
    with resolved.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if _stat_key(opened) != _stat_key(before):
            raise UnitUpdaterError(
                f"file changed before reading: {resolved}"
            )
        raw = handle.read()
        ending_handle = os.fstat(handle.fileno())
    after = resolved.stat()
    if (
        _stat_key(opened) != _stat_key(ending_handle)
        or _stat_key(before) != _stat_key(after)
        or len(raw) != after.st_size
    ):
        raise UnitUpdaterError(f"file changed while reading: {resolved}")
    return raw, {
        "path": str(resolved),
        "size": len(raw),
        "modifiedNs": after.st_mtime_ns,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def read_optional_stable_file_bytes(
    path: Path,
) -> tuple[bytes | None, dict[str, Any]]:
    try:
        return read_stable_file_bytes(path)
    except FileNotFoundError:
        resolved = path.resolve()
        if path.exists():
            raise UnitUpdaterError(
                f"optional file changed while checking existence: {resolved}"
            )
        return None, {
            "path": str(resolved),
            "exists": False,
        }


def stable_optional_file_identity(path: Path) -> dict[str, Any]:
    try:
        return stable_file_identity(path)
    except FileNotFoundError:
        resolved = path.resolve()
        if path.exists():
            raise UnitUpdaterError(
                f"optional file changed while checking existence: {resolved}"
            )
        return {
            "path": str(resolved),
            "exists": False,
        }


def scan_hash_source(path: Path) -> HashSourceScan:
    resolved = path.resolve(strict=True)
    before = resolved.stat()
    digest = hashlib.sha256()
    records: dict[int, HashPathRecord] = {}
    ambiguous: dict[int, set[str]] = {}
    line_count = 0
    relevant_count = 0
    duplicate_count = 0

    with resolved.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if _stat_key(opened) != _stat_key(before):
            raise HashSourceError(
                f"hash source changed before scanning: {resolved}"
            )
        for raw_line in handle:
            line_count += 1
            digest.update(raw_line)
            try:
                line = raw_line.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError as exc:
                raise HashSourceError(
                    f"hash source is not UTF-8 at line {line_count}"
                ) from exc
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                continue
            declared_text, raw_path = parts
            quick_path = raw_path.replace("\\", "/").casefold()
            if (
                not quick_path.startswith("data/characters/")
                or "/skins/skin" not in quick_path
                or not quick_path.endswith(".bin")
            ):
                continue
            try:
                normalized = normalize_wad_path(raw_path)
            except ValueError:
                continue
            match = SKIN_PATH_RE.fullmatch(normalized)
            if match is None:
                continue
            relevant_count += 1
            if HASH_RE.fullmatch(declared_text) is None:
                raise HashSourceError(
                    f"invalid path hash for relevant line {line_count}: "
                    f"{declared_text!r}"
                )
            declared = int(declared_text, 16)
            computed = wad_path_hash(normalized)
            if declared != computed:
                raise HashSourceError(
                    f"XXH64 mismatch for relevant path {normalized}: "
                    f"{declared:016x} != {computed:016x}"
                )
            record = HashPathRecord(
                path_hash=declared,
                path=normalized,
                unit=match.group("unit"),
                skin_number=int(match.group("skin")),
            )
            previous = records.get(declared)
            if previous is None:
                records[declared] = record
            elif previous.path == normalized:
                duplicate_count += 1
            else:
                ambiguous.setdefault(declared, {previous.path}).add(
                    normalized
                )

        ending_handle = os.fstat(handle.fileno())
    after = resolved.stat()
    if (
        _stat_key(opened) != _stat_key(ending_handle)
        or _stat_key(before) != _stat_key(after)
    ):
        raise HashSourceError(
            f"hash source changed while scanning: {resolved}"
        )

    scan_identity = {
        "path": str(resolved),
        "size": after.st_size,
        "modifiedNs": after.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }
    try:
        verified_identity = stable_file_identity(resolved)
    except (UnitUpdaterError, OSError) as exc:
        raise HashSourceError(str(exc)) from exc
    if verified_identity != scan_identity:
        raise HashSourceError(
            f"hash source changed after scanning: {resolved}"
        )

    ambiguous_records = tuple(
        {
            "pathHash": f"{path_hash:016x}",
            "paths": sorted(paths),
        }
        for path_hash, paths in sorted(ambiguous.items())
    )
    return HashSourceScan(
        identity=scan_identity,
        records=records,
        lines=line_count,
        relevant_lines=relevant_count,
        duplicate_lines=duplicate_count,
        ambiguous_hashes=ambiguous_records,
    )


def resolve_champions_dir(
    league_path: Path | None,
    config_path: Path,
) -> Path:
    if league_path is None:
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise UnitUpdaterError(
                f"cannot read local League path from {config_path}: {exc}"
            ) from exc
        raw_path = config.get("lol_path") if isinstance(config, dict) else None
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise UnitUpdaterError(
                f"{config_path} has no non-empty lol_path"
            )
        league_path = Path(raw_path)
    champions_dir = (
        league_path.resolve()
        / "Game"
        / "DATA"
        / "FINAL"
        / "Champions"
    )
    if not champions_dir.is_dir():
        raise UnitUpdaterError(
            f"League Champions directory not found: {champions_dir}"
        )
    return champions_dir


def load_official_identities(
    champions_dir: Path,
    generation: tuple[script.LcuWadGenerationEntry, ...],
) -> tuple[
    tuple[ChampionIdentity, ...],
    tuple[script.LcuJsonSourceIdentity, script.LcuJsonSourceIdentity],
]:
    try:
        summary_record = script.load_lcu_json_with_identity(
            champions_dir,
            script.LCU_CHAMPION_SUMMARY_PATH,
            expected_generation=generation,
        )
        skins_record = script.load_lcu_json_with_identity(
            champions_dir,
            script.LCU_SKINS_PATH,
            expected_generation=generation,
        )
        identities = parse_official_champion_identities(
            summary_record.data,
            skins_record.data,
        )
        return identities, (
            summary_record.source,
            skins_record.source,
        )
    except (
        ChampionIdentityError,
        script.LcuDataError,
        WadError,
        OSError,
    ) as exc:
        raise UnitUpdaterError(str(exc)) from exc


def locate_official_wad(
    identity: ChampionIdentity,
    champions_dir: Path,
) -> Path:
    expected = f"{identity.wad_base}.wad.client".casefold()
    matches = [
        path
        for path in champions_dir.iterdir()
        if path.is_file() and path.name.casefold() == expected
    ]
    if not matches:
        raise MissingOfficialWad(
            f"official champion id {identity.champion_id} expects "
            f"{identity.wad_base}.wad.client; found 0"
        )
    if len(matches) > 1:
        raise AmbiguousOfficialWad(
            f"official champion id {identity.champion_id} expects exactly "
            f"one {identity.wad_base}.wad.client; found {len(matches)}"
        )
    return matches[0]


def official_target_skin_numbers(
    identity: ChampionIdentity,
    champions_dir: Path,
    generation: tuple[script.LcuWadGenerationEntry, ...],
) -> tuple[tuple[int, ...], script.LcuJsonSourceIdentity]:
    try:
        rel_path = (
            "plugins/rcp-be-lol-game-data/global/default/v1/champions/"
            f"{identity.champion_id}.json"
        )
        record = script.load_lcu_json_with_identity(
            champions_dir,
            rel_path,
            expected_generation=generation,
        )
        catalog = script.parse_official_name_catalog(
            identity.champion_id,
            record.data,
        )
    except (
        ChampionIdentityError,
        script.LcuDataError,
        WadError,
        OSError,
    ) as exc:
        raise UnitUpdaterError(str(exc)) from exc
    if catalog is None:
        raise UnitUpdaterError(
            f"malformed local LCU champion data for id "
            f"{identity.champion_id}"
        )
    out_of_schema = sorted(
        skin_number
        for skin_number in catalog.names_by_skin_number
        if skin_number > 999
    )
    if out_of_schema:
        raise UnitUpdaterError(
            f"official champion id {identity.champion_id} has skin numbers "
            f"outside the fullSkinId 0..999 schema: {out_of_schema}"
        )
    numbers = tuple(
        sorted(
            skin_number
            for skin_number in catalog.names_by_skin_number
            if 1 <= skin_number <= 999
        )
    )
    if not numbers:
        raise UnitUpdaterError(
            f"official champion id {identity.champion_id} has no target skins"
        )
    return numbers, record.source


def probe_unit_paths(
    prepared: PreparedChampionWad,
    units: Iterable[str],
    skin_numbers: tuple[int, ...],
) -> dict[str, set[int]]:
    selected = tuple(sorted(set(units)))
    if not selected:
        return {}
    paths = [
        champion_skin_path(unit, skin_number)
        for unit in selected
        for skin_number in (0, *skin_numbers)
    ]
    inspected = prepared.inspect_paths(paths)
    return {
        unit: {
            skin_number
            for skin_number in (0, *skin_numbers)
            if inspected[champion_skin_path(unit, skin_number)] is not None
        }
        for unit in selected
    }


def hash_units_for_wad(
    prepared: PreparedChampionWad,
    scan: HashSourceScan,
) -> dict[str, set[int]]:
    units: dict[str, set[int]] = {}
    for path_hash, record in scan.records.items():
        if path_hash not in prepared.chunks_by_hash:
            continue
        units.setdefault(record.unit, set()).add(record.skin_number)
    return units


def _empty_categories() -> dict[str, list[dict[str, Any]]]:
    return {
        "added": [],
        "seen": [],
        "not_seen": [],
        "base_only": [],
        "target_only": [],
        "ambiguous_hash": [],
        "missing_wad": [],
        "unsupported_wad": [],
        "incomplete_source": [],
    }


def _category_item(
    identity: ChampionIdentity,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "championId": identity.champion_id,
        "champion": identity.display_name,
        **fields,
    }


def _registry_or_empty(
    path: Path,
    identities: tuple[ChampionIdentity, ...],
    raw_bytes: bytes | None,
) -> CandidateRegistry:
    if raw_bytes is None:
        return CandidateRegistry(entries={})
    return load_candidate_registry(
        path,
        identities,
        raw_bytes=raw_bytes,
    )


def _canonical_digest(document: Any) -> str:
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def retain_existing_candidates(
    existing_units: Iterable[str],
    discovered_units: Iterable[str],
) -> tuple[str, ...]:
    """Return a canonical union; absence from one scan never deletes a unit."""

    return tuple(sorted(set(existing_units) | set(discovered_units)))


def _layout_categories(
    identity: ChampionIdentity,
    layout: Any,
    categories: dict[str, list[dict[str, Any]]],
) -> dict[str, int]:
    counts = {
        "skins": len(layout.skins),
        "paired": 0,
        "baseOnly": 0,
        "targetOnly": 0,
        "absent": 0,
    }
    for skin in layout.skins:
        counts["paired"] += len(skin.paired)
        counts["baseOnly"] += len(skin.base_only)
        counts["targetOnly"] += len(skin.target_only)
        counts["absent"] += len(skin.absent_candidates)
        categories["base_only"].extend(
            _category_item(
                identity,
                skinNumber=skin.skin_number,
                unit=state.unit,
            )
            for state in skin.base_only
        )
        categories["target_only"].extend(
            _category_item(
                identity,
                skinNumber=skin.skin_number,
                unit=state.unit,
            )
            for state in skin.target_only
        )
    return counts


@dataclass(frozen=True)
class ChampionAuditAttempt:
    entry: CandidateRegistryEntry
    report: Mapping[str, Any]
    categories: Mapping[str, tuple[dict[str, Any], ...]]
    wad_path: Path
    wad_identity: WadFileIdentity
    wad_version: str
    toc_digest: str
    lcu_source: script.LcuJsonSourceIdentity


def _merge_categories(
    destination: dict[str, list[dict[str, Any]]],
    source: Mapping[str, Iterable[dict[str, Any]]],
) -> None:
    for name, records in source.items():
        destination[name].extend(records)


def wad_error_category(error: WadError) -> str:
    if isinstance(
        error,
        (
            UnsupportedWadVersion,
            UnsupportedWadFeature,
            WadReadLimitExceeded,
        ),
    ):
        return "unsupported_wad"
    return "incomplete_source"


def _wad_record(attempt: ChampionAuditAttempt) -> dict[str, Any]:
    identity = attempt.wad_identity
    return {
        "championId": attempt.entry.champion_id,
        "path": str(attempt.wad_path.resolve()),
        "device": identity.device,
        "inode": identity.inode,
        "size": identity.size,
        "modifiedNs": identity.mtime_ns,
        "version": attempt.wad_version,
        "tocDigest": attempt.toc_digest,
    }


def _verify_pinned_champion_wad(
    wad_path: Path,
    expected_identity: WadFileIdentity,
    expected_toc_digest: str,
) -> None:
    index = parse_wad_index(wad_path)
    if (
        index.file_identity != expected_identity
        or index.toc_digest != expected_toc_digest
    ):
        raise WadChangedDuringRead(
            wad_path,
            expected_identity,
            index.file_identity,
        )


def _audit_champion_once(
    *,
    identity: ChampionIdentity,
    wad_path: Path,
    champions_dir: Path,
    lcu_generation: tuple[script.LcuWadGenerationEntry, ...],
    current_registry: CandidateRegistry,
    hash_scan: HashSourceScan,
) -> ChampionAuditAttempt:
    mounted = PreparedChampionWad(wad_path, identity=identity)
    pinned_identity = mounted.file_identity
    pinned_toc_digest = mounted.toc_digest
    pinned_version = str(mounted.version)
    validate_identity_wad(identity, mounted)
    skin_numbers, lcu_source = official_target_skin_numbers(
        identity,
        champions_dir,
        lcu_generation,
    )

    existing = current_registry.entries.get(identity.champion_id)
    existing_units = (
        set()
        if existing is None
        else set(existing.auxiliary_units)
    )
    probed = probe_unit_paths(
        mounted,
        existing_units,
        skin_numbers,
    )
    hash_units = hash_units_for_wad(mounted, hash_scan)

    local_categories = _empty_categories()
    source_candidates = set(hash_units)
    source_candidates.discard(identity.main_unit)
    added = sorted(source_candidates - existing_units)
    seen = sorted(
        unit
        for unit in existing_units
        if probed.get(unit) or unit in hash_units
    )
    not_seen = sorted(existing_units - set(seen))
    local_categories["added"].extend(
        _category_item(identity, unit=unit)
        for unit in added
    )
    local_categories["seen"].extend(
        _category_item(identity, unit=unit)
        for unit in seen
    )
    local_categories["not_seen"].extend(
        _category_item(identity, unit=unit)
        for unit in not_seen
    )

    proposed_auxiliary = retain_existing_candidates(
        existing_units,
        added,
    )
    entry = CandidateRegistryEntry(
        champion_id=identity.champion_id,
        alias=identity.alias,
        wad_base=identity.wad_base,
        main_unit=identity.main_unit,
        auxiliary_units=proposed_auxiliary,
    )
    layout = build_champion_layout(
        identity,
        mounted,
        skin_numbers,
        CandidateRegistry(entries={identity.champion_id: entry}),
    )
    layout_counts = _layout_categories(
        identity,
        layout,
        local_categories,
    )
    if (
        mounted.file_identity != pinned_identity
        or mounted.toc_digest != pinned_toc_digest
    ):
        raise WadChangedDuringRead(
            wad_path,
            pinned_identity,
            mounted.file_identity,
        )
    _verify_pinned_champion_wad(
        wad_path,
        pinned_identity,
        pinned_toc_digest,
    )

    champion_report = {
        "championId": identity.champion_id,
        "displayName": identity.display_name,
        "alias": identity.alias,
        "wadBase": identity.wad_base,
        "mainUnit": identity.main_unit,
        "skinCount": len(skin_numbers),
        "existingAuxiliaryUnits": sorted(existing_units),
        "hashSourceUnits": sorted(hash_units),
        "added": added,
        "seen": seen,
        "notSeen": not_seen,
        "retainedNotSeen": not_seen,
        "proposedAuxiliaryUnits": list(proposed_auxiliary),
        "layout": layout_counts,
    }
    return ChampionAuditAttempt(
        entry=entry,
        report=champion_report,
        categories={
            name: tuple(records)
            for name, records in local_categories.items()
        },
        wad_path=wad_path,
        wad_identity=pinned_identity,
        wad_version=pinned_version,
        toc_digest=pinned_toc_digest,
        lcu_source=lcu_source,
    )


def _audit_champion_with_retry(**kwargs: Any) -> ChampionAuditAttempt:
    last_change: WadChangedDuringRead | None = None
    for attempt in range(2):
        try:
            return _audit_champion_once(**kwargs)
        except WadChangedDuringRead as exc:
            last_change = exc
            if attempt:
                break
    assert last_change is not None
    raise last_change


def _implementation_source_identities() -> list[dict[str, Any]]:
    return [
        stable_file_identity(path)
        for path in IMPLEMENTATION_SOURCES
    ]


def _git_head() -> str | None:
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={SCRIPT_DIR.as_posix()}",
                "rev-parse",
                "HEAD",
            ],
            cwd=SCRIPT_DIR,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if re.fullmatch(r"[0-9a-fA-F]{40}", value) else None


def build_audit(
    *,
    champions_dir: Path,
    hashes_game: Path,
    registry_path: Path,
    champion_query: str | None,
) -> tuple[dict[str, Any], dict[str, Any], bool, bool]:
    categories = _empty_categories()
    implementation_sources = _implementation_source_identities()
    try:
        lcu_generation = script.capture_lcu_wad_generation(champions_dir)
        identities, roster_sources = load_official_identities(
            champions_dir,
            lcu_generation,
        )
    except (
        script.LcuDataError,
        WadError,
        UnitUpdaterError,
        OSError,
    ) as exc:
        raise UnitUpdaterError(
            f"cannot load pinned local LCU identity inputs: {exc}"
        ) from exc
    if champion_query is None:
        selected = identities
        scope = "all"
    else:
        try:
            selected = (
                find_champion_identity(identities, champion_query),
            )
        except ChampionIdentityError as exc:
            raise UnitUpdaterError(str(exc)) from exc
        scope = "champion"

    registry_raw, registry_identity = read_optional_stable_file_bytes(
        registry_path
    )
    current_registry = _registry_or_empty(
        registry_path,
        identities,
        registry_raw,
    )
    try:
        hash_scan = scan_hash_source(hashes_game)
    except (HashSourceError, OSError) as exc:
        categories["incomplete_source"].append(
            {
                "source": "hashes.game",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        hash_scan = None

    if hash_scan is not None:
        categories["ambiguous_hash"].extend(
            dict(record)
            for record in hash_scan.ambiguous_hashes
        )

    proposed_entries = dict(current_registry.entries)
    champion_reports: list[dict[str, Any]] = []
    attempts: list[ChampionAuditAttempt] = []
    lcu_sources = {
        source.normalized_path: source
        for source in roster_sources
    }
    for identity in selected:
        if hash_scan is None:
            continue
        try:
            wad_path = locate_official_wad(identity, champions_dir)
        except MissingOfficialWad as exc:
            categories["missing_wad"].append(
                _category_item(identity, error=str(exc))
            )
            continue
        except (AmbiguousOfficialWad, OSError) as exc:
            categories["incomplete_source"].append(
                _category_item(
                    identity,
                    source="official WAD selection",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        try:
            attempt = _audit_champion_with_retry(
                identity=identity,
                wad_path=wad_path,
                champions_dir=champions_dir,
                lcu_generation=lcu_generation,
                current_registry=current_registry,
                hash_scan=hash_scan,
            )
        except WadError as exc:
            category = wad_error_category(exc)
            categories[category].append(
                _category_item(
                    identity,
                    wad=wad_path.name,
                    source="champion audit",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        except (
            ChampionDataError,
            ChampionLayoutError,
            UnitUpdaterError,
            OSError,
        ) as exc:
            categories["incomplete_source"].append(
                _category_item(
                    identity,
                    wad=wad_path.name,
                    source="champion audit",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        attempts.append(attempt)
        proposed_entries[identity.champion_id] = attempt.entry
        champion_reports.append(dict(attempt.report))
        _merge_categories(categories, attempt.categories)
        existing_lcu_source = lcu_sources.get(
            attempt.lcu_source.normalized_path
        )
        if (
            existing_lcu_source is not None
            and existing_lcu_source != attempt.lcu_source
        ):
            categories["incomplete_source"].append(
                _category_item(
                    identity,
                    source="LCU JSON identity",
                    error="one normalized path resolved to multiple inputs",
                )
            )
        else:
            lcu_sources[
                attempt.lcu_source.normalized_path
            ] = attempt.lcu_source

    proposed_document = candidate_registry_entries_document(
        proposed_entries.values()
    )
    current_document = candidate_registry_entries_document(
        current_registry.entries.values()
    )
    changed = proposed_document != current_document

    try:
        ending_lcu_generation = script.capture_lcu_wad_generation(
            champions_dir
        )
        if ending_lcu_generation != lcu_generation:
            raise UnitUpdaterError(
                "local LCU WAD generation changed during the audit"
            )
    except (script.LcuDataError, UnitUpdaterError, WadError, OSError) as exc:
        categories["incomplete_source"].append(
            {
                "source": "LCU WAD snapshot",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )

    for attempt in attempts:
        try:
            _verify_pinned_champion_wad(
                attempt.wad_path,
                attempt.wad_identity,
                attempt.toc_digest,
            )
        except (WadError, OSError) as exc:
            categories["incomplete_source"].append(
                {
                    "championId": attempt.entry.champion_id,
                    "source": "final champion WAD identity",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    final_identity_checks = (
        (
            "candidate registry",
            stable_optional_file_identity,
            registry_path,
            registry_identity,
        ),
    )
    if hash_scan is not None:
        final_identity_checks += (
            (
                "hashes.game",
                stable_file_identity,
                hashes_game,
                dict(hash_scan.identity),
            ),
        )
    for source_name, capture, path, expected in final_identity_checks:
        try:
            actual = capture(path)
            if actual != expected:
                raise UnitUpdaterError(
                    f"{source_name} changed during the audit"
                )
        except (UnitUpdaterError, OSError) as exc:
            categories["incomplete_source"].append(
                {
                    "source": source_name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    try:
        ending_implementation_sources = _implementation_source_identities()
        if ending_implementation_sources != implementation_sources:
            raise UnitUpdaterError(
                "audit implementation sources changed during execution"
            )
    except (UnitUpdaterError, OSError) as exc:
        categories["incomplete_source"].append(
            {
                "source": "implementation",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )

    blocking = any(
        categories[name]
        for name in (
            "ambiguous_hash",
            "missing_wad",
            "unsupported_wad",
            "incomplete_source",
        )
    )
    report = {
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "status": "failed" if blocking else "passed",
        "mode": "check",
        "scope": {
            "kind": scope,
            "query": champion_query,
            "selectedChampionIds": [
                identity.champion_id
                for identity in selected
            ],
        },
        "officialRoster": {
            "source": "local LCU champion-summary + valid base skins",
            "championCount": len(identities),
            "championIds": [
                identity.champion_id
                for identity in identities
            ],
            "countIsDynamic": True,
        },
        "inputs": {
            "championsDirectory": str(champions_dir.resolve()),
            "lcuWads": script.lcu_wad_generation_document(
                lcu_generation
            ),
            "lcuJson": [
                script.lcu_json_source_document(source)
                for _, source in sorted(lcu_sources.items())
            ],
            "hashSource": (
                None
                if hash_scan is None
                else {
                    **hash_scan.identity,
                    "lines": hash_scan.lines,
                    "relevantLines": hash_scan.relevant_lines,
                    "uniqueRelevantPaths": len(hash_scan.records),
                    "duplicateRelevantLines": hash_scan.duplicate_lines,
                    "structurallyValid": not bool(
                        hash_scan.ambiguous_hashes
                    ),
                    "coverage": "unknown_or_stale",
                    "coverageReason": (
                        "a structurally valid path dictionary is not proof "
                        "that every current unit name is resolved"
                    ),
                }
            ),
            "candidateRegistry": registry_identity,
            "championWads": [
                _wad_record(attempt)
                for attempt in attempts
            ],
            "implementation": {
                "gitHead": _git_head(),
                "sources": implementation_sources,
            },
        },
        "categories": categories,
        "champions": champion_reports,
        "changes": {
            "detected": changed,
            "currentChampionEntries": len(current_registry.entries),
            "proposedChampionEntries": len(proposed_entries),
            "proposedRegistrySha256": _canonical_digest(
                proposed_document
            ),
            "automaticDeletes": 0,
        },
        "deepScan": {
            "status": "not_requested",
            "note": (
                "best-effort deep scanning remains optional and is not "
                "required for this deterministic check"
            ),
        },
    }
    return report, proposed_document, changed, blocking


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Offline audit/update of champion unit candidates from local LCU "
            "data, champion WAD TOCs, and hashes.game."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="report differences without modifying the registry (default)",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="atomically update the registry after a complete stable audit",
    )
    parser.add_argument(
        "--champion",
        help="limit validation/update to one official champion",
    )
    parser.add_argument(
        "--league-path",
        type=Path,
        help="League of Legends root; otherwise read config.json",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="read-only config containing lol_path",
    )
    parser.add_argument(
        "--hashes-game",
        type=Path,
        default=DEFAULT_HASHES_GAME,
        help="local hashes.game.txt source",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY,
        help="generated candidate registry",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="write the complete audit report as JSON",
    )
    args = parser.parse_args(argv)
    if not args.write:
        args.check = True
    return args


def _paths_alias(left: Path, right: Path) -> bool:
    if left.resolve() == right.resolve():
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(
            left,
            right,
        )
    except OSError:
        return False


def validate_report_destination(
    report_path: Path | None,
    protected_paths: Iterable[Path],
) -> None:
    if report_path is None:
        return
    for protected in protected_paths:
        if _paths_alias(report_path, protected):
            raise UnitUpdaterError(
                f"--report destination aliases a read-only audit input: "
                f"{protected.resolve()}"
            )


def protected_wad_paths(champions_dir: Path) -> tuple[Path, ...]:
    champion_wads = tuple(
        path
        for path in champions_dir.iterdir()
        if path.is_file()
        and path.name.casefold().endswith(".wad.client")
    )
    try:
        game_data_dir = (
            script.lol_root_from_champions_dir(champions_dir)
            / script.LCU_GAME_DATA_REL
        )
        lcu_wads = tuple(
            path
            for path in game_data_dir.glob("*.wad")
            if path.is_file()
        )
    except SystemExit as exc:
        raise UnitUpdaterError(str(exc)) from exc
    return champion_wads + lcu_wads


def failure_report(
    *,
    champion_query: str | None,
    error: BaseException,
    mode: str = "check",
) -> dict[str, Any]:
    categories = _empty_categories()
    categories["incomplete_source"].append(
        {
            "source": "audit input",
            "error": f"{type(error).__name__}: {error}",
        }
    )
    return {
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "status": "failed",
        "mode": mode,
        "scope": {
            "kind": "all" if champion_query is None else "champion",
            "query": champion_query,
            "selectedChampionIds": [],
        },
        "officialRoster": {
            "source": "local LCU champion-summary + valid base skins",
            "championCount": 0,
            "championIds": [],
            "countIsDynamic": True,
        },
        "inputs": {},
        "categories": categories,
        "champions": [],
        "changes": {
            "detected": False,
            "currentChampionEntries": 0,
            "proposedChampionEntries": 0,
            "proposedRegistrySha256": None,
            "automaticDeletes": 0,
        },
        "deepScan": {
            "status": "not_requested",
            "note": "audit failed before deterministic scanning completed",
        },
        "writeStatus": "failed" if mode == "write" else "not_requested",
    }


def write_report(path: Path, report: Mapping[str, Any]) -> bool:
    try:
        script.write_json_atomically(path, report)
    except OSError as exc:
        print(
            f"champion unit audit failed writing report: {exc}",
            file=sys.stderr,
        )
        return False
    return True


def write_registry_after_revalidation(
    *,
    args: argparse.Namespace,
    champions_dir: Path,
    initial_report: Mapping[str, Any],
    proposed: Mapping[str, Any],
) -> dict[str, Any]:
    """Commit one proposal only if a lock-held full audit is identical."""

    temp_path, temp_digest = prepare_atomic_json(
        args.registry,
        proposed,
    )
    try:
        with exclusive_registry_lock(args.registry):
            (
                final_report,
                final_proposed,
                final_changed,
                final_blocking,
            ) = build_audit(
                champions_dir=champions_dir,
                hashes_game=args.hashes_game,
                registry_path=args.registry,
                champion_query=args.champion,
            )
            if final_blocking:
                raise RegistryWriteError(
                    "lock-held audit became blocking; registry not written"
                )
            if not final_changed:
                raise RegistryWriteError(
                    "registry changed before commit; proposal is no longer "
                    "a visible update"
                )
            if final_proposed != proposed:
                raise RegistryWriteError(
                    "audit proposal changed before commit"
                )
            if final_report.get("inputs") != initial_report.get("inputs"):
                raise RegistryWriteError(
                    "an audit input changed before commit"
                )
            commit_atomic_json(
                temp_path,
                args.registry,
                temp_digest,
            )
    finally:
        temp_path.unlink(missing_ok=True)

    updated = dict(initial_report)
    updated["mode"] = "write"
    updated["writeStatus"] = "written"
    updated["postWriteRegistry"] = stable_file_identity(args.registry)
    return updated


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    static_inputs = (
        args.config,
        args.hashes_game,
        args.registry,
        *IMPLEMENTATION_SOURCES,
    )
    try:
        validate_report_destination(args.report, static_inputs)
    except UnitUpdaterError as exc:
        print(f"champion unit audit failed: {exc}", file=sys.stderr)
        return 2

    try:
        champions_dir = resolve_champions_dir(
            args.league_path,
            args.config,
        )
    except (UnitUpdaterError, OSError) as exc:
        print(f"champion unit audit failed: {exc}", file=sys.stderr)
        if args.report is not None:
            report = failure_report(
                champion_query=args.champion,
                error=exc,
                mode="write" if args.write else "check",
            )
            if not write_report(args.report, report):
                return 2
        return 2

    try:
        validate_report_destination(
            args.report,
            protected_wad_paths(champions_dir),
        )
    except (UnitUpdaterError, OSError) as exc:
        print(f"champion unit audit failed: {exc}", file=sys.stderr)
        return 2

    try:
        report, proposed, changed, blocking = build_audit(
            champions_dir=champions_dir,
            hashes_game=args.hashes_game,
            registry_path=args.registry,
            champion_query=args.champion,
        )
    except (
        CandidateRegistryError,
        ChampionDataError,
        ChampionIdentityError,
        UnitUpdaterError,
        WadError,
        script.LcuDataError,
        OSError,
    ) as exc:
        print(f"champion unit audit failed: {exc}", file=sys.stderr)
        if args.report is not None:
            report = failure_report(
                champion_query=args.champion,
                error=exc,
                mode="write" if args.write else "check",
            )
            if not write_report(args.report, report):
                return 2
        return 2

    report["mode"] = "write" if args.write else "check"
    if blocking:
        report["writeStatus"] = (
            "blocked" if args.write else "not_requested"
        )
    elif args.write and not changed:
        report["writeStatus"] = "no_change"
    elif args.write:
        try:
            report = write_registry_after_revalidation(
                args=args,
                champions_dir=champions_dir,
                initial_report=report,
                proposed=proposed,
            )
        except (
            CandidateRegistryError,
            ChampionDataError,
            ChampionIdentityError,
            RegistryWriteError,
            UnitUpdaterError,
            WadError,
            script.LcuDataError,
            OSError,
        ) as exc:
            report["status"] = "failed"
            report["writeStatus"] = "failed"
            report["writeError"] = f"{type(exc).__name__}: {exc}"
            print(
                f"champion unit update failed: {exc}",
                file=sys.stderr,
            )
            if (
                args.report is not None
                and not write_report(args.report, report)
            ):
                return 2
            return 2
    else:
        report["writeStatus"] = "not_requested"

    if args.report is not None and not write_report(args.report, report):
        return 2

    categories = report["categories"]
    print(
        "champion unit audit: "
        f"status={report['status']} "
        f"champions={len(report['champions'])} "
        f"added={len(categories['added'])} "
        f"not_seen={len(categories['not_seen'])} "
        f"base_only={len(categories['base_only'])} "
        f"target_only={len(categories['target_only'])} "
        f"changes={changed} "
        f"write={report['writeStatus']}"
    )
    if blocking:
        return 2
    if args.write:
        return 0
    return 1 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
