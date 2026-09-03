"""Audit PreparedChampionWad against a source Golden without legacy tools.

The formal Phase 1 run is intentionally read-only.  It binds every source
identity before creating a PreparedChampionWad, then performs exactly one
Prepared session and one read_many call per selected champion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from rebaser.wad_access import (  # noqa: E402
    PreparedChampionWad,
    UnsupportedWadFeature,
    WadError,
    WadFileIdentity,
    WadIndex,
    WadPathNotFound,
    normalize_wad_path,
    parse_wad_index,
    wad_path_hash,
)


SCHEMA_VERSION = 1
DEFAULT_POOL = REPO_ROOT / "benchmarks" / "pools" / "upgrade-v2.json"
DEFAULT_OUTPUT = (
    REPO_ROOT / ".cache" / "audits" / "prepared-audit.json"
)
DEFAULT_CONFIG = REPO_ROOT / "config.json"
DEFAULT_HASH_SOURCE = REPO_ROOT / "cslol-tools" / "hashes.game.txt"
DEFAULT_LEGACY_TOOL = REPO_ROOT / "cslol-tools" / "wad-extract.exe"
DEFAULT_LCU_HASHES = REPO_ROOT / "bin" / "hashes" / "hashes.lcu.txt"
LCU_GAME_DATA_REL = Path("Plugins") / "rcp-be-lol-game-data"
LCU_DATA_PREFIX = "plugins/rcp-be-lol-game-data/global/default/v1"
LCU_CHAMPION_SUMMARY_PATH = f"{LCU_DATA_PREFIX}/champion-summary.json"
LCU_SKINS_PATH = f"{LCU_DATA_PREFIX}/skins.json"
LCU_REGRESSION_IDS = (799, 800, 804, 805, 893, 904)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GAME_VERSION_RE = re.compile(r"^(?P<core>\d+\.\d+\.\d+)")
SKIN_PATH_RE = re.compile(
    r"^data/characters/(?P<unit>[a-z0-9_]+)/skins/skin(?P<skin>\d+)\.bin$",
    re.IGNORECASE,
)


FIXED_POOL_CONTRACTS: dict[str, dict[str, int]] = {
    "upgrade-v2": {
        "selectedChampions": 10,
        "comparableChampions": 9,
        "directOnlyChampions": 1,
        "comparableSkins": 402,
        "directOnlyDeclaredSkins": 9,
        "totalDeclaredSkins": 411,
        "skins": 402,
        "comparablePairReferences": 909,
        "directOnlyPairReferences": 9,
        "totalPairReferences": 918,
        "pairReferences": 909,
        "comparableUniqueBaseChunks": 20,
        "directOnlyBaseChunks": 1,
        "totalUniqueBaseChunks": 21,
        "uniqueBaseChunks": 20,
        "comparableLogicalPathReferences": 1818,
        "directOnlyLogicalPathReferences": 18,
        "totalLogicalPathReferences": 1836,
        "comparableUniqueChunks": 929,
        "directOnlyChunks": 10,
        "totalRequiredChunks": 939,
        "preparedSessions": 10,
        "wadIndexes": 10,
        "readManyCalls": 10,
        "physicalChunkReads": 939,
        "successfulChunkReads": 939,
        "failedChunkReads": 0,
        "compressionType0Reads": 0,
        "compressionType1Reads": 0,
        "compressionType3Reads": 939,
        "compressionOtherReads": 0,
        "shaComparisons": 929,
        "shaMismatches": 0,
        "missingRequiredPaths": 0,
        "unsupportedRequiredChunks": 0,
        "readFailures": 0,
        "lcuOfficialChampionIds": 173,
        "lcuOfficialComputedPaths": 175,
        "lcuOfficialWadHits": 175,
        "lcuOfficialWrongCompression": 0,
        "lcuOfficialNonZeroSubchunks": 0,
        "lcuOfficialDuplicateHits": 0,
        "lcuOfficialReadablePaths": 175,
        "lcuOfficialUnsupportedPaths": 0,
        "lcuOfficialReadFailures": 0,
        "lcuRegressionComputedPaths": 6,
        "lcuRegressionLegacyTableMissing": 6,
        "lcuRegressionLegacyTableMismatches": 0,
        "lcuRegressionWadHits": 6,
        "lcuRegressionWrongCompression": 0,
        "lcuRegressionNonZeroSubchunks": 0,
        "lcuRegressionDuplicateHits": 0,
        "lcuRegressionReadablePaths": 6,
        "lcuRegressionUnsupportedPaths": 0,
        "lcuRegressionReadFailures": 0,
        "lcuCombinedReadFailures": 0,
    }
}


class AuditFailure(ValueError):
    """A fail-closed source, identity, or audit-contract failure."""

    def __init__(self, phase: str, message: str) -> None:
        self.phase = phase
        super().__init__(message)


@dataclass(frozen=True)
class AuditInputs:
    source_golden: Path
    pool: Path = DEFAULT_POOL
    output: Path = DEFAULT_OUTPUT
    config: Path = DEFAULT_CONFIG
    hash_source: Path = DEFAULT_HASH_SOURCE
    legacy_tool: Path = DEFAULT_LEGACY_TOOL
    lcu_hashes: Path = DEFAULT_LCU_HASHES


@dataclass(frozen=True)
class ChampionPlan:
    champion_id: int
    champion: str
    wad_name: str
    main_unit: str
    source_mode: str
    required_paths: tuple[str, ...]
    expected_sha256: Mapping[str, str]
    skin_count: int
    pair_references: int
    unique_base_chunks: int
    logical_path_references: int
    expected_wad_identity: Mapping[str, object]


@dataclass(frozen=True)
class LcuWadSnapshot:
    wad_path: Path
    index: WadIndex
    identity: Mapping[str, object]


class PreparedObserver:
    """Count only Prepared WAD operations; LCU TOC scans are separate."""

    def __init__(self) -> None:
        self.indexes = 0
        self.physical_chunks = 0
        self.successful_chunks = 0
        self.failed_chunks = 0
        self.compression_reads: dict[int, int] = {}
        self.events: dict[str, int] = {}

    def __call__(self, event: str, /, **fields: object) -> None:
        self.events[event] = self.events.get(event, 0) + 1
        if event == "wad.index.complete":
            self.indexes += 1
        elif event == "wad.read.chunk_attempt":
            self.physical_chunks += 1
            compression_type = fields.get("compression_type")
            if isinstance(compression_type, int):
                self.compression_reads[compression_type] = (
                    self.compression_reads.get(compression_type, 0) + 1
                )
        elif event == "wad.read.chunk":
            self.successful_chunks += 1
        elif event == "wad.read.chunk_failure":
            self.failed_chunks += 1


def read_stable_bytes(path: Path) -> tuple[bytes, dict[str, object]]:
    resolved = path.resolve(strict=True)
    before = resolved.stat()
    data = resolved.read_bytes()
    after = resolved.stat()
    if _stat_key(before) != _stat_key(after):
        raise AuditFailure("identity", f"file changed while reading: {resolved}")
    return data, _file_identity(resolved, after, hashlib.sha256(data).hexdigest())


def read_stable_json(path: Path) -> tuple[Any, dict[str, object]]:
    raw, identity = read_stable_bytes(path)
    try:
        return json.loads(raw.decode("utf-8")), identity
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditFailure("source-contract", f"invalid JSON {path}: {exc}") from exc


def stable_file_identity(path: Path) -> dict[str, object]:
    resolved = path.resolve(strict=True)
    before = resolved.stat()
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    after = resolved.stat()
    if _stat_key(before) != _stat_key(after):
        raise AuditFailure("identity", f"file changed while hashing: {resolved}")
    return _file_identity(resolved, after, digest.hexdigest())


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


def comparable_game_version(version: str) -> str:
    core = version.split("+", 1)[0]
    parts = core.split(".")
    if len(parts) == 4 and all(part.isdecimal() for part in parts):
        return ".".join((parts[0], parts[1], parts[2] + parts[3]))
    match = GAME_VERSION_RE.match(core)
    if match is None:
        raise AuditFailure("game-identity", f"unrecognized game version: {version!r}")
    return match.group("core")


def expand_skin_set(champion: Mapping[str, Any]) -> tuple[int, ...]:
    skin_set = champion.get("skinSet")
    if not isinstance(skin_set, dict):
        raise AuditFailure("source-contract", "champion is missing skinSet")
    ranges = skin_set.get("ranges")
    excluded = skin_set.get("exclude")
    if not isinstance(ranges, list) or not isinstance(excluded, list):
        raise AuditFailure("source-contract", "champion has an invalid skinSet")
    excluded_set = _positive_int_set(excluded, "skinSet.exclude")
    skins: list[int] = []
    declared: set[int] = set()
    for item in ranges:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in item
            )
        ):
            raise AuditFailure("source-contract", f"invalid skin range: {item!r}")
        first, last = item
        if first <= 0 or last < first or last > 999:
            raise AuditFailure("source-contract", f"invalid skin range: {item!r}")
        for skin_number in range(first, last + 1):
            if skin_number in declared:
                raise AuditFailure(
                    "source-contract",
                    f"overlapping skin range at skin{skin_number}",
                )
            declared.add(skin_number)
            if skin_number not in excluded_set:
                skins.append(skin_number)
    if not excluded_set.issubset(declared):
        raise AuditFailure(
            "source-contract",
            f"skin exclusions outside ranges: {sorted(excluded_set - declared)}",
        )
    if champion.get("skinCount") != len(skins):
        raise AuditFailure(
            "source-contract",
            f"skinSet count {len(skins)} does not match skinCount",
        )
    return tuple(sorted(skins))


def build_champion_plans(
    pool: Mapping[str, Any],
    source_golden: Mapping[str, Any],
    selected_ids: set[int],
) -> list[ChampionPlan]:
    pool_champions = _champions_by_id(pool.get("champions"), "pool")
    source_champions = _champions_by_id(
        source_golden.get("champions"),
        "source Golden",
    )
    if set(pool_champions) != set(source_champions):
        raise AuditFailure(
            "source-contract",
            "pool and source Golden champion IDs do not match",
        )

    plans: list[ChampionPlan] = []
    for champion_id in pool_champions:
        if champion_id not in selected_ids:
            continue
        champion = pool_champions[champion_id]
        source = source_champions[champion_id]
        name = _required_string(champion, "query", "pool champion")
        if source.get("champion") != name:
            raise AuditFailure(
                "source-contract",
                f"champion {champion_id} name differs between pool and Golden",
            )
        wad_name = _required_string(champion, "wadName", name)
        main_unit = normalize_wad_path(
            _required_string(champion, "mainUnit", name)
        )
        if "/" in main_unit:
            raise AuditFailure("source-contract", f"{name}: invalid mainUnit")
        skins = expand_skin_set(champion)
        expectation = champion.get("legacyExpectation")
        status = source.get("status")
        if expectation == "success" and status == "success":
            plan = _build_comparable_plan(
                champion_id,
                name,
                wad_name,
                main_unit,
                skins,
                champion,
                source,
            )
        elif expectation == "unsupported" and status == "expected_unsupported":
            plan = _build_direct_only_plan(
                champion_id,
                name,
                wad_name,
                main_unit,
                skins,
                champion,
                source,
            )
        else:
            raise AuditFailure(
                "source-contract",
                f"{name}: legacy expectation {expectation!r} and Golden "
                f"status {status!r} are inconsistent",
            )
        plans.append(plan)
    return plans


def scan_hash_source(
    path: Path,
    comparable_paths: set[str],
    direct_only_paths: set[str],
) -> tuple[dict[str, object], dict[str, object]]:
    resolved = path.resolve(strict=True)
    before = resolved.stat()
    digest = hashlib.sha256()
    found: dict[str, int] = {}
    relevant = comparable_paths | direct_only_paths
    with resolved.open("rb") as handle:
        for raw_line in handle:
            digest.update(raw_line)
            try:
                line = raw_line.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError as exc:
                raise AuditFailure(
                    "hash-source",
                    f"hash source is not UTF-8 at byte {handle.tell()}",
                ) from exc
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                continue
            normalized = normalize_wad_path(parts[1])
            if normalized not in relevant:
                continue
            try:
                declared = int(parts[0], 16)
            except ValueError as exc:
                raise AuditFailure(
                    "hash-source",
                    f"invalid relevant hash line for {normalized}",
                ) from exc
            computed = wad_path_hash(normalized)
            if declared != computed:
                raise AuditFailure(
                    "hash-source",
                    f"XXH64 mismatch for relevant path {normalized}: "
                    f"{declared:016x} != {computed:016x}",
                )
            previous = found.get(normalized)
            if previous is not None and previous != declared:
                raise AuditFailure(
                    "hash-source",
                    f"conflicting hashes for relevant path {normalized}",
                )
            found[normalized] = declared
    after = resolved.stat()
    if _stat_key(before) != _stat_key(after):
        raise AuditFailure("identity", f"hash source changed: {resolved}")
    identity = _file_identity(resolved, after, digest.hexdigest())
    missing_comparable = sorted(comparable_paths - found.keys())
    if missing_comparable:
        raise AuditFailure(
            "hash-source",
            f"hash source is missing {len(missing_comparable)} comparable paths; "
            f"first={missing_comparable[0]}",
        )
    direct_found = sorted(direct_only_paths & found.keys())
    return identity, {
        "relevantComparablePaths": len(comparable_paths),
        "validatedComparableLines": len(comparable_paths),
        "missingComparablePaths": 0,
        "directOnlyPaths": len(direct_only_paths),
        "directOnlyPathsPresent": len(direct_found),
        "directOnlyPathsAbsent": len(direct_only_paths) - len(direct_found),
        "directOnlyPresentExamples": direct_found[:10],
        "algorithm": "XXH64",
        "seed": 0,
        "fullRelevantLineValidation": True,
    }


def audit_lcu_official_data(
    league_root: Path,
    legacy_hashes_path: Path,
) -> dict[str, object]:
    """Audit every official champion JSON and the fixed hash-regression subset.

    Discovery is deliberately two-stage.  The summary and skins index are
    located and read first; only then are champion paths computed from the
    locally installed summary.  The fixed six-path regression is folded into
    the second stage so overlapping paths are not read twice.
    """

    game_data_dir = league_root / LCU_GAME_DATA_REL
    snapshots = _snapshot_lcu_wads(game_data_dir)
    scanned = [
        {
            "wad": snapshot.wad_path.name,
            "version": str(snapshot.index.version),
            "tocDigest": snapshot.index.toc_digest,
            "size": snapshot.identity["size"],
            "sha256": snapshot.identity["sha256"],
        }
        for snapshot in snapshots
    ]

    discovery_paths = (LCU_CHAMPION_SUMMARY_PATH, LCU_SKINS_PATH)
    discovery, discovery_values = _audit_lcu_stage(
        snapshots,
        discovery_paths,
        stage="official-indexes",
    )
    _require_lcu_stage_passed(discovery)

    summary = discovery_values[normalize_wad_path(LCU_CHAMPION_SUMMARY_PATH)]
    skins = discovery_values[normalize_wad_path(LCU_SKINS_PATH)]
    if not isinstance(skins, dict):
        raise AuditFailure(
            "lcu-official-data",
            f"{LCU_SKINS_PATH} must be a JSON object",
        )
    champion_ids, excluded_summary = _parse_official_champion_ids(summary, skins)

    official_champion_paths = tuple(
        _lcu_champion_path(champion_id) for champion_id in champion_ids
    )
    regression_paths = tuple(
        _lcu_champion_path(champion_id) for champion_id in LCU_REGRESSION_IDS
    )
    all_official_paths = (*discovery_paths, *official_champion_paths)
    _reject_lcu_path_hash_collisions(all_official_paths)

    champion_stage_paths = tuple(
        dict.fromkeys((*official_champion_paths, *regression_paths))
    )
    champions, champion_values = _audit_lcu_stage(
        snapshots,
        champion_stage_paths,
        stage="official-champions-and-regressions",
    )

    discovery_records = {
        str(record["path"]): record for record in discovery["paths"]  # type: ignore[index]
    }
    champion_records = {
        str(record["path"]): record for record in champions["paths"]  # type: ignore[index]
    }
    champion_schema_ids = dict.fromkeys((*champion_ids, *LCU_REGRESSION_IDS))
    for champion_id in champion_schema_ids:
        path = normalize_wad_path(_lcu_champion_path(champion_id))
        previous_read = champion_records[path]["read"]
        assert isinstance(previous_read, dict)
        if previous_read.get("status") != "passed":
            continue
        value = champion_values.get(path)
        payload_id = value.get("id") if isinstance(value, dict) else None
        if (
            not isinstance(value, dict)
            or isinstance(payload_id, bool)
            or not isinstance(payload_id, int)
            or payload_id != champion_id
        ):
            champion_records[path]["read"] = {
                **previous_read,
                "status": "invalid_schema",
                "error": (
                    "official champion JSON must be an object whose id matches "
                    f"the requested path ({champion_id})"
                ),
            }
    del champion_values
    official_records = [
        *(discovery_records[normalize_wad_path(path)] for path in discovery_paths),
        *(
            champion_records[normalize_wad_path(path)]
            for path in official_champion_paths
        ),
    ]
    official_metrics = _summarize_lcu_records(official_records)
    official = {
        "status": (
            "passed"
            if official_metrics["readFailures"] == 0
            and official_metrics["duplicateHits"] == 0
            and official_metrics["wrongCompression"] == 0
            and official_metrics["nonZeroSubchunks"] == 0
            else "failed"
        ),
        "algorithm": "XXH64",
        "seed": 0,
        "officialChampionIds": len(champion_ids),
        "championIds": list(champion_ids),
        "excludedSummaryEntries": excluded_summary,
        **official_metrics,
        "wadIndexesScanned": len(scanned),
        "scannedWads": scanned,
        "stages": {
            "officialIndexes": _lcu_stage_summary(discovery),
            "officialChampionsAndRegressions": {
                **_lcu_stage_summary(champions),
                **_summarize_lcu_records(champion_records.values()),
            },
        },
        "paths": official_records,
    }

    legacy_data, legacy_identity = read_stable_bytes(legacy_hashes_path)
    legacy_entries = _parse_legacy_lcu_hashes(legacy_data)
    regression_records: list[dict[str, object]] = []
    for champion_id, path in zip(LCU_REGRESSION_IDS, regression_paths):
        normalized = normalize_wad_path(path)
        computed = wad_path_hash(normalized)
        legacy_value = legacy_entries.get(normalized)
        regression_records.append(
            {
                "championId": champion_id,
                **champion_records[normalized],
                "legacyTable": {
                    "present": legacy_value is not None,
                    "declaredPathHash": (
                        None if legacy_value is None else f"{legacy_value:016x}"
                    ),
                    "matchesComputed": (
                        None if legacy_value is None else legacy_value == computed
                    ),
                },
            }
        )
    regression_metrics = _summarize_lcu_records(regression_records)
    legacy_missing = sum(
        not bool(record["legacyTable"]["present"])  # type: ignore[index]
        for record in regression_records
    )
    legacy_mismatches = sum(
        record["legacyTable"]["present"]  # type: ignore[index]
        and not record["legacyTable"]["matchesComputed"]  # type: ignore[index]
        for record in regression_records
    )
    regression = {
        "status": (
            "passed"
            if regression_metrics["readFailures"] == 0
            and regression_metrics["duplicateHits"] == 0
            and regression_metrics["wrongCompression"] == 0
            and regression_metrics["nonZeroSubchunks"] == 0
            and legacy_mismatches == 0
            else "failed"
        ),
        "algorithm": "XXH64",
        "seed": 0,
        "legacyHashTable": legacy_identity,
        "legacyTableMissing": legacy_missing,
        "legacyTableMismatches": legacy_mismatches,
        **regression_metrics,
        "paths": regression_records,
    }

    combined_records = [
        *discovery_records.values(),
        *champion_records.values(),
    ]
    return {
        "official": official,
        "regression": regression,
        "combinedReadFailures": _summarize_lcu_records(combined_records)[
            "readFailures"
        ],
    }


def _snapshot_lcu_wads(game_data_dir: Path) -> tuple[LcuWadSnapshot, ...]:
    wad_paths = sorted(game_data_dir.glob("*.wad"), key=lambda item: item.name.casefold())
    if not wad_paths:
        raise AuditFailure("lcu-official-data", f"no LCU WADs found in {game_data_dir}")
    snapshots: list[LcuWadSnapshot] = []
    for wad_path in wad_paths:
        identity = stable_file_identity(wad_path)
        index = parse_wad_index(wad_path)
        _require_prepared_identity(index.file_identity, identity)
        ending_identity = stable_file_identity(wad_path)
        _require_exact_file_identity(
            identity,
            ending_identity,
            f"{wad_path.name} after LCU TOC snapshot",
        )
        snapshots.append(
            LcuWadSnapshot(
                wad_path=wad_path,
                index=index,
                identity=identity,
            )
        )
    return tuple(snapshots)


def _audit_lcu_stage(
    snapshots: tuple[LcuWadSnapshot, ...],
    requested_paths: Iterable[str],
    *,
    stage: str,
) -> tuple[dict[str, object], dict[str, Any]]:
    paths = tuple(
        dict.fromkeys(normalize_wad_path(path) for path in requested_paths)
    )
    _reject_lcu_path_hash_collisions(paths)
    hashes = {path: wad_path_hash(path) for path in paths}
    snapshots_by_path = {snapshot.wad_path: snapshot for snapshot in snapshots}
    records_by_path: dict[str, dict[str, object]] = {}
    reads_by_wad: dict[Path, list[str]] = {}

    for path in paths:
        path_hash = hashes[path]
        hits: list[dict[str, object]] = []
        matching_wads: list[Path] = []
        for snapshot in snapshots:
            chunk = snapshot.index.chunks_by_hash.get(path_hash)
            if chunk is None:
                continue
            matching_wads.append(snapshot.wad_path)
            hits.append(
                {
                    "wad": snapshot.wad_path.name,
                    "compressionType": chunk.compression_type,
                    "subchunkCount": chunk.subchunk_count,
                    "subchunkIndex": chunk.subchunk_index,
                    "pathHash": f"{path_hash:016x}",
                }
            )

        if not hits:
            read = {
                "status": "missing",
                "error": "computed path hash was not found in any LCU WAD",
            }
        elif len(hits) > 1:
            read = {
                "status": "ambiguous",
                "error": "computed path hash exists in more than one LCU WAD",
            }
        elif hits[0]["compressionType"] != 3:
            read = {
                "status": "unsupported",
                "error": "official LCU JSON chunk is not compression type 3",
            }
        elif hits[0]["subchunkCount"] != 0 or hits[0]["subchunkIndex"] != 0:
            read = {
                "status": "unsupported",
                "error": "official LCU JSON chunk has non-zero subchunk metadata",
            }
        else:
            read = {"status": "pending"}
            reads_by_wad.setdefault(matching_wads[0], []).append(path)

        records_by_path[path] = {
            "path": path,
            "computedPathHash": f"{path_hash:016x}",
            "wadHits": hits,
            "read": read,
        }

    decoded: dict[str, Any] = {}
    prepared_sessions = 0
    read_many_calls = 0
    for wad_path, paths_for_wad in reads_by_wad.items():
        prepared_sessions += 1
        try:
            prepared = PreparedChampionWad(wad_path)
            snapshot = snapshots_by_path[wad_path]
            if (
                prepared.file_identity != snapshot.index.file_identity
                or prepared.toc_digest != snapshot.index.toc_digest
            ):
                raise AuditFailure(
                    "lcu-official-data",
                    f"{wad_path.name} changed after its LCU TOC snapshot",
                )
            read_many_calls += 1
            payloads = prepared.read_many(paths_for_wad)
            ending_identity = stable_file_identity(wad_path)
            _require_exact_file_identity(
                snapshot.identity,
                ending_identity,
                f"{wad_path.name} after LCU Prepared read",
            )
        except UnsupportedWadFeature as exc:
            for path in paths_for_wad:
                records_by_path[path]["read"] = {
                    "status": "unsupported",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            continue
        except (AuditFailure, OSError, WadError) as exc:
            for path in paths_for_wad:
                records_by_path[path]["read"] = {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            continue

        for path in paths_for_wad:
            payload = payloads[path]
            try:
                value = json.loads(payload.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                records_by_path[path]["read"] = {
                    "status": "invalid_json",
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            else:
                decoded[path] = value
                records_by_path[path]["read"] = {
                    "status": "passed",
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }

    records = [records_by_path[path] for path in paths]
    return {
        "stage": stage,
        "requestedPaths": len(paths),
        "preparedSessions": prepared_sessions,
        "readManyCalls": read_many_calls,
        "hitWads": len(reads_by_wad),
        **_summarize_lcu_records(records),
        "paths": records,
    }, decoded


def _require_lcu_stage_passed(stage: Mapping[str, object]) -> None:
    if (
        stage.get("readFailures") != 0
        or stage.get("duplicateHits") != 0
        or stage.get("wrongCompression") != 0
        or stage.get("nonZeroSubchunks") != 0
    ):
        raise AuditFailure(
            "lcu-official-data",
            f"LCU {stage.get('stage')} stage failed its path/TOC/read Gate",
        )


def _parse_official_champion_ids(
    summary: Any,
    skins: Mapping[str, Any],
) -> tuple[tuple[int, ...], dict[str, int]]:
    if not isinstance(summary, list):
        raise AuditFailure(
            "lcu-official-data",
            f"{LCU_CHAMPION_SUMMARY_PATH} must be a JSON array",
        )

    champion_ids: set[int] = set()
    seen_summary_ids: set[int] = set()
    excluded = {
        "nonObjectEntries": 0,
        "invalidChampionIds": 0,
        "missingBaseSkinRecords": 0,
    }
    for entry in summary:
        if not isinstance(entry, dict):
            excluded["nonObjectEntries"] += 1
            continue
        champion_id = entry.get("id")
        # This mirrors script.py's official-summary consumers: only
        # non-negative integer IDs participate.  bool is rejected explicitly
        # because JSON booleans are not champion identifiers.
        if (
            isinstance(champion_id, bool)
            or not isinstance(champion_id, int)
            or champion_id < 0
        ):
            excluded["invalidChampionIds"] += 1
            continue
        if champion_id in seen_summary_ids:
            raise AuditFailure(
                "lcu-official-data",
                f"duplicate official champion id {champion_id}",
            )
        seen_summary_ids.add(champion_id)
        base_skin_id = champion_id * 1000
        base_skin = skins.get(str(base_skin_id))
        if base_skin is None:
            excluded["missingBaseSkinRecords"] += 1
            continue
        if (
            not isinstance(base_skin, dict)
            or base_skin.get("id") != base_skin_id
            or base_skin.get("isBase") is not True
        ):
            raise AuditFailure(
                "lcu-official-data",
                f"invalid base skin record {base_skin_id} for summary id "
                f"{champion_id}",
            )
        champion_ids.add(champion_id)
    if not champion_ids:
        raise AuditFailure(
            "lcu-official-data",
            "champion summary contains no valid official champion IDs",
        )
    return tuple(sorted(champion_ids)), excluded


def _parse_legacy_lcu_hashes(data: bytes) -> dict[str, int]:
    entries: dict[str, int] = {}
    try:
        lines = data.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise AuditFailure("lcu-regression", "legacy LCU hash table is not UTF-8") from exc
    for line in lines:
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        try:
            declared = int(parts[0], 16)
        except ValueError:
            continue
        entries[normalize_wad_path(parts[1])] = declared
    return entries


def _summarize_lcu_records(
    records: Iterable[Mapping[str, object]],
) -> dict[str, int]:
    selected = list(records)
    return {
        "computedPaths": len(selected),
        "wadHits": sum(bool(record["wadHits"]) for record in selected),
        "wrongCompression": sum(
            any(hit["compressionType"] != 3 for hit in record["wadHits"])  # type: ignore[index]
            for record in selected
        ),
        "nonZeroSubchunks": sum(
            any(
                hit["subchunkCount"] != 0 or hit["subchunkIndex"] != 0
                for hit in record["wadHits"]  # type: ignore[index]
            )
            for record in selected
        ),
        "duplicateHits": sum(len(record["wadHits"]) > 1 for record in selected),
        "readablePaths": sum(
            record["read"]["status"] == "passed"  # type: ignore[index]
            for record in selected
        ),
        "unsupportedPaths": sum(
            record["read"]["status"] == "unsupported"  # type: ignore[index]
            for record in selected
        ),
        "readFailures": sum(
            record["read"]["status"] != "passed"  # type: ignore[index]
            for record in selected
        ),
    }


def _lcu_stage_summary(stage: Mapping[str, object]) -> dict[str, object]:
    return {
        field: stage[field]
        for field in (
            "stage",
            "requestedPaths",
            "preparedSessions",
            "readManyCalls",
            "hitWads",
            "computedPaths",
            "wadHits",
            "wrongCompression",
            "nonZeroSubchunks",
            "duplicateHits",
            "readablePaths",
            "unsupportedPaths",
            "readFailures",
        )
    }


def _reject_lcu_path_hash_collisions(paths: Iterable[str]) -> None:
    by_hash: dict[int, str] = {}
    for raw_path in paths:
        path = normalize_wad_path(raw_path)
        path_hash = wad_path_hash(path)
        previous = by_hash.get(path_hash)
        if previous is not None and previous != path:
            raise AuditFailure(
                "lcu-official-data",
                f"LCU XXH64 collision between {previous} and {path}",
            )
        by_hash[path_hash] = path


def _lcu_champion_path(champion_id: int) -> str:
    return f"{LCU_DATA_PREFIX}/champions/{champion_id}.json"


def run_audit(
    inputs: AuditInputs,
    *,
    champion_names: Iterable[str] = (),
    fixed_contract: Mapping[str, int] | None = None,
) -> dict[str, object]:
    pool_data, pool_identity = read_stable_json(inputs.pool)
    source_data, source_identity = read_stable_json(inputs.source_golden)
    if not isinstance(pool_data, dict) or not isinstance(source_data, dict):
        raise AuditFailure("source-contract", "pool and source Golden must be objects")

    all_champions = _champions_by_id(pool_data.get("champions"), "pool")
    selected_ids = _select_champions(all_champions, champion_names)
    complete = selected_ids == set(all_champions)
    result = _new_result(pool_data, source_identity, complete, selected_ids)
    result["metrics"] = _empty_metrics()

    try:
        pool_binding_mode = _validate_top_level_source_contract(
            pool_data,
            pool_identity,
            source_data,
            inputs.pool,
        )
        plans = build_champion_plans(pool_data, source_data, selected_ids)
        comparable_paths = {
            path
            for plan in plans
            if plan.source_mode == "legacy-comparable"
            for path in plan.required_paths
        }
        direct_only_paths = {
            path
            for plan in plans
            if plan.source_mode == "direct-only"
            for path in plan.required_paths
        }

        config_data, config_identity = read_stable_json(inputs.config)
        if not isinstance(config_data, dict):
            raise AuditFailure("game-identity", "config JSON must be an object")
        lol_path = config_data.get("lol_path")
        if not isinstance(lol_path, str) or not lol_path:
            raise AuditFailure("game-identity", "config is missing lol_path")
        league_root = Path(lol_path).resolve(strict=True)
        champions_dir = league_root / "Game" / "DATA" / "FINAL" / "Champions"

        metadata_path = league_root / "Game" / "content-metadata.json"
        metadata_data, metadata_identity = read_stable_json(metadata_path)
        _validate_game_identity(pool_data, source_data, metadata_data, metadata_identity)

        hash_identity, hash_coverage = scan_hash_source(
            inputs.hash_source,
            comparable_paths,
            direct_only_paths,
        )
        _require_bound_identity(
            source_data.get("hashSource"),
            hash_identity,
            "hash source",
        )
        legacy_tool_identity = stable_file_identity(inputs.legacy_tool)
        _require_bound_identity(
            source_data.get("legacyTool"),
            legacy_tool_identity,
            "legacy tool",
        )

        wad_identities: dict[int, dict[str, object]] = {}
        for plan in plans:
            wad_path = champions_dir / plan.wad_name
            actual_identity = stable_file_identity(wad_path)
            _require_bound_identity(
                plan.expected_wad_identity,
                actual_identity,
                f"{plan.champion} WAD",
            )
            wad_identities[plan.champion_id] = actual_identity

        result["identityGate"] = {
            "status": "passed",
            "pool": {**pool_identity, "bindingMode": pool_binding_mode},
            "config": config_identity,
            "clientMetadata": metadata_identity,
            "hashSource": hash_identity,
            "legacyTool": legacy_tool_identity,
            "championWads": [
                {
                    "championId": plan.champion_id,
                    "champion": plan.champion,
                    **wad_identities[plan.champion_id],
                }
                for plan in plans
            ],
        }
        result["hashSourceAudit"] = hash_coverage
    except (AuditFailure, OSError) as exc:
        phase = exc.phase if isinstance(exc, AuditFailure) else "identity"
        result["status"] = "failed"
        result["identityGate"] = {
            "status": "failed",
            "phase": phase,
            "error": f"{type(exc).__name__}: {exc}",
            "preparedReadsStarted": False,
        }
        result["hardGate"] = {
            "status": "not_applicable" if not complete else "failed",
            "reason": (
                "partial champion selection"
                if not complete
                else "source identity/contract Gate failed before Prepared reads"
            ),
        }
        return result

    observer = PreparedObserver()
    metrics = _expected_shape_metrics(plans)
    champions: list[dict[str, object]] = []
    for plan in plans:
        record, counters = _audit_champion(
            plan,
            champions_dir / plan.wad_name,
            observer,
            wad_identities[plan.champion_id],
        )
        champions.append(record)
        for field, value in counters.items():
            metrics[field] += value
    metrics["preparedSessions"] = len(plans)
    metrics["wadIndexes"] = observer.indexes
    metrics["physicalChunkReads"] = observer.physical_chunks
    metrics["successfulChunkReads"] = observer.successful_chunks
    metrics["failedChunkReads"] = observer.failed_chunks
    metrics["compressionType0Reads"] = observer.compression_reads.get(0, 0)
    metrics["compressionType1Reads"] = observer.compression_reads.get(1, 0)
    metrics["compressionType3Reads"] = observer.compression_reads.get(3, 0)
    metrics["compressionOtherReads"] = sum(
        count
        for compression_type, count in observer.compression_reads.items()
        if compression_type not in {0, 1, 3}
    )
    result["champions"] = champions

    try:
        lcu_bundle = audit_lcu_official_data(league_root, inputs.lcu_hashes)
        lcu_official = lcu_bundle["official"]
        lcu_regression = lcu_bundle["regression"]
    except (AuditFailure, OSError, ValueError) as exc:
        lcu_official = {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "officialChampionIds": 0,
            "computedPaths": 0,
            "wadHits": 0,
            "wrongCompression": 0,
            "nonZeroSubchunks": 0,
            "duplicateHits": 0,
            "readablePaths": 0,
            "unsupportedPaths": 0,
            "readFailures": 2,
        }
        lcu_regression = {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "computedPaths": 0,
            "legacyTableMissing": 0,
            "legacyTableMismatches": 0,
            "wadHits": 0,
            "wrongCompression": 0,
            "nonZeroSubchunks": 0,
            "duplicateHits": 0,
            "readablePaths": 0,
            "unsupportedPaths": 0,
            "readFailures": len(LCU_REGRESSION_IDS),
        }
        lcu_bundle = {
            "combinedReadFailures": len(LCU_REGRESSION_IDS) + 2,
        }
    result["lcuOfficialDataAudit"] = lcu_official
    result["lcuLegacyHashRegressionAudit"] = lcu_regression
    for metric_field, audit_field in (
        ("lcuOfficialChampionIds", "officialChampionIds"),
        ("lcuOfficialComputedPaths", "computedPaths"),
        ("lcuOfficialWadHits", "wadHits"),
        ("lcuOfficialWrongCompression", "wrongCompression"),
        ("lcuOfficialNonZeroSubchunks", "nonZeroSubchunks"),
        ("lcuOfficialDuplicateHits", "duplicateHits"),
        ("lcuOfficialReadablePaths", "readablePaths"),
        ("lcuOfficialUnsupportedPaths", "unsupportedPaths"),
        ("lcuOfficialReadFailures", "readFailures"),
    ):
        metrics[metric_field] = int(lcu_official[audit_field])  # type: ignore[index]
    for metric_field, audit_field in (
        ("lcuRegressionComputedPaths", "computedPaths"),
        ("lcuRegressionLegacyTableMissing", "legacyTableMissing"),
        ("lcuRegressionLegacyTableMismatches", "legacyTableMismatches"),
        ("lcuRegressionWadHits", "wadHits"),
        ("lcuRegressionWrongCompression", "wrongCompression"),
        ("lcuRegressionNonZeroSubchunks", "nonZeroSubchunks"),
        ("lcuRegressionDuplicateHits", "duplicateHits"),
        ("lcuRegressionReadablePaths", "readablePaths"),
        ("lcuRegressionUnsupportedPaths", "unsupportedPaths"),
        ("lcuRegressionReadFailures", "readFailures"),
    ):
        metrics[metric_field] = int(lcu_regression[audit_field])  # type: ignore[index]
    metrics["lcuCombinedReadFailures"] = int(lcu_bundle["combinedReadFailures"])
    metrics["readFailures"] += metrics["lcuCombinedReadFailures"]
    result["metrics"] = metrics

    if not complete:
        result["status"] = (
            "passed"
            if metrics["readFailures"] == 0
            and metrics["shaMismatches"] == 0
            and metrics["missingRequiredPaths"] == 0
            and metrics["unsupportedRequiredChunks"] == 0
            else "failed"
        )
        result["hardGate"] = {
            "status": "not_applicable",
            "reason": "partial champion selection",
        }
        return result

    expected = _dynamic_contract(
        plans,
        official_champion_ids=metrics["lcuOfficialChampionIds"],
    )
    formal = fixed_contract
    if formal is None:
        formal = FIXED_POOL_CONTRACTS.get(str(pool_data.get("poolId")))
    if formal is not None:
        expected.update(formal)
    mismatches = {
        field: {"expected": value, "actual": metrics.get(field)}
        for field, value in expected.items()
        if metrics.get(field) != value
    }
    result["hardGate"] = {
        "status": "passed" if not mismatches else "failed",
        "expected": expected,
        "mismatches": mismatches,
    }
    result["status"] = "passed" if not mismatches else "failed"
    return result


def _audit_champion(
    plan: ChampionPlan,
    wad_path: Path,
    observer: PreparedObserver,
    expected_wad_identity: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, int]]:
    before_indexes = observer.indexes
    before_chunks = observer.physical_chunks
    before_successful_chunks = observer.successful_chunks
    before_failed_chunks = observer.failed_chunks
    before_compression_reads = dict(observer.compression_reads)
    counters = {
        "readManyCalls": 0,
        "shaComparisons": 0,
        "shaMismatches": 0,
        "missingRequiredPaths": 0,
        "unsupportedRequiredChunks": 0,
        "readFailures": 0,
    }
    try:
        prepared = PreparedChampionWad(wad_path, observer=observer)
        _require_prepared_identity(prepared.file_identity, expected_wad_identity)
        counters["readManyCalls"] = 1
        payloads = prepared.read_many(plan.required_paths, validate_bin=True)
        _require_prepared_identity(prepared.file_identity, expected_wad_identity)
        ending_identity = stable_file_identity(wad_path)
        _require_exact_file_identity(
            expected_wad_identity,
            ending_identity,
            f"{plan.champion} WAD after Prepared read",
        )
        actual_sha = {
            path: hashlib.sha256(payloads[path]).hexdigest()
            for path in plan.required_paths
        }
        mismatches: list[dict[str, str]] = []
        if plan.source_mode == "legacy-comparable":
            for path, expected in plan.expected_sha256.items():
                counters["shaComparisons"] += 1
                actual = actual_sha[path]
                if actual != expected:
                    counters["shaMismatches"] += 1
                    mismatches.append(
                        {"path": path, "expected": expected, "actual": actual}
                    )
        status = "passed" if not mismatches else "failed"
        record: dict[str, object] = {
            "championId": plan.champion_id,
            "champion": plan.champion,
            "status": status,
            "sourceMode": plan.source_mode,
            "wadVersion": str(prepared.version),
            "tocDigest": prepared.toc_digest,
            "logicalPathReferences": plan.logical_path_references,
            "uniqueRequiredPaths": len(plan.required_paths),
            "expectedManifestSha256": (
                _manifest_digest(plan.expected_sha256)
                if plan.expected_sha256
                else None
            ),
            "actualManifestSha256": _manifest_digest(actual_sha),
            "shaComparisons": counters["shaComparisons"],
            "shaMismatches": mismatches,
        }
    except WadPathNotFound as exc:
        counters["missingRequiredPaths"] = len(exc.paths)
        counters["readFailures"] = 1
        record = _failed_champion_record(plan, exc)
    except UnsupportedWadFeature as exc:
        counters["unsupportedRequiredChunks"] = 1
        counters["readFailures"] = 1
        record = _failed_champion_record(plan, exc)
    except (OSError, ValueError) as exc:
        counters["readFailures"] = 1
        record = _failed_champion_record(plan, exc)
    record["prepared"] = {
        "sessions": 1,
        "indexes": observer.indexes - before_indexes,
        "readManyCalls": counters["readManyCalls"],
        "physicalChunkReads": observer.physical_chunks - before_chunks,
        "successfulChunkReads": (
            observer.successful_chunks - before_successful_chunks
        ),
        "failedChunkReads": observer.failed_chunks - before_failed_chunks,
        "compressionReads": {
            str(compression_type): count - before_compression_reads.get(
                compression_type,
                0,
            )
            for compression_type, count in sorted(observer.compression_reads.items())
            if count - before_compression_reads.get(compression_type, 0)
        },
    }
    return record, counters


def _build_comparable_plan(
    champion_id: int,
    name: str,
    wad_name: str,
    main_unit: str,
    skins: tuple[int, ...],
    champion: Mapping[str, Any],
    source: Mapping[str, Any],
) -> ChampionPlan:
    pairs = source.get("pairs")
    if not isinstance(pairs, list):
        raise AuditFailure("source-contract", f"{name}: Golden pairs must be a list")
    expected_pair_count = _required_int(champion, "pairedCount", name)
    if len(pairs) != expected_pair_count or source.get("pairedCount") != len(pairs):
        raise AuditFailure("source-contract", f"{name}: Golden pairedCount mismatch")
    if source.get("skinCount") != len(skins):
        raise AuditFailure("source-contract", f"{name}: Golden skinCount mismatch")
    if source.get("skinSet") != list(skins):
        raise AuditFailure(
            "source-contract",
            f"{name}: Golden skinSet does not match the selected pool skins",
        )

    expected_sha: dict[str, str] = {}
    base_paths: set[str] = set()
    units_by_skin: dict[int, set[str]] = {}
    for pair in pairs:
        if not isinstance(pair, dict):
            raise AuditFailure("source-contract", f"{name}: invalid pair record")
        context = pair.get("context")
        if not isinstance(context, dict):
            raise AuditFailure("source-contract", f"{name}: pair has no context")
        if context.get("champion") != name:
            raise AuditFailure(
                "source-contract",
                f"{name}: pair context champion does not match",
            )
        skin_number = context.get("skin_number")
        if (
            isinstance(skin_number, bool)
            or not isinstance(skin_number, int)
            or skin_number not in skins
        ):
            raise AuditFailure(
                "source-contract",
                f"{name}: pair context has an unselected skin number",
            )
        unit_value = context.get("unit")
        if not isinstance(unit_value, str) or not unit_value:
            raise AuditFailure("source-contract", f"{name}: pair context has no unit")
        unit = normalize_wad_path(unit_value)
        if "/" in unit or re.fullmatch(r"[a-z0-9_]+", unit) is None:
            raise AuditFailure(
                "source-contract",
                f"{name}: pair context has an invalid unit",
            )
        if context.get("stage") != "phase0-direct-legacy-bytes":
            raise AuditFailure(
                "source-contract",
                f"{name}: pair context has an unexpected evidence stage",
            )

        base_path = normalize_wad_path(
            _required_string(pair, "basePath", name)
        )
        target_path = normalize_wad_path(
            _required_string(pair, "targetPath", name)
        )
        expected_base_path = f"data/characters/{unit}/skins/skin0.bin"
        expected_target_path = (
            f"data/characters/{unit}/skins/skin{skin_number}.bin"
        )
        if (
            SKIN_PATH_RE.fullmatch(base_path) is None
            or SKIN_PATH_RE.fullmatch(target_path) is None
            or base_path != expected_base_path
            or target_path != expected_target_path
        ):
            raise AuditFailure(
                "source-contract",
                f"{name}: pair paths disagree with context unit/skin",
            )
        per_skin_units = units_by_skin.setdefault(skin_number, set())
        if unit in per_skin_units:
            raise AuditFailure(
                "source-contract",
                f"{name}: duplicate pair for skin{skin_number}/{unit}",
            )
        per_skin_units.add(unit)

        for path_field, sha_field in (
            ("basePath", "baseSha256"),
            ("targetPath", "targetSha256"),
        ):
            path = base_path if path_field == "basePath" else target_path
            sha = _required_string(pair, sha_field, name).lower()
            if SHA256_RE.fullmatch(sha) is None:
                raise AuditFailure(
                    "source-contract",
                    f"{name}: invalid {sha_field} for {path}",
                )
            previous = expected_sha.get(path)
            if previous is not None and previous != sha:
                raise AuditFailure(
                    "source-contract",
                    f"{name}: conflicting SHA-256 for {path}",
                )
            expected_sha[path] = sha
            if path_field == "basePath":
                base_paths.add(path)
    missing_main_unit = [
        skin_number
        for skin_number in skins
        if main_unit not in units_by_skin.get(skin_number, set())
    ]
    if missing_main_unit:
        raise AuditFailure(
            "source-contract",
            f"{name}: mainUnit {main_unit!r} is missing for selected skins "
            f"{missing_main_unit}",
        )
    unique_base = _required_int(champion, "uniqueBaseCount", name)
    if len(base_paths) != unique_base or source.get("uniqueBaseCount") != unique_base:
        raise AuditFailure("source-contract", f"{name}: uniqueBaseCount mismatch")
    _reject_path_hash_collisions(expected_sha, name)
    return ChampionPlan(
        champion_id=champion_id,
        champion=name,
        wad_name=wad_name,
        main_unit=main_unit,
        source_mode="legacy-comparable",
        required_paths=tuple(sorted(expected_sha)),
        expected_sha256=expected_sha,
        skin_count=len(skins),
        pair_references=len(pairs),
        unique_base_chunks=len(base_paths),
        logical_path_references=2 * len(pairs),
        expected_wad_identity=_required_mapping(source, "wad", name),
    )


def _build_direct_only_plan(
    champion_id: int,
    name: str,
    wad_name: str,
    main_unit: str,
    skins: tuple[int, ...],
    champion: Mapping[str, Any],
    source: Mapping[str, Any],
) -> ChampionPlan:
    pair_count = _required_int(champion, "pairedCount", name)
    if pair_count != len(skins):
        raise AuditFailure(
            "source-contract",
            f"{name}: direct-only standard paths require one pair per skin",
        )
    if source.get("declaredPairCount") != pair_count:
        raise AuditFailure(
            "source-contract",
            f"{name}: Golden declaredPairCount mismatch",
        )
    paths = (
        f"data/characters/{main_unit}/skins/skin0.bin",
        *(
            f"data/characters/{main_unit}/skins/skin{skin}.bin"
            for skin in skins
        ),
    )
    normalized = tuple(normalize_wad_path(path) for path in paths)
    _reject_path_hash_collisions(dict.fromkeys(normalized, ""), name)
    return ChampionPlan(
        champion_id=champion_id,
        champion=name,
        wad_name=wad_name,
        main_unit=main_unit,
        source_mode="direct-only",
        required_paths=normalized,
        expected_sha256={},
        skin_count=len(skins),
        pair_references=pair_count,
        unique_base_chunks=1,
        logical_path_references=2 * pair_count,
        expected_wad_identity=_required_mapping(source, "wad", name),
    )


def _validate_top_level_source_contract(
    pool: Mapping[str, Any],
    pool_identity: Mapping[str, object],
    source: Mapping[str, Any],
    pool_path: Path,
) -> str:
    if pool.get("schemaVersion") != 1:
        raise AuditFailure("source-contract", "pool schemaVersion must be 1")
    if source.get("schemaVersion") != 2:
        raise AuditFailure("source-contract", "source Golden schemaVersion must be 2")
    lifecycle_fields = {
        "status",
        "complete",
        "expectedChampionCount",
        "processedChampionCount",
    }
    present_lifecycle_fields = lifecycle_fields.intersection(source)
    if present_lifecycle_fields:
        if present_lifecycle_fields != lifecycle_fields:
            raise AuditFailure(
                "source-contract",
                "source Golden has an incomplete lifecycle contract",
            )
        source_champions = source.get("champions")
        if not isinstance(source_champions, list):
            raise AuditFailure(
                "source-contract",
                "source Golden champions must be a list",
            )
        expected_count = source.get("expectedChampionCount")
        processed_count = source.get("processedChampionCount")
        if (
            source.get("status") != "passed"
            or source.get("complete") is not True
            or isinstance(expected_count, bool)
            or not isinstance(expected_count, int)
            or isinstance(processed_count, bool)
            or not isinstance(processed_count, int)
            or expected_count != processed_count
            or processed_count != len(source_champions)
        ):
            raise AuditFailure(
                "source-contract",
                "source Golden lifecycle did not complete successfully",
            )
    input_stability = source.get("inputStability")
    if input_stability is not None:
        if not isinstance(input_stability, Mapping):
            raise AuditFailure(
                "source-contract",
                "source Golden inputStability must be an object",
            )
        if input_stability.get("status") != "passed":
            raise AuditFailure(
                "source-contract",
                "source Golden inputStability did not pass",
            )
        execution_snapshot = input_stability.get("executionSnapshot")
        if not isinstance(execution_snapshot, Mapping):
            raise AuditFailure(
                "source-contract",
                "source Golden is missing its execution snapshot identity",
            )
        _require_bound_identity(
            execution_snapshot.get("legacyTool"),
            source.get("legacyTool"),
            "Golden legacy tool snapshot",
        )
        _require_bound_identity(
            execution_snapshot.get("hashSource"),
            source.get("hashSource"),
            "Golden hash source snapshot",
        )
    for field in ("poolId", "gameVersion"):
        if pool.get(field) != source.get(field):
            raise AuditFailure(
                "source-contract",
                f"pool and source Golden {field} do not match",
            )
    return _require_bound_text_identity(
        source.get("pool"),
        pool_identity,
        pool_path,
        "pool",
    )


def _validate_game_identity(
    pool: Mapping[str, Any],
    source: Mapping[str, Any],
    metadata: Any,
    metadata_identity: Mapping[str, object],
) -> None:
    if not isinstance(metadata, dict):
        raise AuditFailure("game-identity", "content metadata must be an object")
    actual_version = metadata.get("version")
    expected_version = pool.get("gameVersion")
    if not isinstance(actual_version, str) or not isinstance(expected_version, str):
        raise AuditFailure("game-identity", "missing game version")
    if comparable_game_version(actual_version) != comparable_game_version(
        expected_version
    ):
        raise AuditFailure(
            "game-identity",
            f"installed game {actual_version} does not match pool {expected_version}",
        )
    client = source.get("client")
    if not isinstance(client, dict):
        raise AuditFailure("game-identity", "source Golden is missing client identity")
    _require_bound_identity(client.get("source"), metadata_identity, "client metadata")


def _require_bound_identity(
    expected: object,
    actual: Mapping[str, object],
    label: str,
) -> None:
    if not isinstance(expected, Mapping):
        raise AuditFailure("identity", f"missing expected {label} identity")
    for field in ("size", "sha256"):
        if expected.get(field) != actual.get(field):
            raise AuditFailure(
                "identity",
                f"{label} {field} mismatch: expected {expected.get(field)!r}, "
                f"got {actual.get(field)!r}",
            )


def _require_bound_text_identity(
    expected: object,
    actual: Mapping[str, object],
    path: Path,
    label: str,
) -> str:
    """Bind text exactly, allowing only LF/CRLF checkout normalization."""

    if not isinstance(expected, Mapping):
        raise AuditFailure("identity", f"missing expected {label} identity")
    if (
        expected.get("size") == actual.get("size")
        and expected.get("sha256") == actual.get("sha256")
    ):
        return "exact"

    raw, repeated_identity = read_stable_bytes(path)
    if (
        repeated_identity["size"] != actual.get("size")
        or repeated_identity["sha256"] != actual.get("sha256")
    ):
        raise AuditFailure("identity", f"{label} changed during identity Gate")
    normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    variants = (normalized, normalized.replace(b"\n", b"\r\n"))
    for variant in variants:
        if (
            len(variant) == expected.get("size")
            and hashlib.sha256(variant).hexdigest() == expected.get("sha256")
        ):
            return "newline-equivalent"
    raise AuditFailure(
        "identity",
        f"{label} content mismatch: expected size/SHA-256 "
        f"{expected.get('size')}/{expected.get('sha256')}, got "
        f"{actual.get('size')}/{actual.get('sha256')}",
    )


def _expected_shape_metrics(plans: Iterable[ChampionPlan]) -> dict[str, int]:
    selected = list(plans)
    comparable = [plan for plan in selected if plan.source_mode == "legacy-comparable"]
    direct = [plan for plan in selected if plan.source_mode == "direct-only"]
    return {
        "selectedChampions": len(selected),
        "comparableChampions": len(comparable),
        "directOnlyChampions": len(direct),
        # Phase comparisons are intentionally limited to the cohort which
        # has legacy Golden bytes. Direct-only sentinels such as Locke prove
        # dictionary-independent access but must not inflate comparable
        # skins, pairs, bases, or logical references.
        "comparableSkins": sum(plan.skin_count for plan in comparable),
        "directOnlyDeclaredSkins": sum(plan.skin_count for plan in direct),
        "totalDeclaredSkins": sum(plan.skin_count for plan in selected),
        "skins": sum(plan.skin_count for plan in comparable),
        "comparablePairReferences": sum(
            plan.pair_references for plan in comparable
        ),
        "directOnlyPairReferences": sum(
            plan.pair_references for plan in direct
        ),
        "totalPairReferences": sum(
            plan.pair_references for plan in selected
        ),
        "pairReferences": sum(plan.pair_references for plan in comparable),
        "comparableUniqueBaseChunks": sum(
            plan.unique_base_chunks for plan in comparable
        ),
        "directOnlyBaseChunks": sum(
            plan.unique_base_chunks for plan in direct
        ),
        "totalUniqueBaseChunks": sum(
            plan.unique_base_chunks for plan in selected
        ),
        "uniqueBaseChunks": sum(
            plan.unique_base_chunks for plan in comparable
        ),
        "comparableLogicalPathReferences": sum(
            plan.logical_path_references for plan in comparable
        ),
        "directOnlyLogicalPathReferences": sum(
            plan.logical_path_references for plan in direct
        ),
        "totalLogicalPathReferences": sum(
            plan.logical_path_references for plan in selected
        ),
        "comparableUniqueChunks": sum(
            len(plan.required_paths) for plan in comparable
        ),
        "directOnlyChunks": sum(len(plan.required_paths) for plan in direct),
        "totalRequiredChunks": sum(len(plan.required_paths) for plan in selected),
        **{
            field: 0
            for field in (
                "preparedSessions",
                "wadIndexes",
                "readManyCalls",
                "physicalChunkReads",
                "successfulChunkReads",
                "failedChunkReads",
                "compressionType0Reads",
                "compressionType1Reads",
                "compressionType3Reads",
                "compressionOtherReads",
                "shaComparisons",
                "shaMismatches",
                "missingRequiredPaths",
                "unsupportedRequiredChunks",
                "readFailures",
                "lcuOfficialChampionIds",
                "lcuOfficialComputedPaths",
                "lcuOfficialWadHits",
                "lcuOfficialWrongCompression",
                "lcuOfficialNonZeroSubchunks",
                "lcuOfficialDuplicateHits",
                "lcuOfficialReadablePaths",
                "lcuOfficialUnsupportedPaths",
                "lcuOfficialReadFailures",
                "lcuRegressionComputedPaths",
                "lcuRegressionLegacyTableMissing",
                "lcuRegressionLegacyTableMismatches",
                "lcuRegressionWadHits",
                "lcuRegressionWrongCompression",
                "lcuRegressionNonZeroSubchunks",
                "lcuRegressionDuplicateHits",
                "lcuRegressionReadablePaths",
                "lcuRegressionUnsupportedPaths",
                "lcuRegressionReadFailures",
                "lcuCombinedReadFailures",
            )
        },
    }


def _dynamic_contract(
    plans: Iterable[ChampionPlan],
    *,
    official_champion_ids: int,
) -> dict[str, int]:
    expected = _expected_shape_metrics(plans)
    for field in (
        "compressionType0Reads",
        "compressionType1Reads",
        "compressionType3Reads",
        "compressionOtherReads",
    ):
        expected.pop(field)
    official_paths = official_champion_ids + 2
    expected.update(
        {
            "preparedSessions": expected["selectedChampions"],
            "wadIndexes": expected["selectedChampions"],
            "readManyCalls": expected["selectedChampions"],
            "physicalChunkReads": expected["totalRequiredChunks"],
            "successfulChunkReads": expected["totalRequiredChunks"],
            "failedChunkReads": 0,
            "shaComparisons": expected["comparableUniqueChunks"],
            "lcuOfficialChampionIds": official_champion_ids,
            "lcuOfficialComputedPaths": official_paths,
            "lcuOfficialWadHits": official_paths,
            "lcuOfficialReadablePaths": official_paths,
            "lcuRegressionComputedPaths": len(LCU_REGRESSION_IDS),
            "lcuRegressionLegacyTableMissing": len(LCU_REGRESSION_IDS),
            "lcuRegressionWadHits": len(LCU_REGRESSION_IDS),
            "lcuRegressionReadablePaths": len(LCU_REGRESSION_IDS),
        }
    )
    return expected


def _empty_metrics() -> dict[str, int]:
    return _expected_shape_metrics(())


def _new_result(
    pool: Mapping[str, Any],
    source_identity: Mapping[str, object],
    complete: bool,
    selected_ids: set[int],
) -> dict[str, object]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "audit": "phase-1-prepared-champion-wad",
        "poolId": pool.get("poolId"),
        "gameVersion": pool.get("gameVersion"),
        "complete": complete,
        "selectedChampionIds": sorted(selected_ids),
        "sourceGolden": dict(source_identity),
        "status": "running",
        "identityGate": {"status": "pending"},
        "champions": [],
    }


def _failed_champion_record(
    plan: ChampionPlan,
    exc: BaseException,
) -> dict[str, object]:
    return {
        "championId": plan.champion_id,
        "champion": plan.champion,
        "status": "failed",
        "sourceMode": plan.source_mode,
        "logicalPathReferences": plan.logical_path_references,
        "uniqueRequiredPaths": len(plan.required_paths),
        "error": {"type": type(exc).__name__, "message": str(exc)},
    }


def _manifest_digest(paths_to_sha: Mapping[str, str]) -> str:
    digest = hashlib.sha256()
    for path, sha in sorted(paths_to_sha.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _reject_path_hash_collisions(paths: Mapping[str, object], champion: str) -> None:
    by_hash: dict[int, str] = {}
    for path in paths:
        path_hash = wad_path_hash(path)
        previous = by_hash.get(path_hash)
        if previous is not None and previous != path:
            raise AuditFailure(
                "source-contract",
                f"{champion}: XXH64 collision between {previous} and {path}",
            )
        by_hash[path_hash] = path


def _select_champions(
    champions: Mapping[int, Mapping[str, Any]],
    names: Iterable[str],
) -> set[int]:
    wanted = {name.casefold() for name in names}
    if not wanted:
        return set(champions)
    by_name = {
        _required_string(champion, "query", "pool champion").casefold(): champion_id
        for champion_id, champion in champions.items()
    }
    missing = sorted(wanted - by_name.keys())
    if missing:
        raise AuditFailure(
            "selection",
            f"champions not present in pool: {missing}",
        )
    return {by_name[name] for name in wanted}


def _champions_by_id(value: object, label: str) -> dict[int, Mapping[str, Any]]:
    if not isinstance(value, list) or not value:
        raise AuditFailure("source-contract", f"{label} champions must be a list")
    result: dict[int, Mapping[str, Any]] = {}
    for champion in value:
        if not isinstance(champion, Mapping):
            raise AuditFailure("source-contract", f"{label} champion is not an object")
        champion_id = _required_int(champion, "championId", label)
        if champion_id in result:
            raise AuditFailure(
                "source-contract",
                f"{label} has duplicate championId {champion_id}",
            )
        result[champion_id] = champion
    return result


def _required_mapping(
    value: Mapping[str, Any],
    field: str,
    label: str,
) -> Mapping[str, object]:
    item = value.get(field)
    if not isinstance(item, Mapping):
        raise AuditFailure("source-contract", f"{label}: missing {field}")
    return item


def _required_string(
    value: Mapping[str, Any],
    field: str,
    label: str,
) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item:
        raise AuditFailure("source-contract", f"{label}: invalid {field}")
    return item


def _required_int(
    value: Mapping[str, Any],
    field: str,
    label: str,
) -> int:
    item = value.get(field)
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise AuditFailure("source-contract", f"{label}: invalid {field}")
    return item


def _positive_int_set(values: Iterable[object], label: str) -> set[int]:
    result: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise AuditFailure("source-contract", f"{label}: invalid value {value!r}")
        if value in result:
            raise AuditFailure("source-contract", f"{label}: duplicate {value}")
        result.add(value)
    return result


def _require_prepared_identity(
    actual: WadFileIdentity,
    expected: Mapping[str, object],
) -> None:
    actual_fields = {
        "path": str(actual.resolved_path),
        "device": actual.device,
        "inode": actual.inode,
        "size": actual.size,
        "modifiedNs": actual.mtime_ns,
    }
    mismatches = {
        field: {
            "expected": expected.get(field),
            "actual": value,
        }
        for field, value in actual_fields.items()
        if expected.get(field) != value
    }
    if mismatches:
        raise AuditFailure(
            "identity",
            f"Prepared WAD identity differs from the SHA-bound snapshot: "
            f"{mismatches}",
        )


def _require_exact_file_identity(
    expected: Mapping[str, object],
    actual: Mapping[str, object],
    label: str,
) -> None:
    fields = ("path", "device", "inode", "size", "modifiedNs", "sha256")
    mismatches = {
        field: {
            "expected": expected.get(field),
            "actual": actual.get(field),
        }
        for field in fields
        if expected.get(field) != actual.get(field)
    }
    if mismatches:
        raise AuditFailure(
            "identity",
            f"{label} differs from the SHA-bound snapshot: {mismatches}",
        )


def _stat_key(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _file_identity(
    path: Path,
    stat: os.stat_result,
    sha256: str,
) -> dict[str, object]:
    return {
        "path": str(path),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "size": int(stat.st_size),
        "modifiedNs": int(stat.st_mtime_ns),
        "sha256": sha256,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    parser.add_argument(
        "--source-golden",
        type=Path,
        required=True,
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--hashes-game", type=Path, default=DEFAULT_HASH_SOURCE)
    parser.add_argument("--legacy-tool", type=Path, default=DEFAULT_LEGACY_TOOL)
    parser.add_argument("--lcu-hashes", type=Path, default=DEFAULT_LCU_HASHES)
    parser.add_argument("--champion", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    inputs = AuditInputs(
        pool=args.pool,
        source_golden=args.source_golden,
        output=args.output,
        config=args.config,
        hash_source=args.hashes_game,
        legacy_tool=args.legacy_tool,
        lcu_hashes=args.lcu_hashes,
    )
    try:
        result = run_audit(inputs, champion_names=args.champion)
    except (AuditFailure, OSError, ValueError) as exc:
        result = {
            "schemaVersion": SCHEMA_VERSION,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "audit": "phase-1-prepared-champion-wad",
            "complete": not bool(args.champion),
            "status": "failed",
            "error": {
                "phase": exc.phase if isinstance(exc, AuditFailure) else "startup",
                "type": type(exc).__name__,
                "message": str(exc),
            },
            "hardGate": {
                "status": "not_applicable" if args.champion else "failed"
            },
        }
    write_json_atomically(args.output, result)
    metrics = result.get("metrics", {})
    print(
        f"Prepared audit {result['status']}: "
        f"champions={metrics.get('selectedChampions', 0)} "
        f"chunks={metrics.get('physicalChunkReads', 0)} "
        f"shaMismatches={metrics.get('shaMismatches', 0)}",
        flush=True,
    )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
