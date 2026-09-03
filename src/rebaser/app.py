"""Automated League of Legends skin rebaser.

Project layout (relative to the project root):

    config.json              {"lol_path": "..."} (auto-created on first run)
    input/
        <skin name>/
            <unit>/                     (auto-extracted; name = unit,
                                         lowercase, e.g. "annie",
                                         "annietibbers")
                skin0.bin               (base skin)
                skin<N>.bin             (target skin, N != 0)
            <unit2>/                    (optional: summons / additional units)
                ...
            step1/<unit>/        skin0.json, skin<N>.json   (dumped, per unit)
            step2/<unit>/        skin<N>_modified.json      (replaced, per unit)
            step3/               data/characters/<unit>/skins/skin0.bin
            step4/               WAD/<Champion>.wad.client, META/info.json
    output/
        <Champion>/              (champion WAD name, e.g. "JarvanIV")
            <base skin>/
                <base skin>.<zip|fantome>
                <chroma>/
                    <chroma>.<zip|fantome>

Behavior:
    - On first run, prompts for LoL install path and saves to config.json.
    - Prompts for a mode:
        1) champion - input one champion name; every non-classic entry of that
           champion is processed, including chromas.
        2) skin     - input exact skin/chroma names, canonical full skin IDs,
           or `skin<N> <Champion>`, comma-separated; each match is processed
           independently without expanding its group.
      Reads local champion WAD metadata and treats every skin/chroma ID as a
      separate selectable entry.
    - Extracts skin0.bin + skin<N>.bin from the champion WAD found in
      <LoL path>/Game/DATA/FINAL/Champions/ (all matching units auto-discovered).
    - Multi-unit support: champions with summons (Annie+Tibbers, etc.) get one
      subfolder per unit; all units are packed into a single WAD.
    - Main champion identity comes from the local official LCU roster and is
      verified against the exact champion WAD.
    - Author is hardcoded to "Untargetable".
    - Archive output defaults to .zip. Use --format fantome or --format both
      to generate cslol-compatible .fantome output.
    - Existing selected outputs are validated before prepare/extract. Use
      --force to rebuild them regardless of the cache result.
    - Uses the local hashes.game dictionary without networking by default. Use
      --hash-update auto or --hash-update force to check/download explicitly.
    - A timing summary is printed after success or failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from time import perf_counter_ns
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping

from .champion_layout import (
    CandidateRegistry,
    CandidateRegistryError,
    ChampionIdentity,
    ChampionIdentityError,
    ChampionLayout,
    ChampionLayoutError,
    HashSkinCandidateSet,
    LAYOUT_ALGORITHM_VERSION,
    RequiredChunkIdentity,
    SkinLayout,
    build_champion_layout,
    build_hash_skin_champion_layout,
    candidate_registry_from_hash_candidates,
    candidate_units_for,
    champion_skin_path,
    deserialize_skin_layout,
    ensure_hash_chunk_identities,
    ensure_required_chunk_identities,
    derive_hash_skin_candidates,
    find_champion_identity,
    load_candidate_registry,
    parse_official_champion_identities,
    select_main_unit_directory,
    serialize_skin_layout,
    serialize_required_chunk_identity,
    validate_identity_wad,
)
from .base_cache import (
    BaseCacheError,
    BaseParseKey,
    BaseRebaseSnapshot,
    ProcessBaseParseCache,
    ToolIdentity,
    capture_tool_identity,
)
from .persistent_cache import (
    PersistentCacheKey,
    PersistentJsonCache,
)
from .ritobin_batch import (
    RitobinBatchError,
    RitobinBatchItem,
    run_bounded_recursive_batches,
)
from .hashes_game_update import (
    HashUpdateError,
    UPDATE_MODES as HASH_UPDATE_MODES,
    ensure_latest_hashes_game,
)
from .hash_skin_index import (
    HashSkinIndex,
    HashSkinIndexError,
    HashSkinRecord,
    ensure_hash_skin_index,
)

from .paths import DATA_ROOT, PROJECT_ROOT


# Keep the public name for compatibility.  It now deliberately means the
# repository root instead of the directory containing this implementation.
SCRIPT_DIR = PROJECT_ROOT
RITOBIN_CLI = SCRIPT_DIR / "bin" / "ritobin_cli.exe"
WAD_MAKE = SCRIPT_DIR / "cslol-tools" / "wad-make.exe"
WAD_EXTRACT = SCRIPT_DIR / "cslol-tools" / "wad-extract.exe"
HASHES_GAME_PATH = WAD_EXTRACT.with_name("hashes.game.txt")
INPUT_ROOT = Path(
    os.environ.get("LEAGUE_SKIN_REBASER_INPUT_ROOT", SCRIPT_DIR / "input")
).resolve()
OUTPUT_ROOT = Path(
    os.environ.get("LEAGUE_SKIN_REBASER_OUTPUT_ROOT", SCRIPT_DIR / "output")
).resolve()
CACHE_ROOT = Path(
    os.environ.get("LEAGUE_SKIN_REBASER_CACHE_ROOT", SCRIPT_DIR / ".cache")
).resolve()
HASH_UPDATE_STATE_PATH = CACHE_ROOT / "hashes-game-update.json"
HASH_SKIN_INDEX_PATH = CACHE_ROOT / "hash-skin-index-v1.json"
DERIVED_CACHE_ROOT = CACHE_ROOT / "derived"
CONFIG_PATH = SCRIPT_DIR / "config.json"
AUTHOR = "Untargetable"
MOD_DESCRIPTION = "Generated from local League of Legends game files"
REBASE_SCHEMA_VERSION = 2
# v2 binds base snapshots to the exact-output recursive batch implementation.
BASE_PARSE_PARSER_SCHEMA_VERSION = 2
CATALOG_CACHE_SCHEMA_VERSION = 1
CATALOG_PARSER_SCHEMA_VERSION = 1
LAYOUT_CACHE_SCHEMA_VERSION = 1
ARCHIVE_FINGERPRINT_SCHEMA_VERSION = 2
# The byte ceiling remains the hard resource bound. Real dynamic-cohort probes
# established that 128 small files are safe and avoid unnecessary tool starts.
RITOBIN_BATCH_MAX_FILES = 128
# Elementalist Lux currently produces a roughly 68.7 MiB modified JSON file.
# Permit bounded growth above the old 64 MiB ceiling, but isolate any such
# source so it cannot share a recursive Ritobin process with other inputs.
RITOBIN_BATCH_ISOLATE_ABOVE_BYTES = 64 * 1024 * 1024
RITOBIN_BATCH_MAX_INPUT_BYTES = 80 * 1024 * 1024
WAD_CLIENT_SUFFIX = ".wad.client"
LOL_CHAMPIONS_REL = Path("Game") / "DATA" / "FINAL" / "Champions"
LCU_GAME_DATA_REL = Path("Plugins") / "rcp-be-lol-game-data"
CHAMPION_UNITS_PATH = DATA_ROOT / "champion-units.generated.json"
LCU_CHAMPION_SUMMARY_PATH = (
    "plugins/rcp-be-lol-game-data/global/default/v1/champion-summary.json"
)
LCU_SKINS_PATH = "plugins/rcp-be-lol-game-data/global/default/v1/skins.json"
ARCHIVE_FORMATS = ("zip", "fantome")
WAD_MODES = ("direct", "legacy")
METRICS_SCHEMA_VERSION = 1

try:
    import zstandard as zstd
except ImportError:
    zstd = None

from .wad_access import (
    CorruptWad,
    PreparedChampionWad,
    UnsupportedWadFeature,
    UnsupportedWadVersion,
    WadChangedDuringRead,
    WadChunk,
    WadError,
    WadFileIdentity,
    WadIndex,
    capture_wad_file_identity,
    decode_wad_chunk as decode_wad_chunk_core,
    normalize_wad_path,
    parse_wad_index as parse_wad_index_core,
    preflight_wad_chunk,
    validate_bin_payload,
    wad_path_hash,
)


@dataclass(frozen=True)
class TimingSample:
    phase: str
    elapsed_ns: int
    error: str | None
    scope: tuple[tuple[str, str], ...] = ()


@dataclass
class TimingRecorder:
    clock: Callable[[], int] = perf_counter_ns
    samples: list[TimingSample] = field(default_factory=list)

    @contextmanager
    def measure(self, phase: str) -> Iterator[None]:
        start = self.clock()
        error: str | None = None
        try:
            yield
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            self.samples.append(
                TimingSample(
                    phase=phase,
                    elapsed_ns=self.clock() - start,
                    error=error,
                    scope=_ACTIVE_MEASUREMENT_SCOPE.get(),
                )
            )

    def summary(self) -> dict[str, dict[str, float | int]]:
        grouped: dict[str, list[TimingSample]] = {}
        for sample in self.samples:
            grouped.setdefault(sample.phase, []).append(sample)

        out: dict[str, dict[str, float | int]] = {}
        for phase, samples in grouped.items():
            durations = [sample.elapsed_ns for sample in samples]
            total_ns = sum(durations)
            out[phase] = {
                "count": len(samples),
                "failed": sum(sample.error is not None for sample in samples),
                "total_ms": total_ns / 1_000_000,
                "average_ms": total_ns / len(samples) / 1_000_000,
                "max_ms": max(durations) / 1_000_000,
            }
        return out

    def format_summary(self) -> list[str]:
        lines = ["timing summary (nested phases may overlap):"]
        summary = self.summary()
        for phase in sorted(summary):
            item = summary[phase]
            lines.append(
                f"  {phase:<24} count={item['count']:>3} "
                f"total={item['total_ms']:>9.2f} ms "
                f"avg={item['average_ms']:>8.2f} ms "
                f"max={item['max_ms']:>8.2f} ms "
                f"failed={item['failed']}"
            )
        return lines

    def records(self) -> list[dict[str, Any]]:
        return [
            {
                "phase": sample.phase,
                "elapsedNs": sample.elapsed_ns,
                "error": sample.error,
                "scope": dict(sample.scope),
            }
            for sample in self.samples
        ]


@dataclass
class OperationRecorder:
    counts: dict[tuple[str, tuple[tuple[str, str], ...]], int] = field(
        default_factory=dict
    )
    facts: dict[str, Any] = field(default_factory=dict)

    def add(self, name: str, value: int = 1, **labels: Any) -> None:
        if value < 0:
            raise ValueError(f"operation metric {name!r} cannot add a negative value")
        merged = dict(_ACTIVE_MEASUREMENT_SCOPE.get())
        merged.update(
            {
                key: str(label)
                for key, label in labels.items()
                if label is not None
            }
        )
        key = (name, tuple(sorted(merged.items())))
        self.counts[key] = self.counts.get(key, 0) + value

    def set_fact(self, name: str, value: Any) -> None:
        self.facts[name] = value

    def records(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "labels": dict(labels),
                "value": value,
            }
            for (name, labels), value in sorted(
                self.counts.items(),
                key=lambda item: (item[0][0], item[0][1]),
            )
        ]


_ACTIVE_TIMINGS: ContextVar[TimingRecorder | None] = ContextVar(
    "active_timings",
    default=None,
)
_ACTIVE_OPERATIONS: ContextVar[OperationRecorder | None] = ContextVar(
    "active_operations",
    default=None,
)
_ACTIVE_MEASUREMENT_SCOPE: ContextVar[tuple[tuple[str, str], ...]] = ContextVar(
    "active_measurement_scope",
    default=(),
)


@contextmanager
def use_timings(recorder: TimingRecorder | None) -> Iterator[None]:
    token = _ACTIVE_TIMINGS.set(recorder)
    try:
        yield
    finally:
        _ACTIVE_TIMINGS.reset(token)


@contextmanager
def use_operations(recorder: OperationRecorder | None) -> Iterator[None]:
    token = _ACTIVE_OPERATIONS.set(recorder)
    try:
        yield
    finally:
        _ACTIVE_OPERATIONS.reset(token)


@contextmanager
def measurement_scope(**labels: Any) -> Iterator[None]:
    merged = dict(_ACTIVE_MEASUREMENT_SCOPE.get())
    merged.update(
        {
            key: str(value)
            for key, value in labels.items()
            if value is not None
        }
    )
    token = _ACTIVE_MEASUREMENT_SCOPE.set(tuple(sorted(merged.items())))
    try:
        yield
    finally:
        _ACTIVE_MEASUREMENT_SCOPE.reset(token)


def count_operation(name: str, value: int = 1, **labels: Any) -> None:
    recorder = _ACTIVE_OPERATIONS.get()
    if recorder is not None:
        recorder.add(name, value, **labels)


def record_fact(name: str, value: Any) -> None:
    recorder = _ACTIVE_OPERATIONS.get()
    if recorder is not None:
        recorder.set_fact(name, value)


@contextmanager
def timed_phase(phase: str) -> Iterator[None]:
    recorder = _ACTIVE_TIMINGS.get()
    if recorder is None:
        yield
        return
    with recorder.measure(phase):
        yield


def timed_function(phase: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with timed_phase(phase):
                return func(*args, **kwargs)

        return wrapper

    return decorator


class ExternalProcessFailed(RuntimeError):
    def __init__(self, tool: str, result: subprocess.CompletedProcess[str]):
        super().__init__(f"{tool} exited with code {result.returncode}")
        self.tool = tool
        self.result = result


def run_external_process(
    cmd: list[str],
    *,
    tool: str,
    timing_phase: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    count_operation("process.attempts", tool=tool)
    try:
        with timed_phase(timing_phase):
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise ExternalProcessFailed(tool, result)
    except BaseException:
        count_operation("process.failures", tool=tool)
        raise
    count_operation("process.successes", tool=tool)
    return result


def log_process_output(
    result: subprocess.CompletedProcess[str],
    prefix: str,
) -> None:
    if result.stdout.strip():
        log(f"stdout: {result.stdout.strip()}", prefix)
    if result.stderr.strip():
        log(f"stderr: {result.stderr.strip()}", prefix)


@dataclass(frozen=True)
class LocalSkin:
    champion_name: str
    champion_id: int
    skin_number: int
    display_name: str
    base_display_name: str
    internal_name: str
    skinline: str
    parent_skin_number: int | None
    is_chroma: bool
    aliases: tuple[str, ...]

    @property
    def full_skin_id(self) -> int:
        """Return Riot's stable full skin ID for this local skin entry."""
        return self.champion_id * 1000 + self.skin_number

    @property
    def parent_full_skin_id(self) -> int | None:
        """Return the base skin ID inherited by a chroma, when applicable."""
        if self.parent_skin_number is None:
            return None
        return self.champion_id * 1000 + self.parent_skin_number


@dataclass
class ArchivePlan:
    skin: LocalSkin
    source_wad: Path
    work_dir: Path
    output_dir: Path
    disk_name: str
    display_name: str
    version: str
    wad_name: str
    requested_extensions: tuple[str, ...]
    info: dict[str, Any]
    layout_fingerprint: str | None = None
    ritobin_identity: ToolIdentity | None = None
    wad_make_identity: ToolIdentity | None = None

    def path_for(self, extension: str) -> Path:
        return self.output_dir / f"{self.disk_name}.{extension}"


@dataclass
class ArchivePreflight:
    pending: list[ArchivePlan] = field(default_factory=list)
    cache_hits: list[ArchivePlan] = field(default_factory=list)
    materialized: list[ArchivePlan] = field(default_factory=list)


@dataclass(frozen=True)
class BatchedBaseWork:
    key: BaseParseKey
    source_bin: Path
    parsed_json: Path
    batch_item: RitobinBatchItem


@dataclass(frozen=True)
class BatchedUnitWork:
    plan: ArchivePlan
    unit: str
    base_key: BaseParseKey
    target_bin: Path
    target_json: Path
    modified_json: Path
    final_bin: Path
    bin_to_json_item: RitobinBatchItem


@dataclass(frozen=True)
class LocalCatalog:
    champion_name: str
    wad_path: Path
    identity: ChampionIdentity
    main_unit: str
    skins: tuple[LocalSkin, ...]


@dataclass(frozen=True)
class ChampionPrepareSession:
    identity: ChampionIdentity
    source_wad: Path
    source_identity: WadFileIdentity
    prepared: PreparedChampionWad | None
    layout: ChampionLayout | None
    skin_layouts: Mapping[int, SkinLayout]
    required_chunks: Mapping[str, RequiredChunkIdentity]
    layout_fingerprints: Mapping[int, str]
    runtime_session: ChampionRuntimeSession | None = None

    @property
    def backend(self) -> str:
        return "direct" if self.prepared is not None else "legacy"


@dataclass
class ChampionRuntimeSession:
    """Champion-scoped Catalog/Prepare state shared for one CLI run."""

    identity: ChampionIdentity
    source_wad: Path
    source_identity: WadFileIdentity
    toc_digest: str
    lcu_generation: tuple[LcuWadGenerationEntry, ...]
    requested_mode: str
    backend: str
    prepared: PreparedChampionWad | None
    available_path_hashes: frozenset[int]
    hash_skin_index: HashSkinIndex | None = None
    hash_skin_candidates: HashSkinCandidateSet | None = None
    catalog: LocalCatalog | None = None
    main_skin_records: tuple[HashSkinRecord, ...] = ()
    legacy_temp: Any | None = None
    legacy_extracted_root: Path | None = None
    persistent_cache: PersistentJsonCache | None = None

    def close(self) -> None:
        if self.legacy_temp is not None:
            self.legacy_temp.cleanup()
            self.legacy_temp = None
            self.legacy_extracted_root = None


class ChampionSessionPool:
    """Own runtime sessions so temporary legacy trees have explicit lifetime."""

    def __init__(
        self,
        champions_dir: Path,
        wad_mode: str,
        *,
        persistent_cache: PersistentJsonCache | None = None,
        hash_skin_index: HashSkinIndex | None = None,
    ) -> None:
        if wad_mode not in WAD_MODES:
            raise ValueError(f"unsupported WAD mode: {wad_mode}")
        self.champions_dir = champions_dir
        self.wad_mode = wad_mode
        self.persistent_cache = persistent_cache
        self.hash_skin_index = hash_skin_index
        self._sessions: dict[int, ChampionRuntimeSession] = {}

    def __enter__(self) -> ChampionSessionPool:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()

    def session_for_id(
        self,
        champion_id: int,
    ) -> ChampionRuntimeSession | None:
        return self._sessions.get(champion_id)

    def get_catalog(
        self,
        champion_name: str,
        wad_path: Path,
    ) -> LocalCatalog:
        existing = next(
            (
                session
                for session in self._sessions.values()
                if session.source_wad.resolve() == wad_path.resolve()
            ),
            None,
        )
        if existing is not None:
            if existing.catalog is None:
                raise ChampionLayoutError(
                    f"champion session for {champion_name} has no catalog"
                )
            assert_runtime_session_source_current(existing)
            count_operation(
                "cache.catalog.hits",
                champion=champion_name,
                backend=existing.backend,
            )
            return existing.catalog

        last_change: WadChangedDuringRead | None = None
        for attempt in range(2):
            session: ChampionRuntimeSession | None = None
            try:
                session = create_runtime_champion_session(
                    champion_name,
                    wad_path,
                    self.wad_mode,
                    persistent_cache=self.persistent_cache,
                    hash_skin_index=self.hash_skin_index,
                )
                catalog = build_runtime_catalog(session)
                if session.identity.champion_id in self._sessions:
                    raise ChampionIdentityError(
                        f"champion id {session.identity.champion_id} "
                        "resolved to multiple runtime sessions"
                    )
                session.catalog = catalog
                self._sessions[session.identity.champion_id] = session
                return catalog
            except WadChangedDuringRead as exc:
                last_change = exc
                if session is not None:
                    session.close()
                count_operation(
                    "catalog.source_change_retries",
                    champion=champion_name,
                    attempt=attempt + 1,
                )
        assert last_change is not None
        raise last_change


@dataclass(frozen=True)
class OfficialSkinRef:
    champion_id: int
    skin_number: int
    display_name: str


@dataclass(frozen=True)
class OfficialNameCatalog:
    champion_id: int
    names_by_skin_number: Mapping[int, str]


@dataclass(frozen=True)
class LcuWadGenerationEntry:
    path: Path
    file_identity: WadFileIdentity
    version: str
    toc_digest: str


@dataclass(frozen=True)
class LcuChunkIdentity:
    path_hash: int
    offset: int
    compressed_size: int
    decompressed_size: int
    compression_type: int
    subchunk_count: int
    subchunk_index: int
    duplicated: bool
    checksum: int | None
    checksum_kind: str


@dataclass(frozen=True)
class LcuJsonSourceIdentity:
    normalized_path: str
    path_hash: int
    source_wad: LcuWadGenerationEntry
    chunk: LcuChunkIdentity
    raw_size: int
    raw_sha256: str


@dataclass(frozen=True)
class LcuJsonRecord:
    data: Any
    source: LcuJsonSourceIdentity


class LcuDataError(ValueError):
    """Local LCU data is absent, ambiguous, malformed, or unstable."""


def log(msg: str, prefix: str = "rebaser") -> None:
    print(f"[{prefix}] {msg}", flush=True)


_WINDOWS_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*]')


def sanitize_for_windows(name: str) -> str:
    # Windows forbids < > : " / \ | ? * in file/folder names. Strip them and
    # collapse runs of whitespace so "PROJECT: Sivir" -> "PROJECT Sivir".
    return re.sub(r"\s+", " ", _WINDOWS_FORBIDDEN_RE.sub("", name)).strip()


def normalize_champion_name(name: str) -> str:
    # Jarvan IV -> JarvanIV, Miss Fortune -> MissFortune, Kha'Zix -> KhaZix.
    return re.sub(r"[^0-9A-Za-z]", "", name)


def wad_client_base_name(path: Path) -> str:
    name = path.name
    if name.lower().endswith(WAD_CLIENT_SUFFIX):
        return name[:-len(WAD_CLIENT_SUFFIX)]
    return path.stem


def display_path(path: Path, base: Path = SCRIPT_DIR) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def normalize_lcu_path(path: str) -> str:
    return normalize_wad_path(path)


def lol_root_from_champions_dir(champions_dir: Path) -> Path:
    try:
        return champions_dir.resolve().parents[3]
    except IndexError:
        sys.exit(f"could not infer League root from Champions directory: {champions_dir}")


_WAD_INDEX_CACHE: dict[WadFileIdentity, WadIndex] = {}
_LCU_JSON_CACHE: dict[
    tuple[tuple[LcuWadGenerationEntry, ...], str],
    LcuJsonRecord,
] = {}
_OFFICIAL_NAME_CACHE: dict[
    tuple[LcuJsonSourceIdentity, int],
    OfficialNameCatalog,
] = {}
_OFFICIAL_SKIN_INDEX_CACHE: dict[
    LcuJsonSourceIdentity,
    Mapping[str, OfficialSkinRef],
] = {}
_CHAMPION_IDENTITIES_CACHE: dict[
    tuple[LcuJsonSourceIdentity, LcuJsonSourceIdentity],
    tuple[ChampionIdentity, ...],
] = {}


def lcu_path_hash(path: str) -> int:
    return wad_path_hash(path)


def read_exact(f: Any, size: int, what: str) -> bytes:
    data = f.read(size)
    if len(data) != size:
        raise ValueError(f"unexpected end of WAD while reading {what}")
    return data


def load_cached_wad_index(wad_path: Path) -> WadIndex:
    count_operation("wad.index.requests")
    identity = capture_wad_file_identity(wad_path)
    cached = _WAD_INDEX_CACHE.get(identity)
    if cached is not None:
        count_operation("wad.index.cache_hits")
        return cached
    count_operation("wad.index.cache_misses")

    stale_keys = [
        key
        for key in _WAD_INDEX_CACHE
        if key.resolved_path == identity.resolved_path
    ]
    for stale_key in stale_keys:
        del _WAD_INDEX_CACHE[stale_key]

    index = parse_wad_index_core(wad_path)
    _WAD_INDEX_CACHE[index.file_identity] = index
    count_operation("wad.index.builds")
    return index


@timed_function("wad.index")
def parse_wad_index(wad_path: Path) -> Mapping[int, WadChunk]:
    return load_cached_wad_index(wad_path).chunks_by_hash


def decompress_wad_chunk(
    raw: bytes,
    chunk: WadChunk,
    *,
    wad_path: Path | None = None,
) -> bytes:
    return decode_wad_chunk_core(
        raw,
        chunk,
        wad_path=wad_path or Path("<memory>"),
    )


@timed_function("wad.chunk_read")
def read_wad_chunk(wad_path: Path, path_hash: int) -> bytes | None:
    count_operation("wad.chunk.probes")
    raw: bytes | None = None
    chunk: WadChunk | None = None
    for attempt in range(2):
        chunks = parse_wad_index(wad_path)
        try:
            current = capture_wad_file_identity(wad_path)
        except OSError as exc:
            raise WadChangedDuringRead(wad_path, None, None) from exc
        index = _WAD_INDEX_CACHE.get(current)
        if index is None:
            error = WadChangedDuringRead(wad_path, None, current)
        else:
            expected = index.file_identity
            try:
                if index.chunks_by_hash is not chunks:
                    raise WadChangedDuringRead(wad_path, expected, current)

                chunk = chunks.get(path_hash)
                if chunk is None:
                    ending = capture_wad_file_identity(wad_path)
                    if ending != expected:
                        raise WadChangedDuringRead(wad_path, expected, ending)
                    count_operation("wad.chunk.missing")
                    return None

                count_operation("wad.chunk.found")
                try:
                    preflight_wad_chunk(chunk, wad_path=wad_path)
                except WadError as exc:
                    actual = capture_wad_file_identity(wad_path)
                    if actual == expected:
                        raise
                    raise WadChangedDuringRead(
                        wad_path,
                        expected,
                        actual,
                    ) from exc

                with wad_path.open("rb") as f:
                    opened = WadFileIdentity.from_stat(
                        expected.resolved_path,
                        os.fstat(f.fileno()),
                    )
                    if opened != expected:
                        raise WadChangedDuringRead(wad_path, expected, opened)
                    f.seek(chunk.offset)
                    raw = read_exact(
                        f,
                        chunk.compressed_size,
                        f"chunk {path_hash:016x}",
                    )
                    ending_handle = WadFileIdentity.from_stat(
                        expected.resolved_path,
                        os.fstat(f.fileno()),
                    )
                    ending_path = capture_wad_file_identity(wad_path)
                    if ending_handle != expected or ending_path != expected:
                        raise WadChangedDuringRead(
                            wad_path,
                            expected,
                            ending_path,
                        )
                break
            except WadChangedDuringRead as exc:
                error = exc
            except WadError:
                raise
            except OSError as exc:
                try:
                    actual = capture_wad_file_identity(wad_path)
                except OSError:
                    actual = None
                error = WadChangedDuringRead(wad_path, expected, actual)
                error.__cause__ = exc
            except ValueError as exc:
                try:
                    actual = capture_wad_file_identity(wad_path)
                except OSError:
                    actual = None
                if actual == expected:
                    raise CorruptWad(wad_path, str(exc)) from exc
                error = WadChangedDuringRead(wad_path, expected, actual)
                error.__cause__ = exc

        stale_keys = [
            key
            for key in _WAD_INDEX_CACHE
            if key.resolved_path == wad_path.resolve()
        ]
        for stale_key in stale_keys:
            del _WAD_INDEX_CACHE[stale_key]
        if attempt:
            raise error
    else:
        raise AssertionError("WAD read retry loop exhausted")

    assert chunk is not None and raw is not None
    count_operation("wad.chunk.physical_reads")
    count_operation("wad.chunk.compressed_bytes", len(raw))
    count_operation(
        "wad.chunk.decode_attempts",
        compression_type=chunk.compression_type,
    )
    try:
        data = decompress_wad_chunk(raw, chunk, wad_path=wad_path)
    except BaseException:
        count_operation(
            "wad.chunk.decode_failures",
            compression_type=chunk.compression_type,
        )
        raise
    count_operation(
        "wad.chunk.decode_successes",
        compression_type=chunk.compression_type,
    )
    count_operation("wad.chunk.decompressed_bytes", len(data))
    return data


def _lcu_game_data_dir(champions_dir: Path) -> Path:
    game_data_dir = lol_root_from_champions_dir(champions_dir) / LCU_GAME_DATA_REL
    if not game_data_dir.is_dir():
        raise LcuDataError(
            f"LoL game-data plugin directory not found: {game_data_dir}"
        )
    return game_data_dir


def capture_lcu_wad_generation(
    champions_dir: Path,
) -> tuple[LcuWadGenerationEntry, ...]:
    game_data_dir = _lcu_game_data_dir(champions_dir)
    for attempt in range(2):
        wad_paths = tuple(
            sorted(
                game_data_dir.glob("*.wad"),
                key=lambda path: path.name.casefold(),
            )
        )
        if not wad_paths:
            raise LcuDataError(
                f"no local LCU WADs found under {game_data_dir}"
            )
        entries = tuple(
            LcuWadGenerationEntry(
                path=index.file_identity.resolved_path,
                file_identity=index.file_identity,
                version=str(index.version),
                toc_digest=index.toc_digest,
            )
            for index in (
                load_cached_wad_index(wad_path)
                for wad_path in wad_paths
            )
        )
        ending_paths = tuple(
            sorted(
                game_data_dir.glob("*.wad"),
                key=lambda path: path.name.casefold(),
            )
        )
        identities_stable = all(
            capture_wad_file_identity(entry.path) == entry.file_identity
            for entry in entries
        )
        if wad_paths == ending_paths and identities_stable:
            return entries
        if attempt:
            break
    raise LcuDataError(
        f"local LCU WAD generation changed while being captured: "
        f"{game_data_dir}"
    )


def assert_lcu_generation_unchanged(
    champions_dir: Path,
    expected: tuple[LcuWadGenerationEntry, ...],
    context: str,
) -> None:
    if capture_lcu_wad_generation(champions_dir) != expected:
        raise LcuDataError(
            f"local LCU WAD generation changed while {context}"
        )


def lcu_wad_generation_document(
    generation: tuple[LcuWadGenerationEntry, ...],
) -> list[dict[str, Any]]:
    return [
        {
            "path": str(entry.path),
            "size": entry.file_identity.size,
            "modifiedNs": entry.file_identity.mtime_ns,
            "version": entry.version,
            "tocDigest": entry.toc_digest,
        }
        for entry in generation
    ]


def _lcu_chunk_identity(chunk: WadChunk) -> LcuChunkIdentity:
    return LcuChunkIdentity(
        path_hash=chunk.path_hash,
        offset=chunk.offset,
        compressed_size=chunk.compressed_size,
        decompressed_size=chunk.decompressed_size,
        compression_type=chunk.compression_type,
        subchunk_count=chunk.subchunk_count,
        subchunk_index=chunk.subchunk_index,
        duplicated=chunk.duplicated,
        checksum=chunk.checksum,
        checksum_kind=chunk.checksum_kind.value,
    )


def lcu_json_source_document(
    source: LcuJsonSourceIdentity,
) -> dict[str, Any]:
    chunk = source.chunk
    return {
        "normalizedPath": source.normalized_path,
        "pathHash": f"{source.path_hash:016x}",
        "sourceWad": {
            "path": str(source.source_wad.path),
            "size": source.source_wad.file_identity.size,
            "modifiedNs": source.source_wad.file_identity.mtime_ns,
            "version": source.source_wad.version,
            "tocDigest": source.source_wad.toc_digest,
        },
        "chunk": {
            "pathHash": f"{chunk.path_hash:016x}",
            "offset": chunk.offset,
            "compressedSize": chunk.compressed_size,
            "decompressedSize": chunk.decompressed_size,
            "compressionType": chunk.compression_type,
            "subchunkCount": chunk.subchunk_count,
            "subchunkIndex": chunk.subchunk_index,
            "duplicated": chunk.duplicated,
            "checksum": (
                None
                if chunk.checksum is None
                else f"{chunk.checksum:016x}"
            ),
            "checksumKind": chunk.checksum_kind,
        },
        "rawSize": source.raw_size,
        "rawSha256": source.raw_sha256,
    }


def read_lcu_game_data_record(
    champions_dir: Path,
    rel_path: str,
    *,
    expected_generation: tuple[LcuWadGenerationEntry, ...] | None = None,
) -> tuple[bytes, LcuJsonSourceIdentity]:
    generation = capture_lcu_wad_generation(champions_dir)
    if expected_generation is not None and generation != expected_generation:
        raise LcuDataError(
            "local LCU WAD generation differs from the pinned audit input"
        )

    normalized = normalize_lcu_path(rel_path)
    path_hash = lcu_path_hash(normalized)
    matches: list[tuple[LcuWadGenerationEntry, WadChunk]] = []
    for entry in generation:
        index = _WAD_INDEX_CACHE.get(entry.file_identity)
        if index is None:
            index = load_cached_wad_index(entry.path)
        if (
            index.file_identity != entry.file_identity
            or index.toc_digest != entry.toc_digest
        ):
            raise LcuDataError(
                f"local LCU WAD changed before reading {normalized}: "
                f"{entry.path}"
            )
        chunk = index.chunks_by_hash.get(path_hash)
        if chunk is not None:
            matches.append((entry, chunk))

    if len(matches) != 1:
        raise LcuDataError(
            f"LCU game-data path {normalized} expected exactly one WAD "
            f"source; found {len(matches)}"
        )

    source_wad, chunk = matches[0]
    data = read_wad_chunk(source_wad.path, path_hash)
    ending_generation = capture_lcu_wad_generation(champions_dir)
    if ending_generation != generation:
        raise LcuDataError(
            f"local LCU WAD generation changed while reading {normalized}"
        )
    if data is None:
        raise LcuDataError(
            f"LCU game-data path disappeared while reading {normalized}"
        )
    source = LcuJsonSourceIdentity(
        normalized_path=normalized,
        path_hash=path_hash,
        source_wad=source_wad,
        chunk=_lcu_chunk_identity(chunk),
        raw_size=len(data),
        raw_sha256=hashlib.sha256(data).hexdigest(),
    )
    return data, source


def _reject_duplicate_lcu_json_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LcuDataError(f"duplicate key in local LCU JSON: {key!r}")
        result[key] = value
    return result


def _parse_lcu_json_bytes(raw: bytes, normalized: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_reject_duplicate_lcu_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LcuDataError(
            f"failed parsing local LCU game-data JSON {normalized}: {exc}"
        ) from exc


def load_lcu_json_with_identity(
    champions_dir: Path,
    rel_path: str,
    *,
    expected_generation: tuple[LcuWadGenerationEntry, ...] | None = None,
) -> LcuJsonRecord:
    generation = capture_lcu_wad_generation(champions_dir)
    if expected_generation is not None and generation != expected_generation:
        raise LcuDataError(
            "local LCU WAD generation differs from the pinned audit input"
        )
    normalized = normalize_lcu_path(rel_path)
    cache_key = (generation, normalized)
    cached = _LCU_JSON_CACHE.get(cache_key)
    if cached is not None:
        return cached

    raw, source = read_lcu_game_data_record(
        champions_dir,
        normalized,
        expected_generation=generation,
    )
    data = _parse_lcu_json_bytes(raw, normalized)
    record = LcuJsonRecord(data=data, source=source)
    _LCU_JSON_CACHE[cache_key] = record
    return record


def load_official_champion_identities(
    champions_dir: Path,
) -> tuple[ChampionIdentity, ...]:
    try:
        generation = capture_lcu_wad_generation(champions_dir)
    except (LcuDataError, WadError, OSError) as exc:
        sys.exit(f"failed capturing local LCU inputs: {exc}")

    try:
        summary_record = load_lcu_json_with_identity(
            champions_dir,
            LCU_CHAMPION_SUMMARY_PATH,
            expected_generation=generation,
        )
        skins_record = load_lcu_json_with_identity(
            champions_dir,
            LCU_SKINS_PATH,
            expected_generation=generation,
        )
        cache_key = (summary_record.source, skins_record.source)
        cached = _CHAMPION_IDENTITIES_CACHE.get(cache_key)
        if cached is not None:
            assert_lcu_generation_unchanged(
                champions_dir,
                generation,
                "loading cached official champion identities",
            )
            return cached
        summary = summary_record.data
        skins = skins_record.data
        if not isinstance(summary, list):
            raise LcuDataError(
                f"{LCU_CHAMPION_SUMMARY_PATH} must be a JSON array"
            )
        identities = parse_official_champion_identities(summary, skins)
        if capture_lcu_wad_generation(champions_dir) != generation:
            raise LcuDataError(
                "local LCU WAD generation changed while building identities"
            )
    except (ChampionIdentityError, LcuDataError, WadError, OSError) as exc:
        sys.exit(f"failed building local official champion identities: {exc}")
    _CHAMPION_IDENTITIES_CACHE[cache_key] = identities
    return identities


def load_champion_identity(
    champion_name: str,
    champions_dir: Path,
) -> ChampionIdentity:
    try:
        return find_champion_identity(
            load_official_champion_identities(champions_dir),
            champion_name,
        )
    except ChampionIdentityError as exc:
        sys.exit(str(exc))


def _parse_official_skin_name_record(
    champion_id: int,
    raw_id: object,
    raw_name: object,
    source: str,
) -> tuple[int, int, str]:
    if (
        type(raw_id) is not int
        or raw_id < 0
        or raw_id // 1000 != champion_id
    ):
        raise ChampionIdentityError(
            f"{source} has invalid skin id {raw_id!r} for champion "
            f"{champion_id}"
        )
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ChampionIdentityError(
            f"{source} has an empty or non-string skin name"
        )
    return raw_id, raw_id % 1000, raw_name


def parse_official_name_catalog(
    champion_id: int,
    champion_data: Any,
) -> OfficialNameCatalog | None:
    if (
        type(champion_id) is not int
        or champion_id <= 0
    ):
        raise ChampionIdentityError(
            f"official champion id must be a positive integer: "
            f"{champion_id!r}"
        )
    if not isinstance(champion_data, dict):
        return None
    document_champion_id = champion_data.get("id")
    if (
        type(document_champion_id) is not int
        or document_champion_id != champion_id
    ):
        raise ChampionIdentityError(
            f"official champion document id {document_champion_id!r} "
            f"does not match {champion_id}"
        )
    skins_data = champion_data.get("skins")
    if not isinstance(skins_data, list):
        return None

    names: dict[int, str] = {}
    sources: dict[int, str] = {}

    def add_record(
        raw_id: object,
        raw_name: object,
        source: str,
        *,
        quest_parent_echo: tuple[int, str] | None = None,
    ) -> tuple[int, str]:
        full_id, skin_number, name = _parse_official_skin_name_record(
            champion_id,
            raw_id,
            raw_name,
            source,
        )
        existing = names.get(skin_number)
        if existing is not None:
            if quest_parent_echo == (full_id, name):
                return full_id, name
            raise ChampionIdentityError(
                f"duplicate official skin number {skin_number} for champion "
                f"{champion_id}: {sources[skin_number]} and {source}"
            )
        names[skin_number] = name
        sources[skin_number] = source
        return full_id, name

    for skin_index, skin_data in enumerate(skins_data):
        if not isinstance(skin_data, dict):
            raise ChampionIdentityError(
                f"official champion {champion_id} skin[{skin_index}] "
                "must be a JSON object"
            )
        skin_source = f"skin[{skin_index}]"
        parent_record = add_record(
            skin_data.get("id"),
            skin_data.get("name"),
            skin_source,
        )

        quest = skin_data.get("questSkinInfo")
        if quest is not None and not isinstance(quest, dict):
            raise ChampionIdentityError(
                f"{skin_source}.questSkinInfo must be an object or null"
            )
        tiers = quest.get("tiers") if isinstance(quest, dict) else None
        if tiers is not None:
            if not isinstance(tiers, list):
                raise ChampionIdentityError(
                    f"{skin_source}.questSkinInfo.tiers must be an array"
                )
            parent_echo_available = True
            for tier_index, tier_data in enumerate(tiers):
                if not isinstance(tier_data, dict):
                    raise ChampionIdentityError(
                        f"{skin_source}.questSkinInfo.tiers[{tier_index}] "
                        "must be a JSON object"
                    )
                tier_source = (
                    f"{skin_source}.questSkinInfo.tiers[{tier_index}]"
                )
                tier_id = tier_data.get("id")
                tier_name = tier_data.get("name")
                allow_echo = (
                    parent_record
                    if parent_echo_available
                    and (tier_id, tier_name) == parent_record
                    else None
                )
                add_record(
                    tier_id,
                    tier_name,
                    tier_source,
                    quest_parent_echo=allow_echo,
                )
                if allow_echo is not None:
                    parent_echo_available = False

        chromas = skin_data.get("chromas")
        if chromas is None:
            continue
        if not isinstance(chromas, list):
            raise ChampionIdentityError(
                f"{skin_source}.chromas must be an array or null"
            )
        for chroma_index, chroma_data in enumerate(chromas):
            if not isinstance(chroma_data, dict):
                raise ChampionIdentityError(
                    f"{skin_source}.chromas[{chroma_index}] must be a "
                    "JSON object"
                )
            add_record(
                chroma_data.get("id"),
                chroma_data.get("name"),
                f"{skin_source}.chromas[{chroma_index}]",
            )
    return OfficialNameCatalog(
        champion_id=champion_id,
        names_by_skin_number=MappingProxyType(dict(names)),
    )


def load_official_name_catalog(
    champion_name: str,
    champions_dir: Path,
) -> OfficialNameCatalog:
    try:
        generation = capture_lcu_wad_generation(champions_dir)
        identity = load_champion_identity(champion_name, champions_dir)
        rel_path = (
            "plugins/rcp-be-lol-game-data/global/default/v1/champions/"
            f"{identity.champion_id}.json"
        )
        record = load_lcu_json_with_identity(
            champions_dir,
            rel_path,
            expected_generation=generation,
        )
        cache_key = (record.source, identity.champion_id)
        cached = _OFFICIAL_NAME_CACHE.get(cache_key)
        if cached is not None:
            assert_lcu_generation_unchanged(
                champions_dir,
                generation,
                f"loading cached champion {identity.champion_id} names",
            )
            return cached
        catalog = parse_official_name_catalog(
            identity.champion_id,
            record.data,
        )
        if catalog is None:
            raise ChampionIdentityError(
                f"local LCU champion document is malformed: {rel_path}"
            )
        if capture_lcu_wad_generation(champions_dir) != generation:
            raise LcuDataError(
                "local LCU WAD generation changed while loading "
                f"champion {identity.champion_id}"
            )
    except (
        ChampionIdentityError,
        LcuDataError,
        WadError,
        OSError,
    ) as exc:
        sys.exit(
            f"local LCU game-data champion file malformed for "
            f"{champion_name}: {exc}"
        )
    log(
        f"loaded official names for {champion_name} from local LCU "
        f"game-data id {identity.champion_id}"
    )
    _OFFICIAL_NAME_CACHE[cache_key] = catalog
    return catalog


def load_official_skin_index(
    champions_dir: Path,
) -> Mapping[str, OfficialSkinRef]:
    rel_path = "plugins/rcp-be-lol-game-data/global/default/v1/skins.json"
    try:
        generation = capture_lcu_wad_generation(champions_dir)
        record = load_lcu_json_with_identity(
            champions_dir,
            rel_path,
            expected_generation=generation,
        )
    except (LcuDataError, WadError, OSError) as exc:
        sys.exit(f"failed loading local LCU skin index: {exc}")
    cached = _OFFICIAL_SKIN_INDEX_CACHE.get(record.source)
    if cached is not None:
        try:
            assert_lcu_generation_unchanged(
                champions_dir,
                generation,
                "loading cached official skin index",
            )
        except (LcuDataError, WadError, OSError) as exc:
            sys.exit(f"failed loading local LCU skin index: {exc}")
        return cached
    skins_data = record.data
    if not isinstance(skins_data, dict):
        sys.exit(f"local LCU game-data {rel_path} must be a JSON object")

    index: dict[str, OfficialSkinRef] = {}
    official_ids = {
        identity.champion_id
        for identity in load_official_champion_identities(champions_dir)
    }
    for raw_skin_id, skin_data in skins_data.items():
        if not isinstance(skin_data, dict):
            sys.exit(
                f"local LCU skins.json entry {raw_skin_id!r} must be an "
                "object"
            )
        skin_id = skin_data.get("id")
        skin_name = skin_data.get("name")
        if (
            type(skin_id) is not int
            or skin_id < 0
            or str(skin_id) != raw_skin_id
        ):
            sys.exit(
                f"local LCU skins.json entry {raw_skin_id!r} has invalid "
                f"id {skin_id!r}"
            )
        if not isinstance(skin_name, str) or not skin_name.strip():
            sys.exit(
                f"local LCU skins.json entry {raw_skin_id!r} has an "
                "invalid name"
            )
        champion_id = skin_id // 1000
        if champion_id not in official_ids:
            continue
        ref = OfficialSkinRef(
            champion_id=champion_id,
            skin_number=skin_id % 1000,
            display_name=skin_name,
        )
        key = normalize_display_name(skin_name)
        existing = index.get(key)
        if existing is not None:
            if (existing.champion_id, existing.skin_number) != (ref.champion_id, ref.skin_number):
                sys.exit(f"duplicate official skin name in local LCU skins.json: {skin_name!r}")
            continue
        index[key] = ref

    if capture_lcu_wad_generation(champions_dir) != generation:
        sys.exit("local LCU WAD generation changed while building skin index")
    frozen_index = MappingProxyType(dict(index))
    _OFFICIAL_SKIN_INDEX_CACHE[record.source] = frozen_index
    return frozen_index


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def ensure_lol_path() -> Path:
    """Return the LoL Champions directory, prompting and persisting if needed."""
    cfg = load_config()
    lol_root = cfg.get("lol_path")

    if lol_root:
        champions_dir = Path(lol_root) / LOL_CHAMPIONS_REL
        if champions_dir.is_dir():
            return champions_dir
        log(f"warning: saved lol_path no longer valid: {lol_root}")

    while True:
        raw = input(
            "Enter League of Legends path (e.g. C:\\Riot Games\\League of Legends): "
        ).strip().strip('"')
        if not raw:
            continue
        champions_dir = Path(raw) / LOL_CHAMPIONS_REL
        if champions_dir.is_dir():
            cfg["lol_path"] = raw
            save_config(cfg)
            log(f"saved LoL path to {CONFIG_PATH.name}")
            return champions_dir
        log(
            f"Champions directory not found: {champions_dir}\n"
            f"Make sure the path contains Game\\DATA\\FINAL\\Champions"
        )


_STEP_DIR_NAMES = {"step1", "step2", "step3", "step4"}


def find_input_units(skin_dir: Path, only_units: set[str] | None = None) -> list[tuple[Path, Path, str]]:
    # Each non-step subfolder is one unit (main champion or a summon model);
    # returns (base_bin, target_bin, unit_name) per unit, sorted by unit name.
    unit_dirs = [
        p for p in skin_dir.iterdir()
        if p.is_dir() and p.name not in _STEP_DIR_NAMES
    ]
    if only_units is not None:
        wanted = {u.lower() for u in only_units}
        unit_dirs = [p for p in unit_dirs if p.name.lower() in wanted]
    if not unit_dirs:
        if only_units is None:
            sys.exit(
                f"no unit subfolder under {skin_dir}; expected "
                f"{skin_dir}/<Unit>/skin0.bin and skin<N>.bin"
            )
        sys.exit(f"none of the requested unit folders exist under {skin_dir}: {sorted(only_units)}")

    results: list[tuple[Path, Path, str]] = []
    for unit_dir in sorted(unit_dirs, key=lambda p: p.name):
        base: Path | None = None
        target: Path | None = None
        for p in unit_dir.iterdir():
            if not p.is_file():
                continue
            m = re.fullmatch(r"skin(\d+)\.bin", p.name, re.IGNORECASE)
            if not m:
                continue
            if int(m.group(1)) == 0:
                base = p
            else:
                if target is not None:
                    sys.exit(f"multiple non-base skin bins in {unit_dir}: {target.name}, {p.name}")
                target = p
        if base is None:
            sys.exit(f"skin0.bin not found in {unit_dir}")
        if target is None:
            sys.exit(f"skin<N>.bin (N != 0) not found in {unit_dir}")
        results.append((base, target, unit_dir.name))
    return results


def run_ritobin_recursive_conversion(
    source_dir: Path,
    destination_dir: Path,
    in_fmt: str,
    out_fmt: str,
) -> None:
    if not RITOBIN_CLI.exists():
        raise RitobinBatchError(
            f"ritobin_cli.exe not found at {RITOBIN_CLI}"
        )
    cmd = [
        str(RITOBIN_CLI),
        "-r",
        "-i",
        in_fmt,
        "-o",
        out_fmt,
        str(source_dir),
        str(destination_dir),
    ]
    log(f"$ {' '.join(cmd)}", "ritobin-batch")
    count_operation(
        "ritobin.batch.calls",
        in_format=in_fmt,
        out_format=out_fmt,
    )
    try:
        result = run_external_process(
            cmd,
            tool="ritobin-recursive",
            timing_phase=f"ritobin.batch_{in_fmt}_to_{out_fmt}",
        )
    except ExternalProcessFailed as exc:
        log_process_output(exc.result, "ritobin-batch")
        raise
    log_process_output(result, "ritobin-batch")


def diagnose_ritobin_batch_item(
    item: RitobinBatchItem,
    in_fmt: str,
    out_fmt: str,
) -> str | None:
    count_operation(
        "ritobin.batch.diagnostic_calls",
        in_format=in_fmt,
        out_format=out_fmt,
    )
    with tempfile.TemporaryDirectory(
        prefix=".ritobin-diagnostic-",
        dir=INPUT_ROOT,
    ) as temp_name:
        output = Path(temp_name) / f"output.{out_fmt}"
        cmd = [
            str(RITOBIN_CLI),
            "-i",
            in_fmt,
            "-o",
            out_fmt,
            str(item.source),
            str(output),
        ]
        try:
            result = run_external_process(
                cmd,
                tool="ritobin-diagnostic",
                timing_phase=f"ritobin.diagnostic_{in_fmt}_to_{out_fmt}",
            )
        except ExternalProcessFailed as exc:
            count_operation(
                "ritobin.batch.diagnostic_failures",
                in_format=in_fmt,
                out_format=out_fmt,
            )
            tail = "\n".join(
                (exc.result.stdout + exc.result.stderr).splitlines()[-10:]
            )
            return f"exit {exc.result.returncode}: {tail}"
        if not output.is_file():
            count_operation(
                "ritobin.batch.diagnostic_failures",
                in_format=in_fmt,
                out_format=out_fmt,
            )
            tail = "\n".join(
                (result.stdout + result.stderr).splitlines()[-10:]
            )
            return f"missing output: {tail}"
        try:
            if out_fmt == "json":
                json.loads(output.read_text(encoding="utf-8"))
            elif out_fmt == "bin":
                validate_bin_payload(
                    output.read_bytes(),
                    item.relative_path,
                )
        except (OSError, UnicodeError, json.JSONDecodeError, WadError) as exc:
            count_operation(
                "ritobin.batch.diagnostic_failures",
                in_format=in_fmt,
                out_format=out_fmt,
            )
            return f"invalid output: {exc}"
    return None


def run_ritobin_batches(
    items: list[RitobinBatchItem],
    *,
    in_fmt: str,
    out_fmt: str,
) -> None:
    count_operation(
        "ritobin.batch.files",
        len(items),
        in_format=in_fmt,
        out_format=out_fmt,
    )
    count_operation(
        "ritobin.batch.input_bytes",
        sum(item.source.stat().st_size for item in items),
        in_format=in_fmt,
        out_format=out_fmt,
    )
    report = run_bounded_recursive_batches(
        items,
        in_format=in_fmt,
        out_format=out_fmt,
        workspace=INPUT_ROOT,
        max_files=RITOBIN_BATCH_MAX_FILES,
        max_bytes=RITOBIN_BATCH_MAX_INPUT_BYTES,
        isolate_above_bytes=RITOBIN_BATCH_ISOLATE_ABOVE_BYTES,
        run_batch=run_ritobin_recursive_conversion,
        diagnose=diagnose_ritobin_batch_item,
    )
    count_operation(
        "ritobin.batch.completed_batches",
        report.batch_count,
        in_format=in_fmt,
        out_format=out_fmt,
    )
    count_operation(
        "ritobin.batch.max_batch_files",
        report.max_batch_files,
        in_format=in_fmt,
        out_format=out_fmt,
    )
    count_operation(
        "ritobin.batch.max_batch_input_bytes",
        report.max_batch_input_bytes,
        in_format=in_fmt,
        out_format=out_fmt,
    )


def run_wad_make(src_dir: Path, dst_wad: Path) -> None:
    if not WAD_MAKE.exists():
        sys.exit(f"wad-make.exe not found at {WAD_MAKE}")
    cmd = [str(WAD_MAKE), str(src_dir), str(dst_wad)]
    log(f"$ {' '.join(cmd)}", "wad-make")
    try:
        result = run_external_process(
            cmd,
            tool="wad-make",
            timing_phase="wad.make",
        )
    except ExternalProcessFailed as exc:
        log_process_output(exc.result, "wad-make")
        sys.exit(f"wad-make exited with code {exc.result.returncode}")
    log_process_output(result, "wad-make")


SKINLINE_NAMES: dict[str, str] = {
    "academy": "Academy",
    "arcade": "Arcade",
    "arcana": "Arcana",
    "battleacademia": "Battle Academia",
    "bloodmoon": "Blood Moon",
    "cafecuties": "Cafe Cuties",
    "coven": "Coven",
    "elderwood": "Elderwood",
    "frightnight": "Fright Night",
    "highnoon": "High Noon",
    "kda": "K/DA",
    "kdaallout": "K/DA ALL OUT",
    "lunar_b": "Lunar Beast",
    "lunar": "Lunar",
    "mythmaker": "Mythmaker",
    "odyssey": "Odyssey",
    "primalambush": "Primal Ambush",
    "project": "PROJECT:",
    "snowmoon": "Snow Moon",
    "solar_eclipse": "Solar Eclipse",
    "spiritblossom": "Spirit Blossom",
    "starguardian": "Star Guardian",
    "winterblessed": "Winterblessed",
}


def normalize_lookup(name: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", name.lower())


def normalize_display_name(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().casefold()


def normalize_display_words(name: str) -> str:
    return re.sub(r"[^0-9a-z]+", " ", name.casefold()).strip()


def display_name_contains_words(display_name: str, words: str) -> bool:
    if not words:
        return False
    normalized = normalize_display_words(display_name)
    pattern = rf"(?<![0-9a-z]){re.escape(words)}(?![0-9a-z])"
    return re.search(pattern, normalized) is not None


def parenthesized_base_name(name: str) -> str | None:
    match = re.fullmatch(r"(.+?)\s+\([^()]+\)", name.strip())
    return match.group(1) if match else None


def list_champion_wads(champions_dir: Path) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for wad_path in champions_dir.iterdir():
        if not wad_path.is_file() or not wad_path.name.lower().endswith(WAD_CLIENT_SUFFIX):
            continue
        name = wad_client_base_name(wad_path)
        # Locale WADs such as Ahri.en_US.wad.client do not contain gameplay skin metadata.
        if "." in name:
            continue
        out.append((name, wad_path))
    return sorted(out, key=lambda x: x[0].lower())


def find_champion_wad_by_names(
    names: list[str],
    champions_dir: Path,
) -> tuple[str, Path] | None:
    wanted = {
        normalize_champion_name(name).lower()
        for name in names
        if name
    }
    matches = [
        (name, wad_path)
        for name, wad_path in list_champion_wads(champions_dir)
        if normalize_champion_name(name).lower() in wanted
    ]
    if len(matches) > 1:
        sys.exit(f"champion name is ambiguous: {[name for name, _ in matches]}")
    return matches[0] if matches else None


def find_exact_identity_wad(
    identity: ChampionIdentity,
    champions_dir: Path,
) -> tuple[str, Path]:
    expected_name = f"{identity.wad_base}{WAD_CLIENT_SUFFIX}"
    matches = [
        path
        for path in champions_dir.iterdir()
        if path.is_file()
        and path.name.casefold() == expected_name.casefold()
    ]
    if len(matches) != 1:
        sys.exit(
            f"official champion id {identity.champion_id} expects exactly "
            f"one {expected_name}; found {len(matches)} under "
            f"{champions_dir}"
        )
    wad_path = matches[0]
    return wad_client_base_name(wad_path), wad_path


def find_champion_wad(champion_name: str, champions_dir: Path) -> tuple[str, Path]:
    identity = load_champion_identity(champion_name, champions_dir)
    return find_exact_identity_wad(identity, champions_dir)


def find_official_champion_wad(champion_id: int, champions_dir: Path) -> tuple[str, Path]:
    matches = [
        identity
        for identity in load_official_champion_identities(champions_dir)
        if identity.champion_id == champion_id
    ]
    if len(matches) != 1:
        sys.exit(f"official champion summary not found for id {champion_id}")
    return find_exact_identity_wad(matches[0], champions_dir)


def find_official_skin_ref_in_catalog(
    catalog: OfficialNameCatalog,
    skin_name: str,
) -> OfficialSkinRef | None:
    key = normalize_display_name(skin_name)
    matches = [
        OfficialSkinRef(
            champion_id=catalog.champion_id,
            skin_number=skin_number,
            display_name=official_name,
        )
        for skin_number, official_name in catalog.names_by_skin_number.items()
        if normalize_display_name(official_name) == key
    ]
    if len(matches) > 1:
        sys.exit(f"official skin name is ambiguous in local LCU game-data: {skin_name!r}")
    return matches[0] if matches else None


def candidate_official_champion_ids_for_skin_name(
    skin_name: str,
    champions_dir: Path,
) -> list[int]:
    ids: list[int] = []
    for identity in load_official_champion_identities(champions_dir):
        if any(
            display_name_contains_words(skin_name, normalize_display_words(name))
            for name in (identity.alias, identity.display_name)
        ):
            ids.append(identity.champion_id)
    return ids


def resolve_official_skin_ref(
    skin_name: str,
    champions_dir: Path,
) -> OfficialSkinRef | None:
    if re.fullmatch(r"[0-9]+", skin_name):
        invalid_id = f"skin ID must be a canonical positive decimal: {skin_name!r}"
        try:
            full_skin_id = int(skin_name)
        except ValueError:
            sys.exit(invalid_id)
        if full_skin_id <= 0 or str(full_skin_id) != skin_name:
            sys.exit(invalid_id)
        champion_id, skin_number = divmod(full_skin_id, 1000)
        champion_name, _wad_path = find_official_champion_wad(
            champion_id,
            champions_dir,
        )
        official_catalog = load_official_name_catalog(
            champion_name,
            champions_dir,
        )
        display_name = official_catalog.names_by_skin_number.get(skin_number)
        if display_name is None:
            sys.exit(
                "official full skin ID not found in local LCU game-data: "
                f"{full_skin_id}"
            )
        return OfficialSkinRef(champion_id, skin_number, display_name)

    skin_index = load_official_skin_index(champions_dir)
    key = normalize_display_name(skin_name)
    ref = skin_index.get(key)
    if ref is not None:
        return ref

    base_name = parenthesized_base_name(skin_name)
    if base_name is not None:
        base_ref = skin_index.get(normalize_display_name(base_name))
        if base_ref is not None:
            champion_name, _wad_path = find_official_champion_wad(base_ref.champion_id, champions_dir)
            official_catalog = load_official_name_catalog(champion_name, champions_dir)
            ref = find_official_skin_ref_in_catalog(official_catalog, skin_name)
            if ref is not None:
                return ref

    matches: list[OfficialSkinRef] = []
    for champion_id in candidate_official_champion_ids_for_skin_name(skin_name, champions_dir):
        champion_name, _wad_path = find_official_champion_wad(champion_id, champions_dir)
        official_catalog = load_official_name_catalog(champion_name, champions_dir)
        ref = find_official_skin_ref_in_catalog(official_catalog, skin_name)
        if ref is not None:
            matches.append(ref)

    if len(matches) > 1:
        sys.exit(f"official skin name is ambiguous in local LCU game-data: {skin_name!r}")
    return matches[0] if matches else None


def infer_champion_from_skin_name(skin_name: str, champions_dir: Path) -> tuple[str, Path]:
    candidates = [skin_name]
    base_name = parenthesized_base_name(skin_name)
    if base_name:
        candidates.append(base_name)

    for candidate in candidates:
        needle = normalize_lookup(candidate)
        wad_matches = [
            (name, wad_path)
            for name, wad_path in list_champion_wads(champions_dir)
            if needle.endswith(normalize_lookup(name))
        ]
        if len(wad_matches) == 1:
            return wad_matches[0]
        if len(wad_matches) > 1:
            wad_matches.sort(key=lambda x: len(normalize_lookup(x[0])), reverse=True)
            return wad_matches[0]

        official_matches: list[tuple[str, Path]] = []
        for identity in load_official_champion_identities(champions_dir):
            names = (identity.alias, identity.display_name)
            if not any(
                needle.endswith(normalize_lookup(name))
                for name in names
            ):
                continue
            wad = find_champion_wad_by_names(list(names), champions_dir)
            if wad:
                official_matches.append(wad)
        if len(official_matches) == 1:
            return official_matches[0]
        if len(official_matches) > 1:
            official_matches.sort(key=lambda x: len(normalize_lookup(x[0])), reverse=True)
            return official_matches[0]
    sys.exit(
        f"could not infer champion from skin name {skin_name!r}. "
        "Use champion mode, the exact official skin name, or `skin<N> <Champion>`."
    )


def extract_skinline(tags: str) -> str:
    for token in tags.split(","):
        if token.lower().startswith("skinline:"):
            return token.split(":", 1)[1].strip().lower()
    return ""


def pretty_skinline(skinline: str) -> str:
    if not skinline:
        return ""
    if skinline in SKINLINE_NAMES:
        return SKINLINE_NAMES[skinline]
    spaced = re.sub(r"[_-]+", " ", skinline).strip()
    return spaced.title()


def make_base_display_name(champion_name: str, skin_number: int, skinline: str) -> str:
    if skin_number == 0:
        return champion_name
    label = pretty_skinline(skinline)
    if not label:
        return f"{champion_name} Skin {skin_number}"
    if label.endswith(":"):
        return f"{label} {champion_name}"
    return f"{label} {champion_name}"


def make_chroma_display_name(base_name: str, skin_number: int, chroma_index: int) -> str:
    return f"{base_name} (Chroma {chroma_index}, skin{skin_number})"


def get_json_field(entry: dict[str, Any], field_name: str) -> dict[str, Any] | None:
    value = entry.get("value")
    if not isinstance(value, dict):
        return None
    fields = value.get("items")
    if not isinstance(fields, list):
        return None
    for field in fields:
        if isinstance(field, dict) and field.get("key") == field_name:
            return field
    return None


def json_field_value(entry: dict[str, Any], field_name: str) -> Any:
    field = get_json_field(entry, field_name)
    return field.get("value") if field else None


def run_ritobin_dir_quiet(src_dir: Path, dst_dir: Path, in_fmt: str, out_fmt: str) -> None:
    if not RITOBIN_CLI.exists():
        sys.exit(f"ritobin_cli.exe not found at {RITOBIN_CLI}")
    cmd = [str(RITOBIN_CLI), "-r", "-i", in_fmt, "-o", out_fmt, str(src_dir), str(dst_dir)]
    try:
        run_external_process(
            cmd,
            tool="ritobin-recursive",
            timing_phase="catalog.ritobin_scan",
        )
    except ExternalProcessFailed as exc:
        tail = "\n".join(
            (exc.result.stdout + exc.result.stderr).splitlines()[-30:]
        )
        sys.exit(
            "ritobin_cli recursive conversion exited with code "
            f"{exc.result.returncode}\n{tail}"
        )


def parse_skin_metadata(json_dir: Path, skin_number: int) -> dict[str, Any] | None:
    json_path = json_dir / f"skin{skin_number}.json"
    if not json_path.is_file():
        return None
    data = json.loads(json_path.read_text(encoding="utf-8"))
    try:
        skin_entry = find_json_entry(data, "SkinCharacterDataProperties", f"skin{skin_number}")
    except SystemExit:
        return None
    mesh = json_field_value(skin_entry, "SkinMeshProperties")
    return {
        "skin_number": skin_number,
        "classification": json_field_value(skin_entry, "SkinClassification"),
        "internal_name": json_field_value(skin_entry, "ChampionSkinName") or f"skin{skin_number}",
        "parent": json_field_value(skin_entry, "SkinParent"),
        "skinline": extract_skinline(json_field_value(skin_entry, "MetaDataTags") or ""),
        "skeleton": json_field_value({"value": mesh}, "Skeleton") if isinstance(mesh, dict) else None,
        "simple_skin": json_field_value({"value": mesh}, "SimpleSkin") if isinstance(mesh, dict) else None,
        "texture": json_field_value({"value": mesh}, "Texture") if isinstance(mesh, dict) else None,
    }


def make_skin_aliases(skin: LocalSkin) -> tuple[str, ...]:
    values = {
        skin.internal_name,
        f"{skin.champion_name} skin {skin.skin_number}",
        f"{skin.champion_name} skin{skin.skin_number}",
        f"skin {skin.skin_number} {skin.champion_name}",
        f"skin{skin.skin_number} {skin.champion_name}",
    }
    return tuple(sorted({normalize_lookup(v) for v in values if v}))


def extract_wad_to_temp_dir(
    wad_path: Path,
    temp_dir: Path,
    *,
    purpose: str = "unspecified",
    wad_extract_path: Path | None = None,
    hashes_path: Path | None = None,
    expected_wad_identity: WadFileIdentity | None = None,
    expected_toc_digest: str | None = None,
) -> Path:
    extract_tool = WAD_EXTRACT if wad_extract_path is None else wad_extract_path
    if not extract_tool.exists():
        sys.exit(f"wad-extract.exe not found at {extract_tool}")
    extract_tool = extract_tool.resolve(strict=True)
    resolved_hashes: Path | None = None
    if hashes_path is not None:
        resolved_hashes = hashes_path.resolve(strict=True)
    temp_wad = temp_dir / wad_path.name
    count_operation("wad.copy.attempts", purpose=purpose)
    try:
        with timed_phase("wad.copy"):
            resolved_wad = wad_path.resolve(strict=True)
            with (
                wad_path.open("rb") as source,
                temp_wad.open("xb") as destination,
            ):
                opened = WadFileIdentity.from_stat(
                    resolved_wad,
                    os.fstat(source.fileno()),
                )
                pinned = (
                    opened
                    if expected_wad_identity is None
                    else expected_wad_identity
                )
                if opened != pinned:
                    raise WadChangedDuringRead(
                        wad_path,
                        pinned,
                        opened,
                    )
                shutil.copyfileobj(source, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
                ending_handle = WadFileIdentity.from_stat(
                    resolved_wad,
                    os.fstat(source.fileno()),
                )
            ending_path = capture_wad_file_identity(wad_path)
            if ending_handle != pinned or ending_path != pinned:
                raise WadChangedDuringRead(
                    wad_path,
                    pinned,
                    ending_path,
                )
            copied_index = parse_wad_index_core(temp_wad)
            if copied_index.file_identity.size != pinned.size:
                raise CorruptWad(
                    temp_wad,
                    "stable WAD snapshot size differs from the source",
                )
            if (
                expected_toc_digest is not None
                and copied_index.toc_digest != expected_toc_digest
            ):
                raise CorruptWad(
                    temp_wad,
                    "stable WAD snapshot TOC differs from the prepared source",
                )
    except BaseException:
        count_operation("wad.copy.failures", purpose=purpose)
        raise
    count_operation("wad.copy.successes", purpose=purpose)
    count_operation(
        "wad.copy.bytes",
        temp_wad.stat().st_size,
        purpose=purpose,
    )

    extracted_dir = temp_dir / f"{wad_client_base_name(temp_wad)}.wad"
    if resolved_hashes is None:
        cmd = [str(extract_tool), f".\\{temp_wad.name}"]
    else:
        cmd = [
            str(extract_tool),
            str(temp_wad.resolve(strict=True)),
            str(extracted_dir.resolve()),
            str(resolved_hashes),
        ]
    log(f"$ {' '.join(cmd)}  (cwd={temp_dir})", "wad-extract")
    try:
        run_external_process(
            cmd,
            tool="wad-extract",
            timing_phase="wad.extract",
            cwd=temp_dir,
        )
    except ExternalProcessFailed as exc:
        tail = "\n".join(
            (exc.result.stdout + exc.result.stderr).splitlines()[-30:]
        )
        sys.exit(
            f"wad-extract exited with code {exc.result.returncode}\n{tail}"
        )

    if not extracted_dir.is_dir():
        extracted_dirs = [p for p in temp_dir.iterdir() if p.is_dir() and p.name.endswith(".wad")]
        if len(extracted_dirs) != 1:
            sys.exit(f"could not find wad-extract output folder under {temp_dir}")
        extracted_dir = extracted_dirs[0]
    return extracted_dir


def collect_catalog_rows(
    skins_dir: Path,
    json_dir: Path,
) -> list[dict[str, Any]]:
    """Convert and parse the main unit's staged skin metadata."""

    json_dir.mkdir()
    run_ritobin_dir_quiet(skins_dir, json_dir, "bin", "json")
    rows: list[dict[str, Any]] = []
    skin_bins = sorted(
        skins_dir.glob("skin*.bin"),
        key=lambda path: (
            int(match.group(1))
            if (
                match := re.fullmatch(
                    r"skin(\d+)\.bin",
                    path.name,
                    re.IGNORECASE,
                )
            )
            else 999999
        ),
    )
    for skin_bin in skin_bins:
        match = re.fullmatch(
            r"skin(\d+)\.bin",
            skin_bin.name,
            re.IGNORECASE,
        )
        if match is None:
            continue
        metadata = parse_skin_metadata(json_dir, int(match.group(1)))
        if metadata is not None:
            rows.append(metadata)
    return rows


def build_catalog_from_metadata_rows(
    champion_name: str,
    wad_path: Path,
    identity: ChampionIdentity,
    rows: list[dict[str, Any]],
) -> LocalCatalog:
    """Apply the shared catalog/chroma semantics to parsed main-unit rows."""

    rows.sort(key=lambda row: row["skin_number"])
    by_number = {int(row["skin_number"]): row for row in rows}
    skeleton_owners: dict[str, int] = {}
    simple_skin_owners: dict[str, int] = {}
    parent_by_skin: dict[int, int | None] = {}

    for index, row in enumerate(rows):
        skin_number = int(row["skin_number"])
        parent: int | None = None
        raw_parent = row.get("parent")
        if (
            isinstance(raw_parent, int)
            and raw_parent in by_number
            and raw_parent != skin_number
        ):
            parent = raw_parent
        elif (
            isinstance(row.get("skeleton"), str)
            and row["skeleton"] in skeleton_owners
        ):
            parent = skeleton_owners[row["skeleton"]]
        elif (
            isinstance(row.get("simple_skin"), str)
            and row["simple_skin"] in simple_skin_owners
        ):
            parent = simple_skin_owners[row["simple_skin"]]
        elif row.get("classification") == 2:
            for previous in reversed(rows[:index]):
                previous_number = int(previous["skin_number"])
                if (
                    previous.get("skinline") == row.get("skinline")
                    and parent_by_skin.get(previous_number) is None
                ):
                    parent = previous_number
                    break

        parent_by_skin[skin_number] = parent
        if parent is None:
            if isinstance(row.get("skeleton"), str):
                skeleton_owners.setdefault(row["skeleton"], skin_number)
            if isinstance(row.get("simple_skin"), str):
                simple_skin_owners.setdefault(
                    row["simple_skin"],
                    skin_number,
                )

    base_names: dict[int, str] = {}
    base_counts: dict[str, int] = {}
    for row in rows:
        skin_number = int(row["skin_number"])
        if parent_by_skin[skin_number] is not None:
            continue
        name = make_base_display_name(
            champion_name,
            skin_number,
            row.get("skinline") or "",
        )
        key = normalize_lookup(name)
        base_counts[key] = base_counts.get(key, 0) + 1
        if base_counts[key] > 1:
            name = f"{name} skin{skin_number}"
        base_names[skin_number] = name

    chroma_index_by_parent: dict[int, int] = {}
    official_catalog = load_official_name_catalog(
        champion_name,
        wad_path.parent,
    )
    official_names = official_catalog.names_by_skin_number
    skins: list[LocalSkin] = []
    for row in rows:
        skin_number = int(row["skin_number"])
        parent = parent_by_skin[skin_number]
        is_chroma = parent is not None
        if is_chroma:
            assert parent is not None
            parent_official = official_names.get(parent)
            parent_name = parent_official or base_names.get(
                parent,
                make_base_display_name(champion_name, parent, ""),
            )
            chroma_index = chroma_index_by_parent.get(parent, 0) + 1
            chroma_index_by_parent[parent] = chroma_index
            official_name = official_names.get(skin_number)
            display_name = official_name or make_chroma_display_name(
                parent_name,
                skin_number,
                chroma_index,
            )
            base_display_name = parent_name
        else:
            official_name = official_names.get(skin_number)
            display_name = official_name or base_names[skin_number]
            base_display_name = display_name

        skin = LocalSkin(
            champion_name=champion_name,
            champion_id=official_catalog.champion_id,
            skin_number=skin_number,
            display_name=display_name,
            base_display_name=base_display_name,
            internal_name=str(
                row.get("internal_name") or f"skin{skin_number}"
            ),
            skinline=str(row.get("skinline") or ""),
            parent_skin_number=parent,
            is_chroma=is_chroma,
            aliases=(),
        )
        skins.append(
            LocalSkin(
                champion_name=skin.champion_name,
                champion_id=skin.champion_id,
                skin_number=skin.skin_number,
                display_name=skin.display_name,
                base_display_name=skin.base_display_name,
                internal_name=skin.internal_name,
                skinline=skin.skinline,
                parent_skin_number=skin.parent_skin_number,
                is_chroma=skin.is_chroma,
                aliases=make_skin_aliases(skin),
            )
        )

    return LocalCatalog(
        champion_name=champion_name,
        wad_path=wad_path,
        identity=identity,
        main_unit=identity.main_unit,
        skins=tuple(skins),
    )


def assert_runtime_session_source_current(
    session: ChampionRuntimeSession,
) -> None:
    actual = capture_wad_file_identity(session.source_wad)
    if actual != session.source_identity:
        raise WadChangedDuringRead(
            session.source_wad,
            session.source_identity,
            actual,
        )
    if (
        session.prepared is not None
        and session.prepared.file_identity != session.source_identity
    ):
        raise WadChangedDuringRead(
            session.source_wad,
            session.source_identity,
            session.prepared.file_identity,
        )
    if capture_lcu_wad_generation(session.source_wad.parent) != (
        session.lcu_generation
    ):
        raise LcuDataError(
            "local LCU WAD generation changed during champion session "
            f"{session.identity.display_name}"
        )


def create_runtime_champion_session(
    champion_name: str,
    wad_path: Path,
    wad_mode: str,
    *,
    persistent_cache: PersistentJsonCache | None = None,
    hash_skin_index: HashSkinIndex | None = None,
) -> ChampionRuntimeSession:
    try:
        lcu_generation = capture_lcu_wad_generation(wad_path.parent)
        identity = load_champion_identity(champion_name, wad_path.parent)
        if capture_lcu_wad_generation(wad_path.parent) != lcu_generation:
            raise LcuDataError(
                "local LCU WAD generation changed while resolving "
                f"{champion_name}"
            )
    except (LcuDataError, WadError, OSError) as exc:
        raise ChampionIdentityError(
            f"failed binding local LCU inputs for {champion_name}: {exc}"
        ) from exc

    with timed_phase("catalog.session.index"):
        prepared = PreparedChampionWad(wad_path, identity=identity)
    if hash_skin_index is None:
        validate_identity_wad(identity, prepared)
    else:
        expected_name = f"{identity.wad_base}.wad.client"
        if prepared.wad_path.name.casefold() != expected_name.casefold():
            raise ChampionIdentityError(
                f"official champion id {identity.champion_id} expects "
                f"{expected_name}, got {prepared.wad_path.name}"
            )
        main_skin0 = hash_skin_index.record_for(identity.main_unit, 0)
        if (
            main_skin0 is None
            or not prepared.contains_hash(main_skin0.path_hash)
        ):
            raise ChampionIdentityError(
                f"newest dictionary and {prepared.wad_path.name} do not "
                f"jointly prove official mainUnit "
                f"{identity.main_unit!r} skin0"
            )
    session = ChampionRuntimeSession(
        identity=identity,
        source_wad=wad_path,
        source_identity=prepared.file_identity,
        toc_digest=prepared.toc_digest,
        lcu_generation=lcu_generation,
        requested_mode=wad_mode,
        backend=wad_mode,
        prepared=prepared if wad_mode == "direct" else None,
        available_path_hashes=frozenset(prepared.chunks_by_hash),
        hash_skin_index=hash_skin_index,
        persistent_cache=persistent_cache,
    )
    assert_runtime_session_source_current(session)
    count_operation(
        "champion.sessions",
        champion=champion_name,
        backend=wad_mode,
    )
    return session


def direct_catalog_main_records(
    session: ChampionRuntimeSession,
) -> tuple[HashSkinRecord, ...]:
    prepared = session.prepared
    if prepared is None:
        raise ValueError("Direct Catalog requires a PreparedChampionWad")
    index = session.hash_skin_index
    if index is None:
        raise ChampionLayoutError(
            f"Direct Catalog for {session.identity.display_name} has no "
            "validated HashSkinIndex"
        )
    dictionary_records = index.records_for_unit(
        session.identity.main_unit,
    )
    count_operation(
        "catalog.direct.dictionary_records",
        len(dictionary_records),
        champion=session.identity.display_name,
    )
    with timed_phase("catalog.direct_probe"):
        present = tuple(
            record
            for record in dictionary_records
            if record.path_hash in prepared.chunks_by_hash
        )
    skin0 = next(
        (
            record
            for record in present
            if record.skin_number == 0
        ),
        None,
    )
    if skin0 is None:
        raise ChampionIdentityError(
            f"newest dictionary and {session.source_wad.name} do not "
            f"jointly prove official mainUnit "
            f"{session.identity.main_unit!r} skin0"
        )
    count_operation(
        "catalog.direct.present_records",
        len(present),
        champion=session.identity.display_name,
    )
    return present


def catalog_lcu_source_documents(
    session: ChampionRuntimeSession,
) -> list[dict[str, Any]]:
    rel_paths = (
        LCU_CHAMPION_SUMMARY_PATH,
        LCU_SKINS_PATH,
        (
            "plugins/rcp-be-lol-game-data/global/default/v1/champions/"
            f"{session.identity.champion_id}.json"
        ),
    )
    records = [
        load_lcu_json_with_identity(
            session.source_wad.parent,
            rel_path,
            expected_generation=session.lcu_generation,
        )
        for rel_path in rel_paths
    ]
    return [
        lcu_json_source_document(record.source)
        for record in records
    ]


def serialize_local_catalog(catalog: LocalCatalog) -> dict[str, Any]:
    identity = catalog.identity
    return {
        "schemaVersion": CATALOG_CACHE_SCHEMA_VERSION,
        "champion": {
            "id": identity.champion_id,
            "displayName": identity.display_name,
            "alias": identity.alias,
            "wadBase": identity.wad_base,
            "mainUnit": identity.main_unit,
        },
        "skins": [
            {
                "skinNumber": skin.skin_number,
                "displayName": skin.display_name,
                "baseDisplayName": skin.base_display_name,
                "internalName": skin.internal_name,
                "skinline": skin.skinline,
                "parentSkinNumber": skin.parent_skin_number,
                "isChroma": skin.is_chroma,
                "aliases": list(skin.aliases),
            }
            for skin in catalog.skins
        ],
    }


def deserialize_local_catalog(
    payload: object,
    session: ChampionRuntimeSession,
) -> LocalCatalog:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schemaVersion", "champion", "skins"}
        or payload["schemaVersion"] != CATALOG_CACHE_SCHEMA_VERSION
    ):
        raise ChampionLayoutError(
            "persistent Catalog payload has an invalid schema"
        )
    identity = session.identity
    expected_champion = {
        "id": identity.champion_id,
        "displayName": identity.display_name,
        "alias": identity.alias,
        "wadBase": identity.wad_base,
        "mainUnit": identity.main_unit,
    }
    if payload["champion"] != expected_champion:
        raise ChampionLayoutError(
            "persistent Catalog champion identity differs"
        )
    rows = payload["skins"]
    if not isinstance(rows, list):
        raise ChampionLayoutError(
            "persistent Catalog skins must be an array"
        )
    expected_fields = {
        "skinNumber",
        "displayName",
        "baseDisplayName",
        "internalName",
        "skinline",
        "parentSkinNumber",
        "isChroma",
        "aliases",
    }
    skins: list[LocalSkin] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != expected_fields:
            raise ChampionLayoutError(
                f"persistent Catalog skin row {index} has an invalid schema"
            )
        skin_number = row["skinNumber"]
        if (
            isinstance(skin_number, bool)
            or not isinstance(skin_number, int)
            or not 0 <= skin_number <= 999
        ):
            raise ChampionLayoutError(
                f"persistent Catalog skin row {index} has an invalid number"
            )
        parent = row["parentSkinNumber"]
        if (
            parent is not None
            and (
                isinstance(parent, bool)
                or not isinstance(parent, int)
                or not 0 <= parent <= 999
                or parent == skin_number
            )
        ):
            raise ChampionLayoutError(
                f"persistent Catalog skin{skin_number} has an invalid parent"
            )
        text_fields = (
            "displayName",
            "baseDisplayName",
            "internalName",
            "skinline",
        )
        if any(not isinstance(row[field], str) for field in text_fields):
            raise ChampionLayoutError(
                f"persistent Catalog skin{skin_number} has invalid text"
            )
        if (
            not isinstance(row["isChroma"], bool)
            or row["isChroma"] != (parent is not None)
        ):
            raise ChampionLayoutError(
                f"persistent Catalog skin{skin_number} has invalid chroma state"
            )
        aliases_value = row["aliases"]
        if (
            not isinstance(aliases_value, list)
            or any(not isinstance(alias, str) for alias in aliases_value)
        ):
            raise ChampionLayoutError(
                f"persistent Catalog skin{skin_number} aliases are invalid"
            )
        aliases = tuple(aliases_value)
        if aliases != tuple(sorted(set(aliases))):
            raise ChampionLayoutError(
                f"persistent Catalog skin{skin_number} aliases are not canonical"
            )
        skin = LocalSkin(
            champion_name=identity.display_name,
            champion_id=identity.champion_id,
            skin_number=skin_number,
            display_name=row["displayName"],
            base_display_name=row["baseDisplayName"],
            internal_name=row["internalName"],
            skinline=row["skinline"],
            parent_skin_number=parent,
            is_chroma=row["isChroma"],
            aliases=aliases,
        )
        if aliases != make_skin_aliases(skin):
            raise ChampionLayoutError(
                f"persistent Catalog skin{skin_number} aliases differ"
            )
        skins.append(skin)
    numbers = tuple(skin.skin_number for skin in skins)
    if numbers != tuple(sorted(set(numbers))) or 0 not in numbers:
        raise ChampionLayoutError(
            "persistent Catalog skin numbers are not canonical"
        )
    return LocalCatalog(
        champion_name=identity.display_name,
        wad_path=session.source_wad,
        identity=identity,
        main_unit=identity.main_unit,
        skins=tuple(skins),
    )


def build_direct_catalog_cache_key(
    session: ChampionRuntimeSession,
    main_records: tuple[HashSkinRecord, ...],
) -> PersistentCacheKey:
    prepared = session.prepared
    cache = session.persistent_cache
    if prepared is None or cache is None:
        raise ValueError("persistent Direct Catalog cache is unavailable")
    with timed_phase("cache.catalog.identity"):
        chunk_identities = ensure_hash_chunk_identities(
            prepared,
            {
                record.path: record.path_hash
                for record in main_records
            },
        )
        lcu_sources = catalog_lcu_source_documents(session)
        _tool_stat, ritobin_identity = capture_tool_identity(RITOBIN_CLI)
        manifest = {
            "schemaVersion": CATALOG_CACHE_SCHEMA_VERSION,
            "parserSchemaVersion": CATALOG_PARSER_SCHEMA_VERSION,
            "champion": {
                "id": session.identity.champion_id,
                "displayName": session.identity.display_name,
                "alias": session.identity.alias,
                "wadBase": session.identity.wad_base,
                "mainUnit": session.identity.main_unit,
            },
            "wad": {
                "variant": session.source_wad.name,
                "version": str(prepared.version),
                "tocDigest": prepared.toc_digest,
            },
            "mainChunks": [
                serialize_required_chunk_identity(
                    chunk_identities[path]
                )
                for path in sorted(chunk_identities)
            ],
            "ritobin": ritobin_identity.as_json(),
            "lcuSources": lcu_sources,
        }
    return cache.key(manifest)


def stage_catalog_payloads(
    session: ChampionRuntimeSession,
    payloads: Mapping[str, bytes],
) -> LocalCatalog:
    if not payloads:
        raise ChampionLayoutError(
            f"Direct Catalog found no main skin metadata for "
            f"{session.identity.display_name}"
        )
    with tempfile.TemporaryDirectory(
        prefix=".direct-catalog-",
        dir=SCRIPT_DIR,
    ) as temp_name:
        temp_dir = Path(temp_name)
        skins_dir = (
            temp_dir
            / "staging"
            / session.identity.main_unit
            / "skins"
        )
        skins_dir.mkdir(parents=True)
        with timed_phase("catalog.direct_stage"):
            for normalized_path, payload in payloads.items():
                match = re.fullmatch(
                    r"data/characters/[^/]+/skins/skin(\d+)\.bin",
                    normalized_path,
                )
                if match is None:
                    raise ChampionLayoutError(
                        f"unexpected Direct Catalog path: {normalized_path}"
                    )
                (
                    skins_dir
                    / f"skin{int(match.group(1))}.bin"
                ).write_bytes(payload)
        rows = collect_catalog_rows(
            skins_dir,
            temp_dir / "catalog-json",
        )
    if not rows:
        raise ChampionLayoutError(
            f"Ritobin produced no main skin metadata for "
            f"{session.identity.display_name}"
        )
    return build_catalog_from_metadata_rows(
        session.identity.display_name,
        session.source_wad,
        session.identity,
        rows,
    )


@timed_function("catalog.direct")
def build_direct_catalog(
    session: ChampionRuntimeSession,
) -> LocalCatalog:
    prepared = session.prepared
    if prepared is None:
        raise ValueError("Direct Catalog requires a PreparedChampionWad")
    count_operation(
        "catalog.direct.attempts",
        champion=session.identity.display_name,
    )
    try:
        assert_runtime_session_source_current(session)
        main_records = direct_catalog_main_records(session)
        session.main_skin_records = main_records
        cache_key: PersistentCacheKey | None = None
        if session.persistent_cache is not None:
            cache_key = build_direct_catalog_cache_key(
                session,
                main_records,
            )
            with timed_phase("cache.catalog.lookup"):
                lookup = session.persistent_cache.lookup(
                    "catalog",
                    cache_key,
                )
            if lookup.hit:
                try:
                    catalog = deserialize_local_catalog(
                        lookup.payload,
                        session,
                    )
                except ChampionLayoutError:
                    session.persistent_cache.invalidate(
                        "catalog",
                        cache_key,
                    )
                    count_operation(
                        "cache.catalog.persistent_corruptions",
                        champion=session.identity.display_name,
                    )
                else:
                    assert_runtime_session_source_current(session)
                    count_operation(
                        "cache.catalog.persistent_hits",
                        champion=session.identity.display_name,
                    )
                    count_operation(
                        "catalog.direct.successes",
                        champion=session.identity.display_name,
                    )
                    return catalog
            else:
                count_operation(
                    "cache.catalog.persistent_misses",
                    champion=session.identity.display_name,
                    status=lookup.status,
                )
        count_operation(
            "catalog.direct.read_hashes.calls",
            champion=session.identity.display_name,
        )
        main_hashes = tuple(
            record.path_hash
            for record in main_records
        )
        with timed_phase("catalog.direct_read"):
            payloads_by_hash = prepared.read_hashes(
                main_hashes,
                validate_bin=True,
            )
        payloads = {
            record.path: payloads_by_hash[record.path_hash]
            for record in main_records
        }
        catalog = stage_catalog_payloads(session, payloads)
        assert_runtime_session_source_current(session)
        if (
            session.persistent_cache is not None
            and cache_key is not None
        ):
            with timed_phase("cache.catalog.store"):
                stored = session.persistent_cache.store(
                    "catalog",
                    cache_key,
                    serialize_local_catalog(catalog),
                )
            count_operation(
                (
                    "cache.catalog.persistent_stores"
                    if stored
                    else "cache.catalog.persistent_store_failures"
                ),
                champion=session.identity.display_name,
            )
    except BaseException:
        count_operation(
            "catalog.direct.failures",
            champion=session.identity.display_name,
        )
        raise
    count_operation(
        "catalog.direct.successes",
        champion=session.identity.display_name,
    )
    count_operation(
        "cache.catalog.misses",
        champion=session.identity.display_name,
        backend="direct",
    )
    return catalog


def build_explicit_legacy_catalog(
    session: ChampionRuntimeSession,
) -> LocalCatalog:
    count_operation(
        "catalog.legacy.sessions",
        champion=session.identity.display_name,
    )
    temp_owner = tempfile.TemporaryDirectory(
        prefix=".legacy-session-",
        dir=SCRIPT_DIR,
    )
    temp_dir = Path(temp_owner.name)
    try:
        extracted_dir = extract_wad_to_temp_dir(
            session.source_wad,
            temp_dir,
            purpose="legacy-session",
            expected_wad_identity=session.source_identity,
            expected_toc_digest=session.toc_digest,
        )
        characters_dir = extracted_dir / "data" / "characters"
        if not characters_dir.is_dir():
            raise ChampionLayoutError(
                f"no data/characters directory found after extracting "
                f"{session.source_wad.name}"
            )
        unit_dirs = [
            path
            for path in characters_dir.iterdir()
            if path.is_dir()
            and (path / "skins" / "skin0.bin").is_file()
        ]
        main_dir = select_main_unit_directory(
            unit_dirs,
            session.identity,
        )
        rows = collect_catalog_rows(
            main_dir / "skins",
            temp_dir / "catalog-json",
        )
        catalog = build_catalog_from_metadata_rows(
            session.identity.display_name,
            session.source_wad,
            session.identity,
            rows,
        )
        assert_runtime_session_source_current(session)
    except BaseException:
        temp_owner.cleanup()
        raise
    session.legacy_temp = temp_owner
    session.legacy_extracted_root = extracted_dir
    count_operation(
        "cache.catalog.misses",
        champion=session.identity.display_name,
        backend="legacy",
    )
    return catalog


def build_runtime_catalog(
    session: ChampionRuntimeSession,
) -> LocalCatalog:
    if session.backend == "legacy":
        return build_explicit_legacy_catalog(session)
    return build_direct_catalog(session)


@timed_function("catalog.total")
def build_local_catalog(
    champion_name: str,
    wad_path: Path,
    *,
    wad_extract_path: Path | None = None,
    hashes_path: Path | None = None,
) -> LocalCatalog:
    try:
        lcu_generation = capture_lcu_wad_generation(wad_path.parent)
        identity = load_champion_identity(champion_name, wad_path.parent)
        if capture_lcu_wad_generation(wad_path.parent) != lcu_generation:
            raise LcuDataError(
                "local LCU WAD generation changed while resolving "
                f"{champion_name}"
            )
    except (LcuDataError, WadError, OSError) as exc:
        sys.exit(f"failed binding local LCU inputs for {champion_name}: {exc}")
    prepared = PreparedChampionWad(wad_path, identity=identity)
    try:
        validate_identity_wad(identity, prepared)
    except ChampionIdentityError as exc:
        sys.exit(str(exc))

    count_operation("cache.catalog.bypasses", champion=champion_name)

    log(f"building local skin catalog for {champion_name} from {wad_path.name}")
    with tempfile.TemporaryDirectory(prefix=".wad-catalog-", dir=SCRIPT_DIR) as temp_name:
        temp_dir = Path(temp_name)
        extracted_dir = extract_wad_to_temp_dir(
            wad_path,
            temp_dir,
            purpose="catalog",
            wad_extract_path=wad_extract_path,
            hashes_path=hashes_path,
            expected_wad_identity=prepared.file_identity,
            expected_toc_digest=prepared.toc_digest,
        )
        characters_dir = extracted_dir / "data" / "characters"
        if not characters_dir.is_dir():
            sys.exit(f"no data/characters directory found after extracting {wad_path.name}")

        unit_dirs = [
            p for p in characters_dir.iterdir()
            if p.is_dir() and (p / "skins" / "skin0.bin").is_file()
        ]
        if not unit_dirs:
            sys.exit(f"no character skin metadata found in {wad_path.name}")
        try:
            main_dir = select_main_unit_directory(unit_dirs, identity)
        except ChampionIdentityError as exc:
            sys.exit(f"{exc} in {wad_path.name}")
        skins_dir = main_dir / "skins"
        rows = collect_catalog_rows(
            skins_dir,
            temp_dir / "catalog-json",
        )

    catalog = build_catalog_from_metadata_rows(
        champion_name,
        wad_path=wad_path,
        identity=identity,
        rows=rows,
    )
    try:
        final_wad = parse_wad_index_core(wad_path)
        if (
            final_wad.file_identity != prepared.file_identity
            or final_wad.toc_digest != prepared.toc_digest
        ):
            raise WadChangedDuringRead(
                wad_path,
                prepared.file_identity,
                final_wad.file_identity,
            )
        if capture_lcu_wad_generation(wad_path.parent) != lcu_generation:
            raise LcuDataError(
                "local LCU WAD generation changed while building "
                f"{champion_name}"
            )
    except (LcuDataError, WadError, OSError) as exc:
        sys.exit(f"catalog inputs changed for {champion_name}: {exc}")
    return catalog


def deduplicate_local_skin_matches(direct: list[LocalSkin]) -> list[LocalSkin]:
    """Return only the exact matched entries, ordered and de-duplicated by ID."""

    selected: list[LocalSkin] = []
    seen: set[int] = set()
    for skin in sorted(direct, key=lambda s: s.skin_number):
        if skin.skin_number == 0 or skin.skin_number in seen:
            continue
        seen.add(skin.skin_number)
        selected.append(skin)
    return selected


def get_runtime_catalog(
    champion_name: str,
    wad_path: Path,
    champions_dir: Path,
    *,
    wad_mode: str,
    session_pool: ChampionSessionPool | None,
) -> LocalCatalog:
    if session_pool is not None:
        if session_pool.champions_dir.resolve() != champions_dir.resolve():
            raise ValueError(
                "ChampionSessionPool champions directory does not match"
            )
        if session_pool.wad_mode != wad_mode:
            raise ValueError(
                "ChampionSessionPool WAD mode does not match selection mode"
            )
        return session_pool.get_catalog(champion_name, wad_path)
    with ChampionSessionPool(champions_dir, wad_mode) as owned_pool:
        return owned_pool.get_catalog(champion_name, wad_path)


def resolve_local_skin_name(
    skin_name: str,
    champions_dir: Path,
    *,
    wad_mode: str = "direct",
    session_pool: ChampionSessionPool | None = None,
) -> list[LocalSkin]:
    official_ref = resolve_official_skin_ref(skin_name, champions_dir)
    if official_ref is not None:
        if official_ref.skin_number == 0:
            sys.exit(f"{official_ref.display_name!r} is skin0/classic and cannot be used as a target skin")

        champion_name, wad_path = find_official_champion_wad(official_ref.champion_id, champions_dir)
        catalog = get_runtime_catalog(
            champion_name,
            wad_path,
            champions_dir,
            wad_mode=wad_mode,
            session_pool=session_pool,
        )
        direct = [skin for skin in catalog.skins if skin.skin_number == official_ref.skin_number]
        if not direct:
            sys.exit(
                f"official skin {official_ref.display_name!r} maps to skin{official_ref.skin_number}, "
                f"but {wad_path.name} does not contain that skin"
            )
        return deduplicate_local_skin_matches(direct)

    champion_name, wad_path = infer_champion_from_skin_name(skin_name, champions_dir)
    catalog = get_runtime_catalog(
        champion_name,
        wad_path,
        champions_dir,
        wad_mode=wad_mode,
        session_pool=session_pool,
    )

    def matches_query(skin: LocalSkin, query: str) -> bool:
        if normalize_display_name(query) == normalize_display_name(skin.display_name):
            return True
        return normalize_lookup(query) in skin.aliases

    direct = [skin for skin in catalog.skins if matches_query(skin, skin_name)]

    if not direct:
        available = [s.display_name for s in catalog.skins if s.skin_number != 0 and not s.is_chroma]
        sys.exit(
            f"skin not found from local WAD metadata: {skin_name!r}\n"
            f"Available base skins for {champion_name}: {available}"
        )

    return deduplicate_local_skin_matches(direct)


def find_source_wad(champion_unit: str, champions_dir: Path | None = None) -> Path:
    candidates: list[Path] = []

    # Search in LoL Champions directory first (preferred source).
    if champions_dir is not None and champions_dir.is_dir():
        for wad_path in champions_dir.iterdir():
            if not wad_path.is_file():
                continue
            if not wad_path.name.lower().endswith(WAD_CLIENT_SUFFIX):
                continue
            if normalize_champion_name(wad_client_base_name(wad_path)).lower() == champion_unit.lower():
                candidates.append(wad_path)

    # The installed game WAD is the preferred source. Return it immediately so
    # cache preflight does not recursively scan the project on every selection.
    if candidates:
        candidates.sort(key=lambda p: (-p.stat().st_size, str(p).lower()))
        return candidates[0]

    # Fallback: also search project directory.
    for wad_path in SCRIPT_DIR.rglob(f"*{WAD_CLIENT_SUFFIX}"):
        resolved_wad = wad_path.resolve()
        if resolved_wad.is_relative_to(INPUT_ROOT.resolve()) or resolved_wad.is_relative_to(
            OUTPUT_ROOT.resolve()
        ):
            continue
        if normalize_champion_name(wad_client_base_name(wad_path)).lower() == champion_unit.lower():
            candidates.append(wad_path)

    if not candidates:
        search_locations = [str(SCRIPT_DIR)]
        if champions_dir is not None:
            search_locations.insert(0, str(champions_dir))
        sys.exit(
            f"source WAD for {champion_unit!r} not found in:\n"
            + "\n".join(f"  - {loc}" for loc in search_locations)
        )

    # Prefer LoL install dir, then project root, then largest file.
    def sort_key(p: Path) -> tuple:
        in_lol = (
            champions_dir is not None
            and p.resolve().is_relative_to(champions_dir.resolve())
        )
        in_root = p.parent == SCRIPT_DIR
        return (not in_lol, not in_root, -p.stat().st_size, str(p).lower())

    candidates.sort(key=sort_key)
    return candidates[0]


def read_legacy_extracted_units(
    extracted_dir: Path,
    skin_numbers: list[int],
) -> dict[int, list[tuple[str, bytes, bytes]]]:
    characters_dir = extracted_dir / "data" / "characters"
    if not characters_dir.is_dir():
        raise ChampionLayoutError(
            f"no data/characters directory found under {extracted_dir}"
        )

    out: dict[int, list[tuple[str, bytes, bytes]]] = {}
    for skin_number in skin_numbers:
        hits: list[tuple[str, bytes, bytes]] = []
        for character_dir in sorted(characters_dir.iterdir()):
            if not character_dir.is_dir():
                continue
            skins_dir = character_dir / "skins"
            if not skins_dir.is_dir():
                continue
            base_bin = skins_dir / "skin0.bin"
            target_bin = skins_dir / f"skin{skin_number}.bin"
            if base_bin.is_file() and target_bin.is_file():
                log(
                    f"  found unit: {character_dir.name} "
                    f"(skin0 + skin{skin_number})",
                    "wad-extract",
                )
                with timed_phase("prepare.bin_read"):
                    hits.append(
                        (
                            character_dir.name,
                            base_bin.read_bytes(),
                            target_bin.read_bytes(),
                        )
                    )
        if not hits:
            log(
                f"  warning: no unit found for skin{skin_number}",
                "wad-extract",
            )
        out[skin_number] = hits
    return out


def run_wad_extract_to_temp(
    wad_path: Path,
    skin_numbers: list[int],
    *,
    expected_wad_identity: WadFileIdentity | None = None,
    expected_toc_digest: str | None = None,
) -> dict[int, list[tuple[str, bytes, bytes]]]:
    """Extract WAD once and find units for each skin number.

    Returns {skin_number: [(unit_name, base_bytes, target_bytes), ...]}.
    """
    with tempfile.TemporaryDirectory(
        prefix=".wad-extract-",
        dir=SCRIPT_DIR,
    ) as temp_name:
        temp_dir = Path(temp_name)
        extracted_dir = extract_wad_to_temp_dir(
            wad_path,
            temp_dir,
            purpose="prepare",
            expected_wad_identity=expected_wad_identity,
            expected_toc_digest=expected_toc_digest,
        )
        return read_legacy_extracted_units(
            extracted_dir,
            skin_numbers,
        )


@timed_function("prepare.total")
def prepare_skins(
    skins: list[LocalSkin],
    champions_dir: Path | None = None,
    *,
    source_wad: Path | None = None,
    expected_wad_identity: WadFileIdentity | None = None,
    expected_toc_digest: str | None = None,
) -> tuple[dict[Path, set[str]], str]:
    """Prepare input folders for one or more skins (base + chromas).

    Extracts the WAD once and grabs all needed skin numbers in one pass.
    Returns ({skin_dir: found_units}, champion_display_name) for each skin.
    """
    if not skins:
        return {}, ""
    champion_display_name = skins[0].champion_name
    champion_unit = normalize_champion_name(champion_display_name)

    wad_path = (
        find_source_wad(champion_unit, champions_dir)
        if source_wad is None
        else source_wad
    )
    try:
        rel = wad_path.relative_to(SCRIPT_DIR)
    except ValueError:
        rel = wad_path
    log(f"source WAD = {rel}")

    # Map skin_number -> metadata for all selected skins.
    sn_map: dict[int, LocalSkin] = {}
    for skin in skins:
        sn_map[skin.skin_number] = skin

    log(f"extracting {len(sn_map)} skin(s) for {champion_display_name}...")
    all_units = run_wad_extract_to_temp(
        wad_path,
        list(sn_map.keys()),
        expected_wad_identity=expected_wad_identity,
        expected_toc_digest=expected_toc_digest,
    )

    result: dict[Path, set[str]] = {}
    for sn, skin in sn_map.items():
        dname = skin.display_name
        disk_name = sanitize_for_windows(dname)
        skin_dir = INPUT_ROOT / disk_name
        units = all_units.get(sn, [])
        if not units:
            log(f"  skip {dname}: no matching bins in WAD")
            continue
        found: set[str] = set()
        for unit_name, base_data, target_data in units:
            unit_dir = skin_dir / unit_name
            with timed_phase("prepare.input_write"):
                unit_dir.mkdir(parents=True, exist_ok=True)
                (unit_dir / "skin0.bin").write_bytes(base_data)
                (unit_dir / f"skin{sn}.bin").write_bytes(target_data)
            found.add(unit_name)
        log(f"  {dname}: {len(found)} unit(s)")
        result[skin_dir] = found

    return result, champion_display_name


def json_key_repr(value: Any) -> str:
    if isinstance(value, int):
        return f"0x{value:x}"
    return repr(value)


def json_entry_items(data: dict[str, Any], label: str) -> list[dict[str, Any]]:
    try:
        items = data["entries"]["value"]["items"]
    except (KeyError, TypeError):
        sys.exit(f"[{label}] ritobin JSON missing entries.value.items")
    if not isinstance(items, list):
        sys.exit(f"[{label}] ritobin JSON entries.value.items must be a list")
    return [item for item in items if isinstance(item, dict)]


def find_json_entry(data: dict[str, Any], entry_name: str, label: str) -> dict[str, Any]:
    hits = [
        item for item in json_entry_items(data, label)
        if isinstance(item.get("value"), dict) and item["value"].get("name") == entry_name
    ]
    if len(hits) != 1:
        sys.exit(f"[{label}] expected 1 {entry_name} entry, got {len(hits)}")
    return hits[0]


def find_json_field(entry: dict[str, Any], field_name: str, label: str) -> dict[str, Any]:
    value = entry.get("value")
    if not isinstance(value, dict):
        sys.exit(f"[{label}] JSON entry has no object value")
    fields = value.get("items")
    if not isinstance(fields, list):
        sys.exit(f"[{label}] JSON entry has no field list")
    hits = [field for field in fields if isinstance(field, dict) and field.get("key") == field_name]
    if len(hits) != 1:
        sys.exit(f"[{label}] expected 1 field {field_name!r}, got {len(hits)}")
    return hits[0]


def replace_json_entry_key(
    base_key: Any,
    target_entry: dict[str, Any],
    label: str,
) -> None:
    old_key = target_entry.get("key")
    if old_key == base_key:
        log(f"  {label}: already identical, skip")
        return
    log(f"  {label}:")
    log(f"    -  {json_key_repr(old_key)}")
    log(f"    +  {json_key_repr(base_key)}")
    target_entry["key"] = base_key


def replace_json_field(
    base_value: Any,
    target_entry: dict[str, Any],
    field_name: str,
    label: str,
) -> None:
    target_field = find_json_field(target_entry, field_name, f"{label} target")
    old_value = target_field.get("value")
    if old_value == base_value:
        log(f"  {label}: already identical, skip")
        return
    log(f"  {label}:")
    log(f"    -  {json_key_repr(old_value)}")
    log(f"    +  {json_key_repr(base_value)}")
    target_field["value"] = base_value


def extract_base_rebase_snapshot(
    base_data: dict[str, Any],
) -> BaseRebaseSnapshot:
    base_skin = find_json_entry(base_data, "SkinCharacterDataProperties", "base skin")
    base_resolver = find_json_entry(base_data, "ResourceResolver", "base ResourceResolver")
    champion_skin_name = find_json_field(
        base_skin,
        "ChampionSkinName",
        "ChampionSkinName base",
    )
    resource_resolver = find_json_field(
        base_skin,
        "mResourceResolver",
        "mResourceResolver base",
    )
    return BaseRebaseSnapshot.from_values(
        skin_entry_key=base_skin.get("key"),
        champion_skin_name=champion_skin_name.get("value"),
        resource_resolver=resource_resolver.get("value"),
        resolver_entry_key=base_resolver.get("key"),
    )


def apply_base_rebase_snapshot(
    base_snapshot: BaseRebaseSnapshot,
    target_data: dict[str, Any],
) -> dict[str, Any]:
    values = base_snapshot.values()
    target_skin = find_json_entry(target_data, "SkinCharacterDataProperties", "target skin")
    target_resolver = find_json_entry(target_data, "ResourceResolver", "target ResourceResolver")

    replace_json_entry_key(
        values["skinEntryKey"],
        target_skin,
        "SkinCharacterDataProperties entry key",
    )
    replace_json_field(
        values["championSkinName"],
        target_skin,
        "ChampionSkinName",
        "ChampionSkinName",
    )
    replace_json_field(
        values["resourceResolver"],
        target_skin,
        "mResourceResolver",
        "mResourceResolver",
    )
    replace_json_entry_key(
        values["resolverEntryKey"],
        target_resolver,
        "ResourceResolver entry key",
    )
    return target_data


def fresh_dir(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def archive_extensions(archive_format: str) -> tuple[str, ...]:
    """Return the lowercase archive extensions requested by the CLI."""
    if archive_format == "both":
        return ARCHIVE_FORMATS
    if archive_format not in ARCHIVE_FORMATS:
        raise ValueError(f"unsupported archive format: {archive_format}")
    return (archive_format,)


def archive_output_directory(
    champion_name: str,
    base_skin_name: str,
    disk_name: str,
    is_chroma: bool,
    output_root: Path | None = None,
) -> Path:
    root = OUTPUT_ROOT if output_root is None else output_root
    champion_folder = sanitize_for_windows(champion_name)
    base_folder = sanitize_for_windows(base_skin_name)
    archive_dir = root / champion_folder / base_folder
    return archive_dir / disk_name if is_chroma else archive_dir


def canonical_json_sha256(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def archive_tool_fingerprint(
    identity: ToolIdentity | None,
) -> dict[str, Any] | None:
    if identity is None:
        return None
    return {
        "size": identity.size,
        "sha256": identity.sha256,
    }


def capture_archive_tool_identities(
) -> tuple[ToolIdentity, ToolIdentity]:
    if not RITOBIN_CLI.is_file():
        sys.exit(f"ritobin_cli.exe not found at {RITOBIN_CLI}; run setup.bat")
    if not WAD_MAKE.is_file():
        sys.exit(f"wad-make.exe not found at {WAD_MAKE}; run setup.bat")
    _ritobin_stat, ritobin = capture_tool_identity(RITOBIN_CLI)
    _wad_make_stat, wad_make = capture_tool_identity(WAD_MAKE)
    return ritobin, wad_make


def build_rebaser_metadata(
    skin: LocalSkin,
    source_wad: Path,
    display_name: str,
    version: str,
    *,
    layout_fingerprint: str | None = None,
    ritobin_identity: ToolIdentity | None = None,
    wad_make_identity: ToolIdentity | None = None,
) -> dict[str, Any]:
    source_stat = source_wad.stat()
    fingerprint_payload = {
        "fingerprintSchema": ARCHIVE_FINGERPRINT_SCHEMA_VERSION,
        "schema": REBASE_SCHEMA_VERSION,
        "champion": skin.champion_name,
        "skinNumber": skin.skin_number,
        "baseName": skin.base_display_name,
        "displayName": display_name,
        "version": version,
        "author": AUTHOR,
        "description": MOD_DESCRIPTION,
        "layoutFingerprint": layout_fingerprint,
        "tools": {
            "ritobin": archive_tool_fingerprint(
                ritobin_identity
            ),
            "wadMake": archive_tool_fingerprint(
                wad_make_identity
            ),
        },
        "source": {
            "name": source_wad.name,
            "size": source_stat.st_size,
            "modifiedNs": source_stat.st_mtime_ns,
        },
    }
    return {
        "Schema": REBASE_SCHEMA_VERSION,
        "FingerprintSchema": ARCHIVE_FINGERPRINT_SCHEMA_VERSION,
        "Fingerprint": canonical_json_sha256(fingerprint_payload),
        "LayoutFingerprint": layout_fingerprint,
        "RitobinSha256": (
            None
            if ritobin_identity is None
            else ritobin_identity.sha256
        ),
        "WadMakeSha256": (
            None
            if wad_make_identity is None
            else wad_make_identity.sha256
        ),
    }


def build_mod_info(
    display_name: str,
    version: str,
    *,
    skin: LocalSkin | None = None,
    source_wad: Path | None = None,
    layout_fingerprint: str | None = None,
    ritobin_identity: ToolIdentity | None = None,
    wad_make_identity: ToolIdentity | None = None,
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "Author": AUTHOR,
        "Name": display_name,
        "Version": version,
        "Description": MOD_DESCRIPTION,
    }
    if skin is not None and source_wad is not None:
        info["_Rebaser"] = build_rebaser_metadata(
            skin,
            source_wad,
            display_name,
            version,
            layout_fingerprint=layout_fingerprint,
            ritobin_identity=ritobin_identity,
            wad_make_identity=wad_make_identity,
        )
    return info


def create_archive_plan(
    skin: LocalSkin,
    source_wad: Path,
    display_name: str,
    version: str,
    requested_extensions: tuple[str, ...],
    *,
    input_root: Path | None = None,
    output_root: Path | None = None,
    ritobin_identity: ToolIdentity | None = None,
    wad_make_identity: ToolIdentity | None = None,
) -> ArchivePlan:
    disk_name = sanitize_for_windows(skin.display_name)
    work_root = INPUT_ROOT if input_root is None else input_root
    output_dir = archive_output_directory(
        skin.champion_name,
        skin.base_display_name,
        disk_name,
        skin.is_chroma,
        output_root,
    )
    return ArchivePlan(
        skin=skin,
        source_wad=source_wad,
        work_dir=work_root / disk_name,
        output_dir=output_dir,
        disk_name=disk_name,
        display_name=display_name,
        version=version,
        wad_name=source_wad.name,
        requested_extensions=requested_extensions,
        info=build_mod_info(
            display_name,
            version,
            skin=skin,
            source_wad=source_wad,
            ritobin_identity=ritobin_identity,
            wad_make_identity=wad_make_identity,
        ),
        ritobin_identity=ritobin_identity,
        wad_make_identity=wad_make_identity,
    )


def bind_archive_plan_layout(
    plan: ArchivePlan,
    layout_fingerprint: str,
) -> None:
    plan.layout_fingerprint = layout_fingerprint
    plan.info = build_mod_info(
        plan.display_name,
        plan.version,
        skin=plan.skin,
        source_wad=plan.source_wad,
        layout_fingerprint=layout_fingerprint,
        ritobin_identity=plan.ritobin_identity,
        wad_make_identity=plan.wad_make_identity,
    )


@timed_function("cache.plan")
def build_archive_plans(
    selections: list[LocalSkin],
    champions_dir: Path,
    requested_extensions: tuple[str, ...],
    *,
    session_pool: ChampionSessionPool | None = None,
) -> list[ArchivePlan]:
    plans: list[ArchivePlan] = []
    source_wads: dict[int, Path] = {}
    work_paths: dict[str, tuple[str, int]] = {}
    output_paths: dict[str, tuple[str, int]] = {}
    with timed_phase("cache.archive_tool_identity"):
        ritobin_identity, wad_make_identity = (
            capture_archive_tool_identities()
        )
    record_fact(
        "archiveTools",
        {
            "ritobin": ritobin_identity.as_json(),
            "wadMake": wad_make_identity.as_json(),
        },
    )

    for skin in selections:
        identity = (skin.champion_name, skin.skin_number)

        source_wad = source_wads.get(skin.champion_id)
        if source_wad is None:
            source_wad = resolve_archive_source_wad(
                skin,
                champions_dir,
                session_pool,
            )
            source_wads[skin.champion_id] = source_wad

        plan = create_archive_plan(
            skin,
            source_wad,
            skin.display_name,
            "",
            requested_extensions,
            ritobin_identity=ritobin_identity,
            wad_make_identity=wad_make_identity,
        )

        work_key = str(plan.work_dir.absolute()).casefold()
        previous_work = work_paths.get(work_key)
        if previous_work is not None and previous_work != identity:
            sys.exit(
                f"two selected skins map to the same input folder {plan.work_dir}: "
                f"{previous_work} and {identity}"
            )
        work_paths[work_key] = identity

        for extension in requested_extensions:
            output_path = plan.path_for(extension)
            output_key = str(output_path.absolute()).casefold()
            previous_output = output_paths.get(output_key)
            if previous_output is not None and previous_output != identity:
                sys.exit(
                    f"two selected skins map to the same output file {output_path}: "
                    f"{previous_output} and {identity}"
                )
            output_paths[output_key] = identity

        plans.append(plan)

    return plans


def resolve_archive_source_wad(
    skin: LocalSkin,
    champions_dir: Path,
    session_pool: ChampionSessionPool | None,
) -> Path:
    """Use the identity-bound WAD when Catalog already created a session."""

    if session_pool is None:
        return find_source_wad(
            normalize_champion_name(skin.champion_name),
            champions_dir,
        )
    session = session_pool.session_for_id(skin.champion_id)
    if session is None:
        raise ChampionIdentityError(
            f"selected champion id {skin.champion_id} has no bound "
            "ChampionSession"
        )
    if session.identity.champion_id != skin.champion_id:
        raise ChampionIdentityError(
            f"ChampionSession id {session.identity.champion_id} does not "
            f"match selected champion id {skin.champion_id}"
        )
    if not session.source_wad.is_file():
        raise ChampionIdentityError(
            f"identity-bound source WAD is missing: {session.source_wad}"
        )
    return session.source_wad


def build_skin_layout_fingerprint(
    layout: ChampionLayout,
    skin_layout: SkinLayout,
    required_chunks: Mapping[str, RequiredChunkIdentity],
) -> str:
    paired: list[dict[str, Any]] = []
    for state in skin_layout.paired:
        if (
            state.base_path is None
            or state.target_path is None
            or state.base_path not in required_chunks
            or state.target_path not in required_chunks
        ):
            raise ChampionLayoutError(
                f"paired unit {state.unit!r} has incomplete required identities"
            )
        paired.append(
            {
                "unit": state.unit,
                "base": serialize_required_chunk_identity(
                    required_chunks[state.base_path]
                ),
                "target": serialize_required_chunk_identity(
                    required_chunks[state.target_path]
                ),
            }
        )

    identity = layout.identity
    return canonical_json_sha256(
        {
            "schemaVersion": 1,
            "champion": {
                "id": identity.champion_id,
                "alias": identity.alias,
                "wadBase": identity.wad_base,
                "mainUnit": identity.main_unit,
            },
            "wad": {
                "version": layout.wad_version,
                "tocDigest": layout.toc_digest,
            },
            "skinNumber": skin_layout.skin_number,
            "paired": paired,
        }
    )


@timed_function("prepare.legacy_identity")
def stable_wad_content_identity(
    wad_path: Path,
) -> tuple[WadFileIdentity, str]:
    expected = capture_wad_file_identity(wad_path)
    digest = hashlib.sha256()
    byte_count = 0
    with wad_path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            byte_count += len(block)
    actual = capture_wad_file_identity(wad_path)
    if actual != expected:
        raise WadChangedDuringRead(wad_path, expected, actual)
    count_operation("prepare.legacy_identity.bytes", byte_count)
    return expected, digest.hexdigest()


def build_legacy_prepare_session(
    identity: ChampionIdentity,
    source_wad: Path,
    skin_numbers: tuple[int, ...],
    *,
    runtime_session: ChampionRuntimeSession | None = None,
) -> ChampionPrepareSession:
    source_identity, wad_sha256 = stable_wad_content_identity(source_wad)
    fingerprint = canonical_json_sha256(
        {
            "schemaVersion": 1,
            "backend": "legacy-full-wad",
            "champion": {
                "id": identity.champion_id,
                "alias": identity.alias,
                "wadBase": identity.wad_base,
                "mainUnit": identity.main_unit,
            },
            "source": {
                "size": source_identity.size,
                "sha256": wad_sha256,
            },
        }
    )
    return ChampionPrepareSession(
        identity=identity,
        source_wad=source_wad,
        source_identity=source_identity,
        prepared=None,
        layout=None,
        skin_layouts={},
        required_chunks={},
        layout_fingerprints={
            skin_number: fingerprint
            for skin_number in skin_numbers
        },
        runtime_session=runtime_session,
    )


def runtime_dictionary_candidate_registry(
    identity: ChampionIdentity,
    runtime_session: ChampionRuntimeSession,
) -> tuple[
    CandidateRegistry,
    HashSkinCandidateSet,
    dict[str, object],
]:
    index = runtime_session.hash_skin_index
    if index is None:
        raise CandidateRegistryError(
            f"runtime champion session for {identity.display_name} has no "
            "validated HashSkinIndex"
        )
    candidate_set = derive_hash_skin_candidates(
        identity,
        runtime_session.available_path_hashes,
        index,
    )
    count_operation(
        "prepare.dictionary_candidates",
        len(candidate_set.candidates),
        champion=identity.display_name,
    )
    count_operation(
        "prepare.dictionary_candidate_records",
        len(candidate_set.matched_records),
        champion=identity.display_name,
    )
    return (
        candidate_registry_from_hash_candidates(identity, candidate_set),
        candidate_set,
        candidate_set.fact(),
    )


def champion_layout_cache_documents(
    identity: ChampionIdentity,
    registry: CandidateRegistry,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    entry = registry.require(identity.champion_id)
    registry_document = {
        "championId": entry.champion_id,
        "alias": entry.alias,
        "wadBase": entry.wad_base,
        "mainUnit": entry.main_unit,
        "auxiliaryUnits": list(entry.auxiliary_units),
    }
    candidates = candidate_units_for(identity, registry)
    return registry_document, candidates


def build_layout_cache_key(
    cache: PersistentJsonCache,
    identity: ChampionIdentity,
    prepared: PreparedChampionWad,
    skin_number: int,
    registry_document: Mapping[str, Any],
    candidates: tuple[str, ...],
) -> PersistentCacheKey:
    return cache.key(
        {
            "schemaVersion": LAYOUT_CACHE_SCHEMA_VERSION,
            "algorithmVersion": LAYOUT_ALGORITHM_VERSION,
            "champion": {
                "id": identity.champion_id,
                "alias": identity.alias,
                "wadBase": identity.wad_base,
                "mainUnit": identity.main_unit,
            },
            "wad": {
                "variant": prepared.wad_path.name,
                "version": str(prepared.version),
                "tocDigest": prepared.toc_digest,
            },
            "candidateRegistryDigest": canonical_json_sha256(
                registry_document
            ),
            "candidates": list(candidates),
            "skinNumber": skin_number,
        }
    )


def build_persistent_champion_layout(
    identity: ChampionIdentity,
    prepared: PreparedChampionWad,
    skin_numbers: tuple[int, ...],
    registry: CandidateRegistry,
    cache: PersistentJsonCache,
) -> ChampionLayout:
    if (
        not skin_numbers
        or skin_numbers != tuple(sorted(set(skin_numbers)))
        or any(
            isinstance(skin_number, bool)
            or not isinstance(skin_number, int)
            or not 1 <= skin_number <= 999
            for skin_number in skin_numbers
        )
    ):
        raise ChampionLayoutError(
            "persistent Layout skin numbers must be sorted unique values "
            "from 1 through 999"
        )
    validate_identity_wad(identity, prepared)
    registry_document, candidates = champion_layout_cache_documents(
        identity,
        registry,
    )
    keys = {
        skin_number: build_layout_cache_key(
            cache,
            identity,
            prepared,
            skin_number,
            registry_document,
            candidates,
        )
        for skin_number in skin_numbers
    }
    layouts: dict[int, SkinLayout] = {}
    missing: list[int] = []
    with timed_phase("cache.layout.lookup"):
        for skin_number in skin_numbers:
            key = keys[skin_number]
            lookup = cache.lookup("layout", key)
            if lookup.hit:
                try:
                    skin_layout = deserialize_skin_layout(
                        lookup.payload,
                        skin_number=skin_number,
                        candidates=candidates,
                        main_unit=identity.main_unit,
                    )
                except ChampionLayoutError:
                    cache.invalidate("layout", key)
                    count_operation(
                        "cache.layout.persistent_corruptions",
                        champion=identity.display_name,
                    )
                    missing.append(skin_number)
                else:
                    layouts[skin_number] = skin_layout
                    count_operation(
                        "cache.layout.persistent_hits",
                        champion=identity.display_name,
                    )
            else:
                missing.append(skin_number)
                count_operation(
                    "cache.layout.persistent_misses",
                    champion=identity.display_name,
                    status=lookup.status,
                )

    if missing:
        built = build_champion_layout(
            identity,
            prepared,
            tuple(missing),
            registry,
        )
        for skin_layout in built.skins:
            layouts[skin_layout.skin_number] = skin_layout
            with timed_phase("cache.layout.store"):
                stored = cache.store(
                    "layout",
                    keys[skin_layout.skin_number],
                    serialize_skin_layout(skin_layout),
                )
            count_operation(
                (
                    "cache.layout.persistent_stores"
                    if stored
                    else "cache.layout.persistent_store_failures"
                ),
                champion=identity.display_name,
            )

    return ChampionLayout(
        identity=identity,
        wad_path=prepared.wad_path.resolve(),
        wad_file_identity=prepared.file_identity,
        wad_version=str(prepared.version),
        toc_digest=prepared.toc_digest,
        candidates=candidates,
        skins=tuple(layouts[skin_number] for skin_number in skin_numbers),
    )


def build_direct_prepare_session(
    identity: ChampionIdentity,
    source_wad: Path,
    skin_numbers: tuple[int, ...],
    registry: CandidateRegistry,
    *,
    prepared: PreparedChampionWad | None = None,
    runtime_session: ChampionRuntimeSession | None = None,
    persistent_cache: PersistentJsonCache | None = None,
) -> ChampionPrepareSession:
    if prepared is None:
        with timed_phase("prepare.direct.index"):
            prepared = PreparedChampionWad(source_wad, identity=identity)
    else:
        if prepared.file_identity != capture_wad_file_identity(source_wad):
            raise WadChangedDuringRead(
                source_wad,
                prepared.file_identity,
                capture_wad_file_identity(source_wad),
            )
        count_operation("prepare.direct.shared_prepared_hits")
    cache = (
        runtime_session.persistent_cache
        if runtime_session is not None
        else persistent_cache
    )
    hash_skin_candidates = (
        None
        if runtime_session is None
        else runtime_session.hash_skin_candidates
    )
    with timed_phase("prepare.direct.layout"):
        if hash_skin_candidates is not None:
            layout = build_hash_skin_champion_layout(
                identity,
                prepared,
                skin_numbers,
                hash_skin_candidates,
            )
            if cache is not None:
                count_operation(
                    "cache.layout.dictionary_identity_bypasses",
                    len(skin_numbers),
                    champion=identity.display_name,
                )
        elif cache is None:
            layout = build_champion_layout(
                identity,
                prepared,
                skin_numbers,
                registry,
            )
        else:
            layout = build_persistent_champion_layout(
                identity,
                prepared,
                skin_numbers,
                registry,
                cache,
            )
    if hash_skin_candidates is not None:
        count_operation(
            "prepare.direct.dictionary_bound_sessions",
            champion=identity.display_name,
        )
    with timed_phase("prepare.direct.identity"):
        required_chunks = ensure_required_chunk_identities(layout, prepared)
    skin_layouts = {
        skin_layout.skin_number: skin_layout
        for skin_layout in layout.skins
    }
    layout_fingerprints = {
        skin_number: build_skin_layout_fingerprint(
            layout,
            skin_layouts[skin_number],
            required_chunks,
        )
        for skin_number in skin_numbers
    }
    count_operation("prepare.direct.sessions")
    count_operation("prepare.direct.layout_skins", len(skin_numbers))
    count_operation("prepare.direct.required_paths", len(required_chunks))
    return ChampionPrepareSession(
        identity=identity,
        source_wad=source_wad,
        source_identity=layout.wad_file_identity,
        prepared=prepared,
        layout=layout,
        skin_layouts=skin_layouts,
        required_chunks=required_chunks,
        layout_fingerprints=layout_fingerprints,
        runtime_session=runtime_session,
    )


@timed_function("prepare.session")
def build_prepare_sessions(
    plans: list[ArchivePlan],
    champions_dir: Path,
    *,
    wad_mode: str,
    session_pool: ChampionSessionPool | None = None,
) -> dict[int, ChampionPrepareSession]:
    if wad_mode not in WAD_MODES:
        raise ValueError(f"unsupported WAD mode: {wad_mode}")
    if not plans:
        return {}

    official = load_official_champion_identities(champions_dir)
    identities = {
        identity.champion_id: identity
        for identity in official
    }
    registry: CandidateRegistry | None = None
    if wad_mode == "direct":
        if session_pool is None or session_pool.hash_skin_index is None:
            registry = load_candidate_registry(CHAMPION_UNITS_PATH, official)

    grouped: dict[int, list[ArchivePlan]] = {}
    for plan in plans:
        grouped.setdefault(plan.skin.champion_id, []).append(plan)

    sessions: dict[int, ChampionPrepareSession] = {}
    dictionary_candidate_facts: dict[str, dict[str, object]] = {}
    for champion_id in sorted(grouped):
        champion_plans = grouped[champion_id]
        identity = identities.get(champion_id)
        if identity is None:
            raise ChampionIdentityError(
                f"selected champion id {champion_id} is not in the official roster"
            )
        source_paths = {
            plan.source_wad.resolve()
            for plan in champion_plans
        }
        if len(source_paths) != 1:
            raise ChampionLayoutError(
                f"champion id {champion_id} resolved to multiple source WADs"
            )
        source_wad = champion_plans[0].source_wad
        skin_numbers = tuple(
            sorted({plan.skin.skin_number for plan in champion_plans})
        )

        if wad_mode == "legacy":
            count_operation("prepare.legacy.sessions")
            runtime_session = (
                None
                if session_pool is None
                else session_pool.session_for_id(champion_id)
            )
            session = build_legacy_prepare_session(
                identity,
                source_wad,
                skin_numbers,
                runtime_session=runtime_session,
            )
        else:
            runtime_session = (
                None
                if session_pool is None
                else session_pool.session_for_id(champion_id)
            )
            active_registry = registry
            if runtime_session is not None:
                if runtime_session.hash_skin_index is not None:
                    (
                        active_registry,
                        candidate_set,
                        candidate_fact,
                    ) = runtime_dictionary_candidate_registry(
                        identity,
                        runtime_session,
                    )
                    runtime_session.hash_skin_candidates = candidate_set
                    dictionary_candidate_facts[str(champion_id)] = candidate_fact
                elif (
                    session_pool is not None
                    and session_pool.hash_skin_index is not None
                ):
                    raise CandidateRegistryError(
                        f"runtime session for {identity.display_name} lost "
                        "the validated HashSkinIndex"
                    )
            if active_registry is None:
                raise CandidateRegistryError(
                    f"Direct Prepare for {identity.display_name} has neither "
                    "a dictionary-bound runtime session nor a static registry"
                )
            if runtime_session is not None:
                if runtime_session.source_wad.resolve() != source_wad.resolve():
                    raise ChampionLayoutError(
                        f"shared champion session WAD differs for "
                        f"{identity.display_name}"
                    )
                if runtime_session.backend != "direct":
                    raise ChampionLayoutError(
                        f"Direct mode requires a direct runtime session for "
                        f"{identity.display_name}"
                    )
            changed_error: WadChangedDuringRead | None = None
            attempts = 1 if runtime_session is not None else 2
            for attempt in range(attempts):
                try:
                    session = build_direct_prepare_session(
                        identity,
                        source_wad,
                        skin_numbers,
                        active_registry,
                        prepared=(
                            None
                            if runtime_session is None
                            else runtime_session.prepared
                        ),
                        runtime_session=runtime_session,
                        persistent_cache=(
                            None
                            if session_pool is None
                            else session_pool.persistent_cache
                        ),
                    )
                    break
                except WadChangedDuringRead as exc:
                    changed_error = exc
                    count_operation(
                        "prepare.direct.source_change_retries",
                        attempt=attempt + 1,
                    )
            else:
                assert changed_error is not None
                raise changed_error

        sessions[champion_id] = session
        for plan in champion_plans:
            fingerprint = session.layout_fingerprints.get(plan.skin.skin_number)
            if fingerprint is None:
                raise ChampionLayoutError(
                    f"no Layout fingerprint for {plan.display_name}"
                )
            bind_archive_plan_layout(plan, fingerprint)
    if dictionary_candidate_facts:
        record_fact("dictionaryCandidates", dictionary_candidate_facts)
    return sessions


def assert_prepare_session_source_current(
    session: ChampionPrepareSession,
) -> None:
    actual = capture_wad_file_identity(session.source_wad)
    if actual != session.source_identity:
        raise WadChangedDuringRead(
            session.source_wad,
            session.source_identity,
            actual,
        )
    if (
        session.prepared is not None
        and session.prepared.file_identity != session.source_identity
    ):
        raise WadChangedDuringRead(
            session.source_wad,
            session.source_identity,
            session.prepared.file_identity,
        )


def build_base_parse_context(
    session: ChampionPrepareSession,
    unit: str,
) -> dict[str, Any]:
    normalized_unit = unit.casefold()
    runtime = session.runtime_session
    dictionary_candidates = (
        None
        if runtime is None
        else runtime.hash_skin_candidates
    )
    if dictionary_candidates is None:
        base_path = champion_skin_path(normalized_unit, 0)
        required = session.required_chunks.get(base_path)
        base_path_hash = (
            required.chunk.path_hash
            if required is not None
            else wad_path_hash(base_path)
        )
    else:
        base_record = dictionary_candidates.record_for(
            normalized_unit,
            0,
        )
        if base_record is None:
            raise ChampionLayoutError(
                f"dynamic Layout has no dictionary-bound base for "
                f"unit {normalized_unit!r}"
            )
        base_path = base_record.path
        required = session.required_chunks.get(base_path)
        if required is None:
            raise ChampionLayoutError(
                f"dynamic Layout did not preflight base chunk "
                f"{base_path!r}"
            )
        if required.chunk.path_hash != base_record.path_hash:
            raise ChampionLayoutError(
                f"dynamic Layout base hash differs for {base_path!r}"
            )
        base_path_hash = base_record.path_hash
    toc_digest = (
        session.layout.toc_digest
        if session.layout is not None
        else runtime.toc_digest if runtime is not None else None
    )
    wad_version = (
        session.layout.wad_version
        if session.layout is not None
        else None
    )
    source = session.source_identity
    return {
        "championId": session.identity.champion_id,
        "sourceWad": {
            "path": str(source.resolved_path),
            "device": source.device,
            "inode": source.inode,
            "size": source.size,
            "modifiedNs": source.mtime_ns,
        },
        "wadVersion": wad_version,
        "tocDigest": toc_digest,
        "unit": normalized_unit,
        "basePath": base_path,
        "basePathHash": f"{base_path_hash:016x}",
        "chunk": (
            None
            if required is None
            else serialize_required_chunk_identity(required)
        ),
    }


@timed_function("prepare.total")
def materialize_direct_prepare(
    session: ChampionPrepareSession,
    plans: list[ArchivePlan],
) -> dict[Path, set[str]]:
    prepared = session.prepared
    layout = session.layout
    if prepared is None or layout is None:
        raise ValueError("Direct Prepare requires a direct champion session")
    assert_prepare_session_source_current(session)

    required_hashes: set[int] = set()
    paired_count = 0
    for plan in plans:
        skin_layout = session.skin_layouts.get(plan.skin.skin_number)
        if skin_layout is None:
            raise ChampionLayoutError(
                f"no Direct Prepare layout for {plan.display_name}"
            )
        paired_count += len(skin_layout.paired)
        for state in skin_layout.paired:
            if (
                state.base_path is None
                or state.target_path is None
                or state.base_chunk is None
                or state.target_chunk is None
            ):
                raise ChampionLayoutError(
                    f"paired unit {state.unit!r} has incomplete chunk bindings"
                )
            required_hashes.add(state.base_chunk.path_hash)
            required_hashes.add(state.target_chunk.path_hash)

    ordered_hashes = tuple(sorted(required_hashes))
    count_operation("prepare.direct.read_hashes.calls")
    count_operation("prepare.direct.unique_hashes", len(ordered_hashes))
    count_operation("prepare.direct.paired_units", paired_count)
    count_operation("prepare.direct.skins", len(plans))
    with timed_phase("prepare.direct_read"):
        payloads = prepared.read_hashes(ordered_hashes, validate_bin=True)
    assert_prepare_session_source_current(session)

    result: dict[Path, set[str]] = {}
    for plan in plans:
        skin_layout = session.skin_layouts[plan.skin.skin_number]
        skin_dir = fresh_dir(plan.work_dir)
        found: set[str] = set()
        for state in skin_layout.paired:
            assert state.base_path is not None
            assert state.target_path is not None
            assert state.base_chunk is not None
            assert state.target_chunk is not None
            unit_dir = skin_dir / state.unit
            with timed_phase("prepare.input_write"):
                unit_dir.mkdir(parents=True, exist_ok=True)
                (unit_dir / "skin0.bin").write_bytes(
                    payloads[state.base_chunk.path_hash]
                )
                (unit_dir / f"skin{plan.skin.skin_number}.bin").write_bytes(
                    payloads[state.target_chunk.path_hash]
                )
            found.add(state.unit)
        log(f"  {plan.display_name}: {len(found)} unit(s)")
        result[skin_dir] = found
    count_operation("prepare.direct.input_files", paired_count * 2)
    return result


def materialize_runtime_legacy_prepare(
    session: ChampionPrepareSession,
    plans: list[ArchivePlan],
) -> dict[Path, set[str]]:
    runtime = session.runtime_session
    if runtime is None or runtime.backend != "legacy":
        raise ChampionLayoutError(
            "runtime legacy Prepare requires an explicit legacy session"
        )
    assert_runtime_session_source_current(runtime)

    extracted_root = runtime.legacy_extracted_root
    if extracted_root is None:
        raise ChampionLayoutError(
            "explicit legacy session has no reusable extraction"
        )
    units_by_skin = read_legacy_extracted_units(
        extracted_root,
        sorted({plan.skin.skin_number for plan in plans}),
    )

    materialized: dict[Path, set[str]] = {}
    for plan in plans:
        units = units_by_skin.get(plan.skin.skin_number, [])
        if not units:
            continue
        skin_dir = fresh_dir(plan.work_dir)
        found: set[str] = set()
        for unit, base_payload, target_payload in units:
            base_path = champion_skin_path(unit, 0)
            target_path = champion_skin_path(
                unit,
                plan.skin.skin_number,
            )
            validate_bin_payload(base_payload, base_path)
            validate_bin_payload(target_payload, target_path)
            unit_dir = skin_dir / unit
            with timed_phase("prepare.input_write"):
                unit_dir.mkdir(parents=True)
                (unit_dir / "skin0.bin").write_bytes(base_payload)
                (
                    unit_dir
                    / f"skin{plan.skin.skin_number}.bin"
                ).write_bytes(target_payload)
            found.add(unit)
        materialized[skin_dir] = found
        log(f"  {plan.display_name}: {len(found)} unit(s)")

    assert_runtime_session_source_current(runtime)
    count_operation("prepare.legacy.reused_extractions")
    return materialized


def materialize_legacy_prepare(
    session: ChampionPrepareSession,
    plans: list[ArchivePlan],
    champions_dir: Path,
) -> dict[Path, set[str]]:
    if session.backend != "legacy":
        raise ChampionLayoutError(
            "legacy materialization requires an explicit legacy session"
        )
    count_operation("prepare.legacy.attempts")
    assert_prepare_session_source_current(session)
    try:
        if session.runtime_session is not None:
            prepared = materialize_runtime_legacy_prepare(
                session,
                plans,
            )
        else:
            prepared, _champion_name = prepare_skins(
                [plan.skin for plan in plans],
                champions_dir,
                source_wad=session.source_wad,
                expected_wad_identity=session.source_identity,
                expected_toc_digest=(
                    None
                    if session.layout is None
                    else session.layout.toc_digest
                ),
            )
        assert_prepare_session_source_current(session)
    except BaseException:
        count_operation("prepare.legacy.failures")
        raise
    count_operation("prepare.legacy.successes")
    return prepared


def materialize_pending_plans(
    plans: list[ArchivePlan],
    sessions: Mapping[int, ChampionPrepareSession],
    champions_dir: Path,
) -> dict[Path, set[str]]:
    grouped: dict[int, list[ArchivePlan]] = {}
    for plan in plans:
        grouped.setdefault(plan.skin.champion_id, []).append(plan)

    prepared_map: dict[Path, set[str]] = {}
    for champion_id in sorted(grouped):
        champion_plans = grouped[champion_id]
        session = sessions.get(champion_id)
        if session is None:
            raise ChampionLayoutError(
                f"no Prepare session for champion id {champion_id}"
            )
        champion_name = session.identity.display_name
        entry_word = "entry" if len(champion_plans) == 1 else "entries"
        log(
            f"=== champion {champion_name}: "
            f"{len(champion_plans)} skin {entry_word} total ==="
        )
        with measurement_scope(champion=champion_name):
            if session.backend == "legacy":
                skin_units = materialize_legacy_prepare(
                    session,
                    champion_plans,
                    champions_dir,
                )
            else:
                skin_units = materialize_direct_prepare(
                    session,
                    champion_plans,
                )
        prepared_map.update(skin_units)
    return prepared_map


@timed_function("archive.validate")
def validate_mod_archive(archive_path: Path, wad_name: str) -> dict[str, Any]:
    """Validate the ZIP/Fantome structure and return its parsed metadata."""
    expected_wad = f"WAD/{wad_name}"
    with zipfile.ZipFile(archive_path, "r") as zf:
        names = set(zf.namelist())
        unsafe = [
            name
            for name in names
            if name.startswith(("/", "\\")) or ".." in Path(name).parts
        ]
        if unsafe:
            raise ValueError(f"archive contains unsafe paths: {unsafe}")

        missing = {"META/info.json", expected_wad} - names
        if missing:
            raise ValueError(f"archive is missing required entries: {sorted(missing)}")

        try:
            info = json.loads(zf.read("META/info.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("META/info.json is not valid UTF-8 JSON") from exc
        if not isinstance(info, dict):
            raise ValueError("META/info.json must be a JSON object")
        required_fields = ("Name", "Author", "Version", "Description")
        invalid_fields = [
            field for field in required_fields if not isinstance(info.get(field), str)
        ]
        if invalid_fields:
            raise ValueError(
                f"META/info.json requires string fields: {', '.join(invalid_fields)}"
            )

        with zf.open(expected_wad) as wad_member:
            wad_header = wad_member.read(2)
        if wad_header != b"RW":
            raise ValueError(f"{expected_wad} does not have a WAD header")
    return info


def inspect_archive_for_plan(
    archive_path: Path,
    plan: ArchivePlan,
) -> tuple[bool, str]:
    if not archive_path.is_file():
        return False, "missing"
    try:
        actual_info = validate_mod_archive(archive_path, plan.wad_name)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return False, f"invalid: {exc}"

    mismatched = [
        key
        for key, expected_value in plan.info.items()
        if actual_info.get(key) != expected_value
    ]
    if mismatched:
        return False, f"metadata mismatch: {', '.join(mismatched)}"
    return True, "current"


@timed_function("archive.compare")
def archives_identical(first: Path, second: Path) -> bool:
    if first.stat().st_size != second.stat().st_size:
        return False
    with first.open("rb") as left, second.open("rb") as right:
        while True:
            left_chunk = left.read(1024 * 1024)
            right_chunk = right.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def temporary_archive_path(output_dir: Path, disk_name: str) -> Path:
    temp_file = tempfile.NamedTemporaryFile(
        prefix=f".{disk_name}.",
        suffix=".tmp",
        dir=output_dir,
        delete=False,
    )
    temp_path = Path(temp_file.name)
    temp_file.close()
    return temp_path


def copy_archive_atomically(
    source: Path,
    destination: Path,
    plan: ArchivePlan,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = temporary_archive_path(destination.parent, plan.disk_name)
    try:
        count_operation("archive.copy.attempts", purpose="cache-materialize")
        try:
            with timed_phase("archive.copy"):
                shutil.copyfile(source, temp_path)
        except BaseException:
            count_operation("archive.copy.failures", purpose="cache-materialize")
            raise
        count_operation("archive.copy.successes", purpose="cache-materialize")
        count_operation(
            "archive.copy.bytes",
            temp_path.stat().st_size,
            purpose="cache-materialize",
        )
        is_current, reason = inspect_archive_for_plan(temp_path, plan)
        if not is_current:
            raise ValueError(f"copied archive failed validation: {reason}")
        temp_path.replace(destination)
    finally:
        temp_path.unlink(missing_ok=True)


@timed_function("cache.preflight")
def preflight_archive_plans(
    plans: list[ArchivePlan],
    *,
    force: bool,
) -> ArchivePreflight:
    result = ArchivePreflight()
    for plan in plans:
        metric_labels = {
            "champion": plan.skin.champion_name,
            "skin": plan.display_name,
            "skin_number": plan.skin.skin_number,
        }
        if force:
            log(f"force rebuild: {plan.display_name}")
            count_operation("cache.archive.force_bypass", **metric_labels)
            result.pending.append(plan)
            continue

        inspections: dict[str, tuple[bool, str]] = {}

        def inspect(extension: str) -> tuple[bool, str]:
            if extension not in inspections:
                inspections[extension] = inspect_archive_for_plan(
                    plan.path_for(extension),
                    plan,
                )
            return inspections[extension]

        requested_are_current = all(
            inspect(extension)[0]
            for extension in plan.requested_extensions
        )
        if requested_are_current:
            if (
                len(plan.requested_extensions) == 2
                and not archives_identical(
                    plan.path_for(plan.requested_extensions[0]),
                    plan.path_for(plan.requested_extensions[1]),
                )
            ):
                log(f"cache miss (ZIP/Fantome differ): {plan.display_name}")
                count_operation(
                    "cache.archive.misses",
                    reason="format-difference",
                    **metric_labels,
                )
                result.pending.append(plan)
                continue
            log(f"cache hit: {plan.display_name}")
            count_operation("cache.archive.hits", **metric_labels)
            result.cache_hits.append(plan)
            continue

        valid_extensions = [
            extension
            for extension in ARCHIVE_FORMATS
            if inspect(extension)[0]
        ]
        if len(valid_extensions) == 1:
            source_extension = valid_extensions[0]
            source = plan.path_for(source_extension)
            missing_requested = [
                extension
                for extension in plan.requested_extensions
                if not inspect(extension)[0]
            ]
            try:
                for extension in missing_requested:
                    copy_archive_atomically(source, plan.path_for(extension), plan)
            except (OSError, ValueError) as exc:
                log(f"cache copy failed for {plan.display_name}: {exc}")
            else:
                if all(
                    inspect_archive_for_plan(plan.path_for(extension), plan)[0]
                    for extension in plan.requested_extensions
                ):
                    log(
                        f"cache materialized from {source_extension}: "
                        f"{plan.display_name}"
                    )
                    count_operation(
                        "cache.archive.materialized",
                        **metric_labels,
                    )
                    result.materialized.append(plan)
                    continue

        reasons = {
            extension: inspect(extension)[1]
            for extension in plan.requested_extensions
            if not inspect(extension)[0]
        }
        log(f"cache miss: {plan.display_name} ({reasons})")
        count_operation("cache.archive.misses", **metric_labels)
        result.pending.append(plan)
    return result


@timed_function("archive.total")
def write_mod_archives(
    step4: Path,
    wad_dir: Path,
    meta_dir: Path,
    output_dir: Path,
    disk_name: str,
    wad_name: str,
    archive_format: str,
) -> list[Path]:
    """Write validated ZIP/Fantome outputs atomically, compressing only once."""
    extensions = archive_extensions(archive_format)
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary_paths: dict[str, Path] = {}

    try:
        primary_temp = temporary_archive_path(output_dir, disk_name)
        temporary_paths[extensions[0]] = primary_temp
        count_operation("archive.compress.attempts")
        try:
            with timed_phase("archive.compress"):
                with zipfile.ZipFile(primary_temp, "w", zipfile.ZIP_DEFLATED) as zf:
                    for folder in (wad_dir, meta_dir):
                        for item in folder.rglob("*"):
                            if item.is_file():
                                zf.write(item, item.relative_to(step4).as_posix())
        except BaseException:
            count_operation("archive.compress.failures")
            raise
        count_operation("archive.compress.successes")
        count_operation("archive.compress.output_bytes", primary_temp.stat().st_size)
        validate_mod_archive(primary_temp, wad_name)

        # A Fantome file is the same ZIP container with a semantic extension.
        # For "both", copy the validated bytes instead of compressing twice.
        for extension in extensions[1:]:
            duplicate_temp = temporary_archive_path(output_dir, disk_name)
            temporary_paths[extension] = duplicate_temp
            count_operation("archive.copy.attempts", purpose="format-sibling")
            try:
                with timed_phase("archive.copy"):
                    shutil.copyfile(primary_temp, duplicate_temp)
            except BaseException:
                count_operation("archive.copy.failures", purpose="format-sibling")
                raise
            count_operation("archive.copy.successes", purpose="format-sibling")
            count_operation(
                "archive.copy.bytes",
                duplicate_temp.stat().st_size,
                purpose="format-sibling",
            )
            validate_mod_archive(duplicate_temp, wad_name)

        outputs: list[Path] = []
        for extension in extensions:
            final_path = output_dir / f"{disk_name}.{extension}"
            temporary_paths[extension].replace(final_path)
            outputs.append(final_path)
        return outputs
    finally:
        for temp_path in temporary_paths.values():
            temp_path.unlink(missing_ok=True)


@timed_function("skin.package")
def package_processed_skin(
    skin_dir: Path,
    step3: Path,
    display_name: str,
    version: str,
    champion_name: str,
    base_skin_name: str,
    archive_format: str,
    *,
    info_override: dict[str, Any] | None = None,
    is_chroma: bool | None = None,
    wad_name_override: str | None = None,
) -> None:
    disk_name = skin_dir.name
    step4 = fresh_dir(skin_dir / "step4")
    log("--- step 4: build WAD + META ---")
    wad_dir = step4 / "WAD"
    wad_dir.mkdir()
    wad_name = (
        wad_name_override
        if wad_name_override is not None
        else f"{normalize_champion_name(champion_name)}.wad.client"
    )
    wad_path = wad_dir / wad_name
    run_wad_make(step3, wad_path)
    log(f"wad = {wad_path.relative_to(skin_dir)}")

    meta_dir = step4 / "META"
    meta_dir.mkdir()
    info = (
        dict(info_override)
        if info_override is not None
        else build_mod_info(display_name, version)
    )
    info_path = meta_dir / "info.json"
    info_path.write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log(f"wrote {info_path.relative_to(skin_dir)}")

    log(f"--- package WAD + META ({archive_format}) ---")
    if is_chroma is None:
        is_chroma = disk_name != sanitize_for_windows(base_skin_name)
    archive_dir = archive_output_directory(
        champion_name,
        base_skin_name,
        disk_name,
        is_chroma,
    )
    archive_paths = write_mod_archives(
        step4,
        wad_dir,
        meta_dir,
        archive_dir,
        disk_name,
        wad_path.name,
        archive_format,
    )
    for archive_path in archive_paths:
        log(f"final {archive_path.suffix[1:]} = {display_path(archive_path)}")


def resolve_all_champion_skins(
    champion_query: str,
    champions_dir: Path,
    *,
    wad_mode: str = "direct",
    session_pool: ChampionSessionPool | None = None,
) -> list[LocalSkin]:
    champion_name, wad_path = find_champion_wad(champion_query, champions_dir)
    with measurement_scope(champion=champion_name):
        catalog = get_runtime_catalog(
            champion_name,
            wad_path,
            champions_dir,
            wad_mode=wad_mode,
            session_pool=session_pool,
        )
    base_skins = [
        skin
        for skin in catalog.skins
        if skin.skin_number != 0 and not skin.is_chroma
    ]
    selected = [
        skin
        for skin in catalog.skins
        if skin.skin_number != 0
    ]
    if not selected:
        sys.exit(f"未找到 {champion_name!r} 的任何皮肤")
    log(
        f"{champion_name} 共有 {len(base_skins)} 个基础皮肤，"
        f"{len(selected)} 个总条目（含炫彩）:"
    )
    for skin in base_skins:
        chroma_count = sum(
            1
            for item in catalog.skins
            if item.parent_skin_number == skin.skin_number
        )
        suffix = f" (+{chroma_count} chromas)" if chroma_count else ""
        log(f"  - skin{skin.skin_number}: {skin.display_name}{suffix}")
    return selected


def prompt_skin_names(
    champions_dir: Path,
    *,
    wad_mode: str = "direct",
    session_pool: ChampionSessionPool | None = None,
) -> list[LocalSkin]:
    """Ask the user how to select skins and return local-WAD skin selections.

    Mode 1 (champion): user types a champion name; we expand to all of that
    champion's non-classic skins (base skins and chromas) and ask for confirmation.
    Mode 2 (skin):     user types exact skin/chroma names, canonical full skin
                       IDs, or ``skin<N> <Champion>``, comma-separated.
    """
    while True:
        choice = input(
            "选择模式 / Choose mode:\n"
            "  1) 英雄 (champion) - 处理该英雄的所有皮肤 / process every skin of one champion\n"
            "  2) 皮肤 (skin)     - 输入精确名称或 ID / exact name or ID\n"
            "请输入 [1/2]: "
        ).strip()
        if choice in ("1", "2"):
            break
        log("请输入 1 或 2")

    if choice == "1":
        champion_name = ""
        while not champion_name:
            champion_name = input("输入英雄名 / Enter champion name (e.g. Annie): ").strip()
        selected = resolve_all_champion_skins(
            champion_name,
            champions_dir,
            wad_mode=wad_mode,
            session_pool=session_pool,
        )
        confirm = input(
            f"确认处理全部 {len(selected)} 个条目？ / Process all {len(selected)} entries? [y/N]: "
        ).strip().lower()
        if confirm not in ("y", "yes"):
            sys.exit("已取消 / cancelled")
        return selected

    raw = ""
    while not raw:
        raw = input(
            "输入精确皮肤名、完整 skin ID 或 skin<N> 英雄名（多个用逗号分隔），"
            "e.g. Lunar Beast Annie / 1013 / skin13 Annie: "
        ).strip()
    selected: list[LocalSkin] = []
    seen: set[tuple[str, int]] = set()
    for name in [n.strip() for n in raw.split(",") if n.strip()]:
        matches = resolve_local_skin_name(
            name,
            champions_dir,
            wad_mode=wad_mode,
            session_pool=session_pool,
        )
        log(f"matched {len(matches)} local WAD entr{'y' if len(matches) == 1 else 'ies'} for {name!r}:")
        for skin in matches:
            log(f"  - {skin.champion_name} skin{skin.skin_number}: {skin.display_name}")
            key = (skin.champion_name.lower(), skin.skin_number)
            if key in seen:
                continue
            seen.add(key)
            selected.append(skin)
    return selected


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate locally rebased League skins as ZIP and/or Fantome archives."
    )
    parser.add_argument(
        "--format",
        dest="archive_format",
        choices=(*ARCHIVE_FORMATS, "both"),
        default="zip",
        help="output archive format (default: zip)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild selected skins even when current output archives exist",
    )
    parser.add_argument(
        "--wad-mode",
        choices=WAD_MODES,
        default="direct",
        help=(
            "champion WAD preparation backend "
            "(default: direct; legacy keeps bundled wad-extract compatibility)"
        ),
    )
    parser.add_argument(
        "--hash-update",
        choices=HASH_UPDATE_MODES,
        default="never",
        help=(
            "CommunityDragon hashes.game update policy "
            "(default: never/offline; use auto to check or force to redownload)"
        ),
    )
    parser.add_argument(
        "--champion",
        help="non-interactively process every non-classic skin for one champion",
    )
    parser.add_argument(
        "--metrics-json",
        type=Path,
        help="write structured timing and operation metrics to this JSON path",
    )
    return parser.parse_args(argv)


def prepare_batched_unit_work(
    plans: list[ArchivePlan],
    prepared_map: Mapping[Path, set[str]],
    sessions: Mapping[int, ChampionPrepareSession],
    base_parse_cache: ProcessBaseParseCache,
) -> tuple[dict[str, BatchedBaseWork], list[BatchedUnitWork]]:
    base_work: dict[str, BatchedBaseWork] = {}
    base_keys: dict[str, tuple[BaseParseKey, int]] = {}
    known_base_keys: set[str] = set()
    unit_work: list[BatchedUnitWork] = []
    for plan in plans:
        skin_dir = plan.work_dir
        only_units = prepared_map[skin_dir]
        session = sessions.get(plan.skin.champion_id)
        if session is None:
            raise ChampionLayoutError(
                f"no Prepare session for champion id {plan.skin.champion_id}"
            )
        units = find_input_units(skin_dir, only_units)
        step1 = fresh_dir(skin_dir / "step1")
        step2 = fresh_dir(skin_dir / "step2")
        step3 = fresh_dir(skin_dir / "step3")
        log(f"=== batch staging: {plan.display_name} ===")
        log(f"units         = {[item[2] for item in units]}")

        for base_bin, target_bin, unit in units:
            normalized_unit = unit.casefold()
            unit_step1 = step1 / unit
            unit_step2 = step2 / unit
            unit_step1.mkdir()
            unit_step2.mkdir()
            context = build_base_parse_context(session, unit)
            with timed_phase("cache.base_parse.identity"):
                key_identity = canonical_json_sha256(
                    {
                        "context": context,
                        "ritobin": (
                            base_parse_cache.tool_identity().as_json()
                        ),
                    }
                )
            cached_key = base_keys.get(key_identity)
            if cached_key is None:
                with timed_phase("cache.base_parse.key"):
                    base_payload = base_bin.read_bytes()
                    key = base_parse_cache.build_key(
                        context,
                        base_payload,
                    )
                base_keys[key_identity] = (key, len(base_payload))
                count_operation("cache.base_parse.key_builds")
                count_operation("cache.base_parse.base_payload_reads")
            else:
                key, expected_size = cached_key
                if base_bin.stat().st_size != expected_size:
                    raise BaseCacheError(
                        f"coalesced base size changed for unit "
                        f"{normalized_unit!r}"
                    )
                count_operation("cache.base_parse.key_coalesced")

            if key.digest in known_base_keys:
                count_operation("cache.base_parse.hits")
                count_operation("cache.base_parse.coalesced")
            else:
                snapshot, cache_tier = (
                    base_parse_cache.get_with_tier(key)
                )
                if snapshot is not None:
                    count_operation("cache.base_parse.hits")
                    if cache_tier == "persistent":
                        count_operation(
                            "cache.base_parse.persistent_hits"
                        )
                else:
                    count_operation("cache.base_parse.misses")
                    if base_parse_cache.persistent_cache is not None:
                        count_operation(
                            "cache.base_parse.persistent_misses",
                            status=cache_tier,
                        )
                    base_json = unit_step1 / f"{base_bin.stem}.json"
                    relative = (
                        f"champion-{plan.skin.champion_id}/"
                        f"skin-{plan.skin.skin_number:03d}/"
                        f"{normalized_unit}/base-{key.digest}.bin"
                    )
                    base_work[key.digest] = BatchedBaseWork(
                        key=key,
                        source_bin=base_bin,
                        parsed_json=base_json,
                        batch_item=RitobinBatchItem(
                            source=base_bin,
                            destination=base_json,
                            relative_path=relative,
                        ),
                    )
                known_base_keys.add(key.digest)

            target_json = unit_step1 / f"{target_bin.stem}.json"
            modified_json = unit_step2 / f"{target_bin.stem}_modified.json"
            final_bin = (
                step3
                / "data"
                / "characters"
                / normalized_unit
                / "skins"
                / "skin0.bin"
            )
            relative = (
                f"champion-{plan.skin.champion_id}/"
                f"skin-{plan.skin.skin_number:03d}/"
                f"{normalized_unit}/target.bin"
            )
            unit_work.append(
                BatchedUnitWork(
                    plan=plan,
                    unit=unit,
                    base_key=key,
                    target_bin=target_bin,
                    target_json=target_json,
                    modified_json=modified_json,
                    final_bin=final_bin,
                    bin_to_json_item=RitobinBatchItem(
                        source=target_bin,
                        destination=target_json,
                        relative_path=relative,
                    ),
                )
            )
    return base_work, unit_work


@timed_function("conversion.batch.total")
def convert_batched_unit_work(
    base_work: Mapping[str, BatchedBaseWork],
    unit_work: list[BatchedUnitWork],
    base_parse_cache: ProcessBaseParseCache,
) -> None:
    count_operation("ritobin.batch.base_files", len(base_work))
    count_operation("ritobin.batch.target_files", len(unit_work))
    run_ritobin_batches(
        [
            *(item.batch_item for item in base_work.values()),
            *(item.bin_to_json_item for item in unit_work),
        ],
        in_fmt="bin",
        out_fmt="json",
    )

    for item in base_work.values():
        with timed_phase("rebase.base_json_parse"):
            base_data = json.loads(
                item.parsed_json.read_text(encoding="utf-8")
            )
            snapshot = extract_base_rebase_snapshot(base_data)
        if base_parse_cache.put(item.key, snapshot):
            count_operation("cache.base_parse.persistent_stores")
        count_operation("cache.base_parse.stores")

    json_to_bin: list[RitobinBatchItem] = []
    for item in unit_work:
        snapshot = base_parse_cache.get(item.base_key)
        if snapshot is None:
            raise BaseCacheError(
                f"missing parsed base snapshot {item.base_key.digest}"
            )
        with measurement_scope(
            champion=item.plan.skin.champion_name,
            skin=item.plan.display_name,
            skin_number=item.plan.skin.skin_number,
            unit=item.unit,
        ):
            with timed_phase("rebase.target_json_parse"):
                target_data = json.loads(
                    item.target_json.read_text(encoding="utf-8")
                )
            with timed_phase("rebase.modify_json"):
                modified_data = apply_base_rebase_snapshot(
                    snapshot,
                    target_data,
                )
            item.modified_json.write_text(
                json.dumps(
                    modified_data,
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        relative = (
            f"champion-{item.plan.skin.champion_id}/"
            f"skin-{item.plan.skin.skin_number:03d}/"
            f"{item.unit.casefold()}/modified.json"
        )
        json_to_bin.append(
            RitobinBatchItem(
                source=item.modified_json,
                destination=item.final_bin,
                relative_path=relative,
            )
        )

    count_operation("ritobin.batch.output_files", len(json_to_bin))
    run_ritobin_batches(
        json_to_bin,
        in_fmt="json",
        out_fmt="bin",
    )
    for item in unit_work:
        validate_bin_payload(
            item.final_bin.read_bytes(),
            (
                f"data/characters/{item.unit.casefold()}/"
                "skins/skin0.bin"
            ),
        )


def package_batched_plans(
    plans: list[ArchivePlan],
    archive_format: str,
) -> None:
    for plan in plans:
        with measurement_scope(
            champion=plan.skin.champion_name,
            skin=plan.display_name,
            skin_number=plan.skin.skin_number,
        ), timed_phase("skin.total"):
            package_processed_skin(
                plan.work_dir,
                plan.work_dir / "step3",
                plan.display_name,
                plan.version,
                plan.skin.champion_name,
                plan.skin.base_display_name,
                archive_format,
                info_override=plan.info,
                is_chroma=plan.skin.is_chroma,
                wad_name_override=plan.wad_name,
            )
            count_operation("skins.generated")


def execute_batched_conversions(
    plans: list[ArchivePlan],
    prepared_map: Mapping[Path, set[str]],
    sessions: Mapping[int, ChampionPrepareSession],
    archive_format: str,
    *,
    persistent_cache: PersistentJsonCache | None = None,
) -> ProcessBaseParseCache:
    cache = ProcessBaseParseCache(
        RITOBIN_CLI,
        rebase_schema=REBASE_SCHEMA_VERSION,
        parser_schema=BASE_PARSE_PARSER_SCHEMA_VERSION,
        persistent_cache=persistent_cache,
    )
    base_work, unit_work = prepare_batched_unit_work(
        plans,
        prepared_map,
        sessions,
        cache,
    )
    convert_batched_unit_work(
        base_work,
        unit_work,
        cache,
    )
    package_batched_plans(plans, archive_format)
    return cache


def execute_selections(
    selections: list[LocalSkin],
    champions_dir: Path,
    *,
    archive_format: str,
    force: bool,
    wad_mode: str = "direct",
    session_pool: ChampionSessionPool | None = None,
) -> None:
    requested_extensions = archive_extensions(archive_format)
    record_fact(
        "selection",
        [
            {
                "champion": skin.champion_name,
                "championId": skin.champion_id,
                "skinNumber": skin.skin_number,
                "fullSkinId": skin.full_skin_id,
                "displayName": skin.display_name,
                "isChroma": skin.is_chroma,
            }
            for skin in selections
        ],
    )
    for skin in selections:
        count_operation(
            "skins.selected",
            champion=skin.champion_name,
            skin_number=skin.skin_number,
        )
    # Bind every plan to its actual per-skin Layout and complete required chunk
    # identities before archive preflight. Reliable current checksums need only
    # the TOC; older/zero checksums are content-hashed into Prepared's read cache.
    plans = build_archive_plans(
        selections,
        champions_dir,
        requested_extensions,
        session_pool=session_pool,
    )
    sessions = build_prepare_sessions(
        plans,
        champions_dir,
        wad_mode=wad_mode,
        session_pool=session_pool,
    )
    source_wads: dict[str, dict[str, Any]] = {}
    for plan in plans:
        key = str(plan.source_wad.resolve())
        if key in source_wads:
            continue
        stat = plan.source_wad.stat()
        source_wads[key] = {
            "path": key,
            "size": stat.st_size,
            "modifiedNs": stat.st_mtime_ns,
        }
    record_fact("sourceWads", list(source_wads.values()))
    persistent_cache = (
        None
        if session_pool is None
        else session_pool.persistent_cache
    )
    preflight = preflight_archive_plans(plans, force=force)

    if not preflight.pending:
        log(
            "nothing to do — all requested "
            f"{archive_format} archives are current"
        )
        log(
            f"selected={len(plans)}, "
            f"cache_hits={len(preflight.cache_hits)}, "
            f"materialized={len(preflight.materialized)}, generated=0"
        )
        if persistent_cache is not None:
            record_fact(
                "persistentCache",
                persistent_cache.fact(),
            )
        return

    # Only cache misses materialize input. Direct mode reads one
    # de-duplicated chunk plan per champion and never invokes wad-extract.
    prepared_map = materialize_pending_plans(
        preflight.pending,
        sessions,
        champions_dir,
    )

    missing_bins = [
        plan.display_name
        for plan in preflight.pending
        if plan.work_dir not in prepared_map
    ]
    if missing_bins:
        sys.exit(
            "no matching skin bins were found in the source WADs for: "
            + ", ".join(missing_bins)
        )
    pending = preflight.pending

    log(f"pending: {[plan.display_name for plan in pending]}")
    base_parse_cache = execute_batched_conversions(
        pending,
        prepared_map,
        sessions,
        archive_format,
        persistent_cache=persistent_cache,
    )

    record_fact("baseParseCache", base_parse_cache.fact())
    if persistent_cache is not None:
        record_fact("persistentCache", persistent_cache.fact())
    generated_selected = len(pending)
    log("all done.")
    log(
        f"selected={len(plans)}, cache_hits={len(preflight.cache_hits)}, "
        f"materialized={len(preflight.materialized)}, generated={generated_selected}"
    )


def run(args: argparse.Namespace) -> None:
    log(f"script dir    = {SCRIPT_DIR}")
    log(f"input root    = {INPUT_ROOT}")
    log(f"output root   = {OUTPUT_ROOT}")
    log(f"cache root    = {CACHE_ROOT}")
    log(f"archive format = {args.archive_format}")
    log(f"force rebuild = {args.force}")
    wad_mode = getattr(args, "wad_mode", "direct")
    log(f"WAD mode      = {wad_mode}")
    hash_update_mode = getattr(args, "hash_update", "never")
    log(f"hash update   = {hash_update_mode}")

    try:
        with timed_phase("hash.update"):
            hash_update = ensure_latest_hashes_game(
                HASHES_GAME_PATH,
                HASH_UPDATE_STATE_PATH,
                mode=hash_update_mode,
            )
    except HashUpdateError as exc:
        sys.exit(f"hashes.game update policy failed: {exc}")
    record_fact("hashDictionary", hash_update.fact())
    count_operation(f"hash.update.{hash_update.action}")
    log(
        f"hash dictionary = {hash_update.action} "
        f"({display_path(hash_update.path)})"
    )
    try:
        with timed_phase("hash.skin_index"):
            hash_skin_index = ensure_hash_skin_index(
                hash_update.path,
                HASH_SKIN_INDEX_PATH,
                expected_source_sha256=hash_update.sha256,
                expected_source_size=hash_update.size,
            )
    except HashSkinIndexError as exc:
        sys.exit(f"could not establish the hashes.game skin index: {exc}")
    record_fact("hashSkinIndex", hash_skin_index.fact())
    count_operation(f"hash.skin_index.{hash_skin_index.action}")
    log(
        f"hash skin index = {hash_skin_index.action} "
        f"({len(hash_skin_index.index.records)} records)"
    )

    INPUT_ROOT.mkdir(parents=True, exist_ok=True)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    champions_dir = ensure_lol_path()
    log(f"champions dir = {champions_dir}")
    record_fact(
        "run",
        {
            "archiveFormat": args.archive_format,
            "force": args.force,
            "wadMode": wad_mode,
            "hashUpdateMode": hash_update_mode,
            "championsDir": str(champions_dir.resolve()),
            "inputRoot": str(INPUT_ROOT),
            "outputRoot": str(OUTPUT_ROOT),
            "cacheRoot": str(CACHE_ROOT),
        },
    )

    persistent_cache = PersistentJsonCache(DERIVED_CACHE_ROOT)
    last_change: WadChangedDuringRead | None = None
    for source_attempt in range(2):
        try:
            with ChampionSessionPool(
                champions_dir,
                wad_mode,
                persistent_cache=persistent_cache,
                hash_skin_index=hash_skin_index.index,
            ) as session_pool:
                champion = getattr(args, "champion", None)
                if champion:
                    with timed_phase("pipeline.noninteractive"):
                        selections = resolve_all_champion_skins(
                            champion,
                            champions_dir,
                            wad_mode=wad_mode,
                            session_pool=session_pool,
                        )
                        execute_selections(
                            selections,
                            champions_dir,
                            archive_format=args.archive_format,
                            force=args.force,
                            wad_mode=wad_mode,
                            session_pool=session_pool,
                        )
                    return

                selections = prompt_skin_names(
                    champions_dir,
                    wad_mode=wad_mode,
                    session_pool=session_pool,
                )
                with timed_phase("pipeline.execute"):
                    execute_selections(
                        selections,
                        champions_dir,
                        archive_format=args.archive_format,
                        force=args.force,
                        wad_mode=wad_mode,
                        session_pool=session_pool,
                    )
                return
        except WadChangedDuringRead as exc:
            last_change = exc
            count_operation(
                "pipeline.source_change_retries",
                attempt=source_attempt + 1,
            )
            if source_attempt == 0:
                log(
                    "source WAD changed during the champion session; "
                    "discarding derived state and restarting once"
                )
    assert last_change is not None
    raise last_change


def build_metrics_report(
    recorder: TimingRecorder,
    operations: OperationRecorder,
    *,
    status: str,
    error: BaseException | None,
) -> dict[str, Any]:
    error_payload: dict[str, str] | None = None
    if error is not None:
        error_payload = {
            "type": type(error).__name__,
            "message": str(error),
        }
    return {
        "schemaVersion": METRICS_SCHEMA_VERSION,
        "status": status,
        "error": error_payload,
        "timing": {
            "summary": recorder.summary(),
            "samples": recorder.records(),
        },
        "operations": operations.records(),
        "facts": operations.facts,
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


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    recorder = TimingRecorder()
    operations = OperationRecorder()
    status = "success"
    error: BaseException | None = None
    with use_timings(recorder), use_operations(operations):
        try:
            # This wall timer intentionally includes time waiting for interactive
            # input. Individual catalog/extract/tool phases exclude prompt time.
            with timed_phase("run.wall"):
                run(args)
        except BaseException as exc:
            status = "failed"
            error = exc
            raise
        finally:
            for line in recorder.format_summary():
                log(line, "timing")
            metrics_json = getattr(args, "metrics_json", None)
            if metrics_json is not None:
                try:
                    write_json_atomically(
                        metrics_json,
                        build_metrics_report(
                            recorder,
                            operations,
                            status=status,
                            error=error,
                        ),
                    )
                except OSError as exc:
                    log(f"failed to write metrics JSON: {exc}", "metrics")


if __name__ == "__main__":
    main()
