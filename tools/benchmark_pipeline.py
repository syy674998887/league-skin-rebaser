"""Run the fixed all-skins benchmark without touching normal project outputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POOL = REPO_ROOT / "benchmarks" / "pools" / "upgrade-v2.json"
DEFAULT_WORK_ROOT = REPO_ROOT / ".benchmarks"
SCENARIOS = (
    "app-cold-build",
    "output-cache-hit",
    "derived-warm-build",
)
RESULT_SCHEMA_VERSION = 2
PIPELINE_METRICS_SCHEMA_VERSION = 1
SCRATCH_SENTINEL = ".league-skin-rebaser-benchmark-v1"
PHASE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SCOPE_LABELS = {"champion", "skin", "skin_number", "unit"}
BYTE_VOLUME_OPERATION_NAMES = frozenset(
    {
        "archive.compress.output_bytes",
        "archive.copy.bytes",
        "wad.copy.bytes",
        "wad.chunk.compressed_bytes",
        "wad.chunk.decompressed_bytes",
    }
)
TOOL_RUNTIME_RELATIVE_PATHS = (
    Path("bin") / "ritobin_cli.exe",
    Path("bin") / "hashes" / "hashes.binentries.txt",
    Path("bin") / "hashes" / "hashes.binfields.txt",
    Path("bin") / "hashes" / "hashes.binhashes.txt",
    Path("bin") / "hashes" / "hashes.bintypes.txt",
    Path("bin") / "hashes" / "hashes.game.txt.0",
    Path("bin") / "hashes" / "hashes.game.txt.1",
    Path("bin") / "hashes" / "hashes.lcu.txt",
    Path("cslol-tools") / "wad-extract.exe",
    Path("cslol-tools") / "hashes.game.txt",
    Path("cslol-tools") / "wad-make.exe",
)


def sha256_file(path: Path) -> str:
    resolved = path.resolve(strict=True)
    before = resolved.stat()
    digest = hashlib.sha256()
    total = 0
    with resolved.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            total += len(block)
            digest.update(block)
    after = resolved.stat()
    before_signature = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_signature = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if before_signature != after_signature or total != after.st_size:
        raise OSError(f"file changed while hashing: {resolved}")
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_with_identity(path: Path) -> tuple[Any, dict[str, Any]]:
    resolved = path.resolve(strict=True)
    before = resolved.stat()
    raw = resolved.read_bytes()
    after = resolved.stat()
    before_signature = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_signature = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if before_signature != after_signature or len(raw) != after.st_size:
        raise ValueError(f"JSON file changed while it was being read: {resolved}")
    return json.loads(raw.decode("utf-8")), {
        "path": str(resolved),
        "size": len(raw),
        "modifiedNs": after.st_mtime_ns,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def write_json_atomically(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    attributes = getattr(path.stat(), "st_file_attributes", 0)
    return bool(attributes & 0x400)


def ensure_scratch_root(root: Path, *, allow_initialize: bool) -> Path:
    absolute = root.absolute()
    if absolute.exists() and _is_reparse_point(absolute):
        raise ValueError(f"benchmark root cannot be a reparse point: {absolute}")
    resolved = root.resolve()
    sentinel = resolved / SCRATCH_SENTINEL
    if sentinel.is_file():
        return resolved
    if not allow_initialize:
        raise ValueError(
            f"custom --work-root must already contain {SCRATCH_SENTINEL}: "
            f"{resolved}"
        )
    resolved.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(
        "league-skin-rebaser benchmark scratch directory\n",
        encoding="utf-8",
    )
    return resolved


def safe_reset_directory(target: Path, allowed_root: Path) -> None:
    root = ensure_scratch_root(allowed_root, allow_initialize=False)
    lexical_target = target.absolute()
    try:
        lexical_relative = lexical_target.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"refusing to reset path outside benchmark root: {lexical_target}"
        ) from exc
    if (
        len(lexical_relative.parts) != 3
        or lexical_relative.parts[0] not in {"work", "raw"}
        or PHASE_RE.fullmatch(lexical_relative.parts[1]) is None
        or not lexical_relative.parts[2].isdigit()
        or int(lexical_relative.parts[2]) <= 0
    ):
        raise ValueError(
            "refusing to reset anything except "
            "<benchmark-root>/{work,raw}/<phase>/<championId>: "
            f"{lexical_target}"
        )

    current = root
    for part in lexical_relative.parts:
        current = current / part
        if current.exists() and _is_reparse_point(current):
            raise ValueError(f"refusing to reset through a reparse point: {current}")

    resolved = lexical_target.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError(f"refusing to reset path outside benchmark root: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def reject_reparse_tree(path: Path) -> None:
    pending = [path]
    while pending:
        current = pending.pop()
        if current.is_symlink() or (
            current.exists() and _is_reparse_point(current)
        ):
            raise ValueError(f"benchmark path cannot be a reparse point: {current}")
        if current.is_dir():
            pending.extend(current.iterdir())


def prepare_scenario_directories(
    champion_root: Path,
    scenario: str,
) -> tuple[Path, Path, Path]:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown benchmark scenario: {scenario}")
    lexical_root = champion_root.absolute()
    reject_reparse_tree(lexical_root)
    root = lexical_root.resolve()
    if not root.is_dir():
        raise ValueError(f"benchmark champion root does not exist: {root}")

    input_root = root / "input"
    output_root = root / "output"
    cache_root = root / "cache"
    roots = (input_root, output_root, cache_root)
    for path in roots:
        if path.is_symlink() or (path.exists() and _is_reparse_point(path)):
            raise ValueError(f"benchmark path cannot be a reparse point: {path}")
        if path.resolve().parent != root:
            raise ValueError(f"benchmark scenario path escaped champion root: {path}")

    if scenario == "derived-warm-build":
        for path in (input_root, output_root):
            if path.exists():
                reject_reparse_tree(path)
                shutil.rmtree(path)

    for path in roots:
        path.mkdir(parents=True, exist_ok=True)
    return roots


def scenario_environment(
    input_root: Path,
    output_root: Path,
    cache_root: Path,
) -> dict[str, str]:
    env = os.environ.copy()
    env["LEAGUE_SKIN_REBASER_INPUT_ROOT"] = str(input_root)
    env["LEAGUE_SKIN_REBASER_OUTPUT_ROOT"] = str(output_root)
    env["LEAGUE_SKIN_REBASER_CACHE_ROOT"] = str(cache_root)
    return env


def validate_phase(phase: str) -> str:
    if PHASE_RE.fullmatch(phase) is None:
        raise ValueError(
            "phase must be a 1-64 character lowercase slug using "
            "letters, digits, '.', '_' or '-'"
        )
    return phase


def resolve_scenarios(raw: str, *, include_derived_warm: bool) -> list[str]:
    parts = [item.strip() for item in raw.split(",")]
    if not parts or any(not item for item in parts):
        raise ValueError("scenarios must be a non-empty comma-separated list")
    if len(parts) != len(set(parts)):
        raise ValueError(f"duplicate scenarios are not allowed: {parts}")
    unknown = sorted(set(parts) - set(SCENARIOS))
    if unknown:
        raise ValueError(f"unknown scenarios: {unknown}")
    if "derived-warm-build" in parts:
        raise ValueError(
            "derived-warm-build is opt-in; use --include-derived-warm "
            "instead of adding it to --scenarios"
        )
    allowed_prefixes = [
        ["app-cold-build"],
        ["app-cold-build", "output-cache-hit"],
    ]
    if parts not in allowed_prefixes:
        raise ValueError(
            "scenarios must follow the canonical order: "
            "app-cold-build,output-cache-hit"
        )
    if include_derived_warm:
        if parts != allowed_prefixes[-1]:
            raise ValueError(
                "--include-derived-warm requires both app-cold-build and "
                "output-cache-hit"
            )
        return [*parts, "derived-warm-build"]
    return parts


def git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={REPO_ROOT.as_posix()}", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def expand_skin_set(champion: dict[str, Any]) -> list[int]:
    spec = champion.get("skinSet")
    if not isinstance(spec, dict):
        raise ValueError(f"champion {champion.get('championId')} has no skinSet")
    ranges = spec.get("ranges")
    excluded = spec.get("exclude")
    if not isinstance(ranges, list) or not ranges:
        raise ValueError("skinSet.ranges must be a non-empty list")
    if (
        not isinstance(excluded, list)
        or any(
            not isinstance(value, int) or not 1 <= value <= 999
            for value in excluded
        )
        or excluded != sorted(set(excluded))
    ):
        raise ValueError(
            "skinSet.exclude must contain sorted unique integers in 1..999"
        )
    numbers: set[int] = set()
    for item in ranges:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(value, int) for value in item)
            or not 1 <= item[0] <= 999
            or not 1 <= item[1] <= 999
            or item[1] < item[0]
        ):
            raise ValueError(f"invalid skin range: {item!r}")
        expanded = set(range(item[0], item[1] + 1))
        overlap = numbers & expanded
        if overlap:
            raise ValueError(f"overlapping skin ranges: {sorted(overlap)}")
        numbers.update(expanded)
    unknown_exclusions = set(excluded) - numbers
    if unknown_exclusions:
        raise ValueError(f"skin exclusions are outside ranges: {unknown_exclusions}")
    return sorted(numbers - set(excluded))


def expected_full_skin_ids(champion: dict[str, Any]) -> list[int]:
    champion_id = int(champion["championId"])
    return [
        champion_id * 1000 + skin_number
        for skin_number in expand_skin_set(champion)
    ]


def integer_set_sha256(values: list[int]) -> str:
    canonical = json.dumps(sorted(values), separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def runtime_expectation(champion: dict[str, Any]) -> str:
    """Return the default Direct-mode expectation for a benchmark champion."""

    return str(
        champion.get(
            "directExpectation",
            champion["legacyExpectation"],
        )
    )


def _validate_pool_payload(payload: Any, path: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"pool must be a JSON object: {path}")
    if payload.get("schemaVersion") != 1:
        raise ValueError(f"unsupported pool schema in {path}")
    for key in ("poolId", "gameVersion", "description", "osPageCachePolicy"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise ValueError(f"pool has invalid {key}: {path}")
    champions = payload.get("champions")
    if not isinstance(champions, list) or not champions:
        raise ValueError(f"pool has no champions: {path}")
    ids: set[int] = set()
    queries: set[str] = set()
    wad_names: set[str] = set()
    total_skins = 0
    total_paired = 0
    total_base = 0
    for champion in champions:
        champion_id = champion.get("championId")
        if not isinstance(champion_id, int) or champion_id <= 0:
            raise ValueError(f"invalid championId in {path}: {champion_id!r}")
        if champion_id in ids:
            raise ValueError(f"duplicate championId in {path}: {champion_id}")
        ids.add(champion_id)
        for key in ("query", "wadName", "mainUnit", "legacyExpectation"):
            if not isinstance(champion.get(key), str) or not champion[key]:
                raise ValueError(f"invalid {key} for champion {champion_id}")
        query_key = champion["query"].casefold()
        wad_key = champion["wadName"].casefold()
        if query_key in queries:
            raise ValueError(f"duplicate champion query in {path}: {champion['query']}")
        if wad_key in wad_names:
            raise ValueError(f"duplicate champion WAD in {path}: {champion['wadName']}")
        queries.add(query_key)
        wad_names.add(wad_key)
        expectation = champion["legacyExpectation"]
        if expectation not in {"success", "unsupported"}:
            raise ValueError(
                f"invalid legacyExpectation for champion {champion_id}: "
                f"{expectation!r}"
            )
        direct_expectation = runtime_expectation(champion)
        if direct_expectation not in {"success", "unsupported"}:
            raise ValueError(
                f"invalid directExpectation for champion {champion_id}: "
                f"{direct_expectation!r}"
            )
        if expectation == "unsupported":
            for key in ("legacyFailureType", "legacyFailureMessage"):
                if not isinstance(champion.get(key), str) or not champion[key]:
                    raise ValueError(
                        f"unsupported champion {champion_id} requires {key}"
                    )
        elif any(
            key in champion
            for key in ("legacyFailureType", "legacyFailureMessage")
        ):
            raise ValueError(
                f"successful champion {champion_id} cannot declare a legacy failure"
            )
        cases = champion.get("cases")
        if not isinstance(cases, list) or not cases or not all(
            isinstance(item, str) and item
            for item in cases
        ) or len(cases) != len(set(cases)):
            raise ValueError(f"invalid cases for champion {champion_id}")
        skin_numbers = expand_skin_set(champion)
        for key in ("skinCount", "pairedCount", "uniqueBaseCount"):
            if not isinstance(champion.get(key), int) or champion[key] <= 0:
                raise ValueError(f"invalid {key} for champion {champion_id}")
        if champion["skinCount"] != len(skin_numbers):
            raise ValueError(
                f"skinCount mismatch for champion {champion_id}: "
                f"{champion['skinCount']} != {len(skin_numbers)}"
            )
        if champion["pairedCount"] < champion["skinCount"]:
            raise ValueError(
                f"pairedCount cannot be smaller than skinCount for {champion_id}"
            )
        if champion["uniqueBaseCount"] > champion["pairedCount"]:
            raise ValueError(
                f"uniqueBaseCount cannot exceed pairedCount for {champion_id}"
            )
        total_skins += champion["skinCount"]
        total_paired += champion["pairedCount"]
        total_base += champion["uniqueBaseCount"]

    totals = payload.get("totals")
    expected_totals = {
        "champions": len(champions),
        "skins": total_skins,
        "paired": total_paired,
        "uniqueBase": total_base,
        "currentRebaseRitobinProcesses": 3 * total_paired,
        "phase5LogicalConversions": total_base + 2 * total_paired,
    }
    if totals != expected_totals:
        raise ValueError(f"pool totals mismatch: {totals!r} != {expected_totals!r}")
    common = payload.get("commonSuccess")
    if not isinstance(common, dict):
        raise ValueError("pool has no commonSuccess definition")
    excluded_ids = common.get("excludeChampionIds")
    if (
        not isinstance(excluded_ids, list)
        or any(not isinstance(value, int) for value in excluded_ids)
        or excluded_ids != sorted(set(excluded_ids))
        or not set(excluded_ids).issubset(ids)
    ):
        raise ValueError("invalid commonSuccess.excludeChampionIds")
    common_champions = [
        champion
        for champion in champions
        if champion["championId"] not in set(excluded_ids)
    ]
    expected_common = {
        "excludeChampionIds": excluded_ids,
        "champions": len(common_champions),
        "skins": sum(item["skinCount"] for item in common_champions),
        "paired": sum(item["pairedCount"] for item in common_champions),
        "uniqueBase": sum(item["uniqueBaseCount"] for item in common_champions),
    }
    if common != expected_common:
        raise ValueError(
            f"commonSuccess totals mismatch: {common!r} != {expected_common!r}"
        )
    return payload


def load_pool_with_identity(
    path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload, identity = read_json_with_identity(path)
    return _validate_pool_payload(payload, path), identity


def load_pool(path: Path) -> dict[str, Any]:
    payload, _ = load_pool_with_identity(path)
    return payload


def configured_league_root() -> Path:
    config = read_json(REPO_ROOT / "config.json")
    root = Path(config["lol_path"]).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"configured League root does not exist: {root}")
    return root


def windows_file_version(path: Path) -> str | None:
    if os.name != "nt":
        return None
    escaped = str(path).replace("'", "''")
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"(Get-Item -LiteralPath '{escaped}').VersionInfo.FileVersion",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def child_python_identity(python: Path) -> dict[str, Any]:
    requested = python.resolve()
    if not requested.is_file():
        raise FileNotFoundError(f"benchmark child Python does not exist: {requested}")
    code = (
        "import json,platform,sys;"
        "import zstandard;"
        "print(json.dumps({"
        "'executable':sys.executable,"
        "'version':sys.version,"
        "'platform':platform.platform(),"
        "'implementation':platform.python_implementation(),"
        "'cacheTag':sys.implementation.cache_tag,"
        "'zstandardVersion':zstandard.__version__,"
        "'zstandardBackend':zstandard.backend,"
        "'zstandardModule':zstandard.__file__"
        "}))"
    )
    result = subprocess.run(
        [str(requested), "-B", "-c", code],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"benchmark child Python is unusable: {python}\n{result.stderr}"
        )
    payload = json.loads(result.stdout)
    resolved_executable = Path(payload["executable"]).resolve()
    zstandard_module = Path(payload["zstandardModule"]).resolve()
    return {
        "requestedPath": str(requested),
        "requestedSha256": sha256_file(requested),
        "executable": str(resolved_executable),
        "executableSha256": sha256_file(resolved_executable),
        "version": payload["version"],
        "platform": payload["platform"],
        "implementation": payload["implementation"],
        "cacheTag": payload["cacheTag"],
        "zstandardVersion": payload["zstandardVersion"],
        "zstandardBackend": payload["zstandardBackend"],
        "zstandardModule": str(zstandard_module),
        "zstandardModuleSha256": sha256_file(zstandard_module),
    }


def _stat_signature(stat: os.stat_result) -> tuple[int, int, int, int]:
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
    )


def _stable_wad_identity(
    path: Path,
    *,
    include_full_sha256: bool,
) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    path_before = resolved.stat()
    toc_digest = hashlib.sha256()
    full_digest = hashlib.sha256() if include_full_sha256 else None
    total = 0
    with resolved.open("rb") as handle:
        handle_before = os.fstat(handle.fileno())
        if _stat_signature(path_before) != _stat_signature(handle_before):
            raise OSError(f"WAD changed before identity capture: {resolved}")
        header = handle.read(272)
        if len(header) != 272 or header[:2] != b"RW":
            raise ValueError(f"invalid WAD header: {resolved}")
        major = header[2]
        minor = header[3]
        if major != 3 or minor > 4:
            raise ValueError(
                f"unsupported WAD version for benchmark identity: "
                f"{major}.{minor} in {resolved}"
            )
        chunk_count = struct.unpack("<I", header[268:272])[0]
        toc_size = chunk_count * 32
        if 272 + toc_size > handle_before.st_size:
            raise ValueError(f"WAD TOC exceeds file size: {resolved}")
        toc_digest.update(header)
        if full_digest is not None:
            full_digest.update(header)
            total += len(header)
        remaining = toc_size
        while remaining:
            block = handle.read(min(1024 * 1024, remaining))
            if not block:
                raise ValueError(f"truncated WAD TOC: {resolved}")
            toc_digest.update(block)
            if full_digest is not None:
                full_digest.update(block)
                total += len(block)
            remaining -= len(block)
        if full_digest is not None:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                full_digest.update(block)
                total += len(block)
        handle_after = os.fstat(handle.fileno())
    path_after = resolved.stat()
    signatures = {
        _stat_signature(path_before),
        _stat_signature(handle_before),
        _stat_signature(handle_after),
        _stat_signature(path_after),
    }
    if len(signatures) != 1:
        raise OSError(f"WAD changed while capturing identity: {resolved}")
    if full_digest is not None and total != path_after.st_size:
        raise OSError(f"WAD size changed while capturing identity: {resolved}")
    identity = {
        "path": str(resolved),
        "size": path_after.st_size,
        "modifiedNs": path_after.st_mtime_ns,
        "version": f"{major}.{minor}",
        "chunkCount": chunk_count,
        "tocBytes": toc_size,
        "tocSha256": toc_digest.hexdigest(),
    }
    if full_digest is not None:
        identity["sha256"] = full_digest.hexdigest()
    return identity


def wad_toc_identity(path: Path) -> dict[str, Any]:
    return _stable_wad_identity(path, include_full_sha256=False)


def wad_full_identity(path: Path) -> dict[str, Any]:
    return _stable_wad_identity(path, include_full_sha256=True)


def source_file_paths(pool_path: Path) -> list[Path]:
    ignored_parts = {".benchmarks", ".git", ".venv", "__pycache__"}
    python_sources = [
        path.resolve()
        for path in REPO_ROOT.rglob("*.py")
        if not ignored_parts.intersection(path.relative_to(REPO_ROOT).parts)
    ]
    direct_inputs = [
        pool_path.resolve(),
        REPO_ROOT / "config.json",
        REPO_ROOT / "data" / "champion-units.generated.json",
        REPO_ROOT / "pyproject.toml",
        REPO_ROOT / "uv.lock",
    ]
    return sorted(
        {path.resolve() for path in [*python_sources, *direct_inputs]},
        key=lambda path: path.as_posix().casefold(),
    )


def display_source_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def compact_operations(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    totals: dict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)
    operations = metrics.get("operations")
    if not isinstance(operations, list):
        raise ValueError("pipeline metrics operations must be a list")
    for item in operations:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("labels"), dict)
            or not isinstance(item.get("value"), int)
            or item["value"] < 0
        ):
            raise ValueError(f"invalid pipeline operation metric: {item!r}")
        labels = {
            key: str(value)
            for key, value in item["labels"].items()
            if key not in _SCOPE_LABELS
        }
        key = (item["name"], tuple(sorted(labels.items())))
        totals[key] += item["value"]
    return [
        {
            "name": name,
            "labels": dict(labels),
            "value": value,
        }
        for (name, labels), value in sorted(totals.items())
    ]


def operation_total(
    operations: list[dict[str, Any]],
    name: str,
    **required_labels: str,
) -> int:
    total = 0
    for item in operations:
        if item["name"] != name:
            continue
        labels = item.get("labels", {})
        if all(labels.get(key) == value for key, value in required_labels.items()):
            total += int(item["value"])
    return total


def source_identity(
    pool_path: Path,
    league_root: Path,
    pool: dict[str, Any],
    python: Path,
    *,
    pool_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tools = tuple(
        REPO_ROOT / relative_path
        for relative_path in TOOL_RUNTIME_RELATIVE_PATHS
    )
    source_files = source_file_paths(pool_path)
    champions_dir = league_root / "Game" / "DATA" / "FINAL" / "Champions"
    wads: list[dict[str, Any]] = []
    for champion in pool["champions"]:
        path = champions_dir / champion["wadName"]
        if not path.is_file():
            raise FileNotFoundError(f"benchmark WAD not found: {path}")
        wad_identity = wad_full_identity(path)
        wads.append(
            {
                "championId": champion["championId"],
                **wad_identity,
            }
        )
    game_exe = league_root / "Game" / "League of Legends.exe"
    if not game_exe.is_file():
        raise FileNotFoundError(f"League executable not found: {game_exe}")
    actual_version = windows_file_version(game_exe)
    if actual_version is None:
        raise ValueError(f"could not determine installed client version: {game_exe}")
    if actual_version != pool["gameVersion"]:
        raise ValueError(
            f"pool gameVersion {pool['gameVersion']} does not match "
            f"installed client {actual_version}"
        )
    lcu_dir = league_root / "Plugins" / "rcp-be-lol-game-data"
    lcu_wads = sorted(lcu_dir.glob("*.wad"), key=lambda path: path.name.casefold())
    if not lcu_wads:
        raise FileNotFoundError(f"no LCU game-data WADs under {lcu_dir}")
    if pool_identity is None:
        loaded_pool, pool_identity = load_pool_with_identity(pool_path)
        if loaded_pool != pool:
            raise ValueError("parsed pool does not match its captured identity")
    return {
        "git": {
            "head": git_output("rev-parse", "HEAD"),
            "status": git_output("status", "--short", "--untracked-files=all"),
            "diffSha256": hashlib.sha256(
                git_output("diff", "--binary", "HEAD").encode("utf-8")
            ).hexdigest(),
        },
        "sourceFiles": [
            {
                "path": display_source_path(path),
                "sha256": sha256_file(path),
            }
            for path in source_files
        ],
        "poolSource": pool_identity,
        "python": child_python_identity(python),
        "tools": [
            {
                "path": str(path.relative_to(REPO_ROOT)),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in tools
        ],
        "client": {
            "configuredRoot": str(league_root),
            "declaredVersion": pool["gameVersion"],
            "actualVersion": actual_version,
            "executable": {
                "path": str(game_exe),
                "size": game_exe.stat().st_size,
                "modifiedNs": game_exe.stat().st_mtime_ns,
                "sha256": sha256_file(game_exe),
            },
            "wads": wads,
            "lcuWads": [
                wad_toc_identity(path)
                for path in lcu_wads
            ],
        },
    }


_WAD_TOC_IDENTITY_FIELDS = (
    "path",
    "size",
    "modifiedNs",
    "version",
    "chunkCount",
    "tocBytes",
    "tocSha256",
)


def _full_lcu_identity(toc_identity: dict[str, Any]) -> dict[str, Any]:
    full_identity = wad_full_identity(Path(toc_identity["path"]))
    changed_fields = [
        field
        for field in _WAD_TOC_IDENTITY_FIELDS
        if toc_identity.get(field) != full_identity.get(field)
    ]
    if changed_fields:
        raise OSError(
            "LCU WAD changed between TOC and full identity capture "
            f"({', '.join(changed_fields)}): {toc_identity['path']}"
        )
    return full_identity


def current_input_snapshot(identity: dict[str, Any]) -> dict[str, Any]:
    """Return the exact runtime inputs that must remain stable for one series."""

    client = identity.get("client")
    if not isinstance(client, dict):
        raise ValueError("benchmark identity is missing client inputs")
    lcu_wads = client.get("lcuWads")
    if not isinstance(lcu_wads, list):
        raise ValueError("benchmark identity is missing LCU WAD inputs")
    snapshot = {
        "sourceFiles": identity.get("sourceFiles"),
        "poolSource": identity.get("poolSource"),
        "python": identity.get("python"),
        "tools": identity.get("tools"),
        "client": {
            "configuredRoot": client.get("configuredRoot"),
            "declaredVersion": client.get("declaredVersion"),
            "actualVersion": client.get("actualVersion"),
            "executable": client.get("executable"),
            "wads": client.get("wads"),
            "lcuWads": [
                _full_lcu_identity(item)
                for item in lcu_wads
                if isinstance(item, dict)
                and isinstance(item.get("path"), str)
            ],
        },
    }
    if len(snapshot["client"]["lcuWads"]) != len(lcu_wads):
        raise ValueError("benchmark identity contains an invalid LCU WAD entry")
    return json.loads(json.dumps(snapshot))


def summarize_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(metrics, dict):
        raise ValueError("pipeline metrics must be a JSON object")
    if metrics.get("schemaVersion") != PIPELINE_METRICS_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported pipeline metrics schema: "
            f"{metrics.get('schemaVersion')!r}"
        )
    status = metrics.get("status")
    if status not in {"success", "failed"}:
        raise ValueError(f"invalid pipeline metrics status: {status!r}")
    error = metrics.get("error")
    if status == "success" and error is not None:
        raise ValueError("successful pipeline metrics cannot contain an error")
    if status == "failed" and (
        not isinstance(error, dict)
        or not isinstance(error.get("type"), str)
        or not error["type"]
        or not isinstance(error.get("message"), str)
    ):
        raise ValueError("failed pipeline metrics require an exact error payload")
    facts = metrics.get("facts")
    if not isinstance(facts, dict):
        raise ValueError("pipeline metrics facts must be an object")
    selection = facts.get("selection", [])
    if not isinstance(selection, list):
        raise ValueError("pipeline metrics selection must be a list")
    normalized_selection: list[dict[str, int]] = []
    for item in selection:
        if not isinstance(item, dict) or any(
            not isinstance(item.get(key), int)
            for key in ("championId", "skinNumber", "fullSkinId")
        ):
            raise ValueError(f"invalid pipeline selection item: {item!r}")
        normalized_selection.append(
            {
                "championId": item["championId"],
                "skinNumber": item["skinNumber"],
                "fullSkinId": item["fullSkinId"],
            }
        )
    timing = metrics.get("timing")
    if not isinstance(timing, dict) or not isinstance(timing.get("summary"), dict):
        raise ValueError("pipeline metrics timing.summary must be an object")
    operations = compact_operations(metrics)
    return {
        "status": status,
        "error": error,
        "selection": normalized_selection,
        "skinNumbers": [item["skinNumber"] for item in normalized_selection],
        "fullSkinIds": [item["fullSkinId"] for item in normalized_selection],
        "timing": timing["summary"],
        "operations": operations,
    }


def selection_validation_errors(
    champion: dict[str, Any],
    selection: list[dict[str, int]],
) -> list[str]:
    champion_id = champion["championId"]
    expected_numbers = expand_skin_set(champion)
    expected_ids = expected_full_skin_ids(champion)
    actual_numbers = [item["skinNumber"] for item in selection]
    actual_ids = [item["fullSkinId"] for item in selection]
    errors: list[str] = []

    if len(selection) != len({(item["championId"], item["fullSkinId"]) for item in selection}):
        errors.append("selection contains duplicate champion/full-skin identities")
    wrong_champions = sorted(
        {item["championId"] for item in selection}
        - {champion_id}
    )
    if wrong_champions:
        errors.append(
            f"selection contains unexpected champion IDs: {wrong_champions}"
        )
    inconsistent = [
        item
        for item in selection
        if item["fullSkinId"] != item["championId"] * 1000 + item["skinNumber"]
    ]
    if inconsistent:
        errors.append("selection contains inconsistent fullSkinId values")
    if sorted(actual_numbers) != expected_numbers:
        errors.append(
            "skin-number set mismatch: "
            f"actual={sorted(actual_numbers)}, expected={expected_numbers}"
        )
    if sorted(actual_ids) != expected_ids:
        errors.append(
            "full-skin-ID set mismatch: "
            f"actual={sorted(actual_ids)}, expected={expected_ids}"
        )
    return errors


def scenario_command(
    python: Path,
    champion: dict[str, Any],
    scenario: str,
    metrics_path: Path,
) -> list[str]:
    command = [
        str(python),
        "-B",
        str(REPO_ROOT / "script.py"),
        "--champion",
        champion["query"],
        "--format",
        "zip",
        "--hash-update",
        "never",
        "--metrics-json",
        str(metrics_path),
    ]
    return command


def run_scenario(
    *,
    python: Path,
    champion: dict[str, Any],
    scenario: str,
    champion_root: Path,
    raw_root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    input_root, output_root, cache_root = prepare_scenario_directories(
        champion_root,
        scenario,
    )
    metrics_path = raw_root / f"{scenario}.metrics.json"
    log_path = raw_root / f"{scenario}.log"
    raw_root.mkdir(parents=True, exist_ok=True)
    metrics_path.unlink(missing_ok=True)

    env = scenario_environment(input_root, output_root, cache_root)
    command = scenario_command(python, champion, scenario, metrics_path)
    started = time.perf_counter_ns()
    timed_out = False
    with log_path.open("w", encoding="utf-8", newline="\n") as log_handle:
        try:
            completed = subprocess.run(
                command,
                cwd=REPO_ROOT,
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_seconds,
            )
            return_code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            return_code = -1
    wall_ns = time.perf_counter_ns() - started

    metrics_error: str | None = None
    if metrics_path.is_file():
        try:
            summary = summarize_metrics(read_json(metrics_path))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            metrics_error = str(exc)
            summary = {}
    else:
        metrics_error = "pipeline metrics file is missing"
        summary = {}
    if not summary:
        summary = {
        "status": "missing",
        "error": None,
        "selection": [],
        "skinNumbers": [],
        "fullSkinIds": [],
        "timing": {},
        "operations": [],
        }
    actual_skin_count = len(summary["skinNumbers"])
    generated = operation_total(summary["operations"], "skins.generated")
    cache_hits = operation_total(summary["operations"], "cache.archive.hits")
    validation_errors: list[str] = []
    expected_skin_count = int(champion["skinCount"])

    if not timed_out:
        if metrics_error is not None:
            validation_errors.append(f"invalid pipeline metrics: {metrics_error}")
        expected_metrics_status = "success" if return_code == 0 else "failed"
        if summary["status"] != expected_metrics_status:
            validation_errors.append(
                f"pipeline metrics status {summary['status']!r}; "
                f"expected {expected_metrics_status!r}"
            )
    if return_code == 0:
        validation_errors.extend(
            selection_validation_errors(champion, summary["selection"])
        )
    if return_code == 0 and scenario in {
        "app-cold-build",
        "derived-warm-build",
    }:
        if generated != expected_skin_count:
            validation_errors.append(
                f"generated {generated} skins; expected {expected_skin_count}"
            )
        if cache_hits != 0:
            validation_errors.append(
                f"{scenario} reported {cache_hits} archive cache hits"
            )
    if return_code == 0 and scenario == "output-cache-hit":
        if generated != 0:
            validation_errors.append(f"cache-hit run generated {generated} skins")
        if cache_hits != expected_skin_count:
            validation_errors.append(
                f"cache-hit run reported {cache_hits} hits; expected "
                f"{expected_skin_count}"
            )

    error_payload = summary.get("error")
    if timed_out:
        status = "timeout"
    elif return_code == 0 and not validation_errors:
        status = "success"
    elif (
        return_code != 0
        and not validation_errors
        and runtime_expectation(champion) == "unsupported"
        and scenario == "app-cold-build"
        and isinstance(error_payload, dict)
        and error_payload.get("type") == champion["legacyFailureType"]
        and error_payload.get("message") == champion["legacyFailureMessage"]
    ):
        status = "expected_unsupported"
    else:
        status = "failure"

    return {
        "championId": champion["championId"],
        "champion": champion["query"],
        "scenario": scenario,
        "status": status,
        "returnCode": return_code,
        "wallMs": wall_ns / 1_000_000,
        "skinCount": actual_skin_count,
        "expectedSkinCount": expected_skin_count,
        "skinSetSha256": integer_set_sha256(summary["fullSkinIds"]),
        "expectedSkinSetSha256": integer_set_sha256(
            expected_full_skin_ids(champion)
        ),
        "validationErrors": validation_errors,
        "metrics": summary,
        "rawMetrics": str(metrics_path),
        "log": str(log_path),
    }


def aggregate_runs(
    pool: dict[str, Any],
    runs: list[dict[str, Any]],
    *,
    selected_champion_ids: list[int] | None = None,
    scenarios: list[str] | None = None,
) -> dict[str, Any]:
    champions_by_id = {
        champion["championId"]: champion
        for champion in pool["champions"]
    }
    selected_ids = (
        list(champions_by_id)
        if selected_champion_ids is None
        else selected_champion_ids
    )
    requested_scenarios = list(SCENARIOS) if scenarios is None else scenarios
    excluded = set(pool["commonSuccess"]["excludeChampionIds"])
    by_scenario: dict[str, dict[str, Any]] = {}
    for scenario in requested_scenarios:
        scenario_runs = [run for run in runs if run["scenario"] == scenario]
        runs_by_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for run in scenario_runs:
            runs_by_id[run["championId"]].append(run)
        duplicate_ids = sorted(
            champion_id
            for champion_id, items in runs_by_id.items()
            if len(items) != 1
        )
        missing_ids = sorted(set(selected_ids) - set(runs_by_id))
        unexpected_ids = sorted(set(runs_by_id) - set(selected_ids))
        statuses = {
            status: sorted(
                run["championId"]
                for run in scenario_runs
                if run["status"] == status
            )
            for status in (
                "success",
                "expected_unsupported",
                "expected_skipped",
                "skipped",
                "failure",
                "timeout",
            )
        }

        def cohort(champion_ids: list[int]) -> dict[str, Any]:
            non_success_ids = sorted(
                champion_id
                for champion_id in champion_ids
                if len(runs_by_id.get(champion_id, [])) != 1
                or runs_by_id[champion_id][0]["status"] != "success"
            )
            comparable = (
                bool(champion_ids)
                and not duplicate_ids
                and not unexpected_ids
                and not non_success_ids
            )
            return {
                "championIds": champion_ids,
                "expectedChampions": len(champion_ids),
                "expectedSkins": sum(
                    champions_by_id[champion_id]["skinCount"]
                    for champion_id in champion_ids
                ),
                "comparable": comparable,
                "wallMs": (
                    sum(runs_by_id[champion_id][0]["wallMs"] for champion_id in champion_ids)
                    if comparable
                    else None
                ),
                "measuredSkins": (
                    sum(runs_by_id[champion_id][0]["skinCount"] for champion_id in champion_ids)
                    if comparable
                    else None
                ),
                "nonSuccessChampionIds": non_success_ids,
            }

        common_ids = [
            champion_id
            for champion_id in selected_ids
            if champion_id not in excluded
        ]
        by_scenario[scenario] = {
            "attempted": len(scenario_runs),
            "expected": len(selected_ids),
            "complete": (
                not missing_ids
                and not duplicate_ids
                and not unexpected_ids
            ),
            "missingChampionIds": missing_ids,
            "duplicateChampionIds": duplicate_ids,
            "unexpectedChampionIds": unexpected_ids,
            "statusChampionIds": statuses,
            "fixedComparableCohort": cohort(common_ids),
            "allSelectedCohort": cohort(selected_ids),
        }
    return by_scenario


def _nested_value(
    payload: Any,
    path: tuple[str, ...],
    *,
    owner: str,
) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            dotted = ".".join(path)
            raise ValueError(f"{owner} is missing {dotted}")
        current = current[key]
    return current


def _identity_items(
    payload: Any,
    path: tuple[str, ...],
    key_field: str,
    key_type: type,
    *,
    owner: str,
) -> dict[Any, dict[str, Any]]:
    items = _nested_value(payload, path, owner=owner)
    dotted = ".".join(path)
    if not isinstance(items, list):
        raise ValueError(f"{owner} {dotted} must be a list")
    by_key: dict[Any, dict[str, Any]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"{owner} {dotted}[{index}] must be an object")
        key = item.get(key_field)
        if (
            not isinstance(key, key_type)
            or (key_type is int and isinstance(key, bool))
        ):
            raise ValueError(
                f"{owner} {dotted}[{index}].{key_field} has an invalid value"
            )
        if key in by_key:
            raise ValueError(
                f"{owner} {dotted} contains duplicate {key_field} {key!r}"
            )
        by_key[key] = item
    return by_key


def _benchmark_run_map(
    payload: Any,
    *,
    owner: str,
) -> dict[tuple[int, str], dict[str, Any]]:
    runs = _nested_value(payload, ("runs",), owner=owner)
    if not isinstance(runs, list):
        raise ValueError(f"{owner} runs must be a list")
    by_key: dict[tuple[int, str], dict[str, Any]] = {}
    for index, run in enumerate(runs):
        if not isinstance(run, dict):
            raise ValueError(f"{owner} runs[{index}] must be an object")
        champion_id = run.get("championId")
        scenario = run.get("scenario")
        if (
            not isinstance(champion_id, int)
            or isinstance(champion_id, bool)
            or champion_id <= 0
            or not isinstance(scenario, str)
            or not scenario
        ):
            raise ValueError(f"{owner} runs[{index}] has an invalid run key")
        key = (champion_id, scenario)
        if key in by_key:
            raise ValueError(f"{owner} contains duplicate run key {key!r}")
        by_key[key] = run
    return by_key


def _run_key_payload(
    keys: set[tuple[int, str]],
) -> list[dict[str, Any]]:
    return [
        {"championId": champion_id, "scenario": scenario}
        for champion_id, scenario in sorted(keys)
    ]


def _operation_map(
    run: dict[str, Any],
    *,
    owner: str,
) -> dict[tuple[str, tuple[tuple[str, str], ...]], int]:
    if run.get("status") == "expected_skipped" and "metrics" not in run:
        return {}
    metrics = run.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError(f"{owner} run metrics must be an object")
    operations = metrics.get("operations")
    if not isinstance(operations, list):
        raise ValueError(f"{owner} run metrics.operations must be a list")

    normalized: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
    for index, item in enumerate(operations):
        if not isinstance(item, dict):
            raise ValueError(f"{owner} operation {index} must be an object")
        name = item.get("name")
        labels = item.get("labels")
        value = item.get("value")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{owner} operation {index} has an invalid name")
        if not isinstance(labels, dict) or any(
            not isinstance(key, str) or not isinstance(label, str)
            for key, label in labels.items()
        ):
            raise ValueError(f"{owner} operation {index} has invalid labels")
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise ValueError(f"{owner} operation {index} has an invalid value")
        if name in BYTE_VOLUME_OPERATION_NAMES:
            continue
        key = (name, tuple(sorted(labels.items())))
        if key in normalized:
            raise ValueError(
                f"{owner} contains duplicate normalized operation {key!r}"
            )
        normalized[key] = value
    return normalized


def _operation_payload(
    key: tuple[str, tuple[tuple[str, str], ...]],
    **values: int,
) -> dict[str, Any]:
    name, labels = key
    return {
        "name": name,
        "labels": dict(labels),
        **values,
    }


def compare_operation_baseline(
    current: dict[str, Any],
    baseline: Any,
    baseline_identity: dict[str, Any],
) -> dict[str, Any]:
    comparability_mismatches: list[dict[str, Any]] = []

    def mismatch(field: str, message: str, **details: Any) -> None:
        comparability_mismatches.append(
            {
                "field": field,
                "message": message,
                **details,
            }
        )

    for owner, payload in (("baseline", baseline), ("current", current)):
        try:
            schema_version = _nested_value(
                payload,
                ("schemaVersion",),
                owner=owner,
            )
        except ValueError as exc:
            mismatch("schemaVersion", str(exc))
        else:
            if schema_version != RESULT_SCHEMA_VERSION:
                mismatch(
                    "schemaVersion",
                    f"{owner} schemaVersion is {schema_version!r}; "
                    f"expected {RESULT_SCHEMA_VERSION}",
                )

    for field, path in (
        ("pool", ("pool",)),
        ("selectedChampionIds", ("selectedChampionIds",)),
        ("scenarios", ("scenarios",)),
        ("identity.python", ("identity", "python")),
        (
            "identity.client.declaredVersion",
            ("identity", "client", "declaredVersion"),
        ),
        (
            "identity.client.actualVersion",
            ("identity", "client", "actualVersion"),
        ),
        (
            "identity.client.executable",
            ("identity", "client", "executable"),
        ),
    ):
        try:
            baseline_value = _nested_value(baseline, path, owner="baseline")
            current_value = _nested_value(current, path, owner="current")
        except ValueError as exc:
            mismatch(field, str(exc))
            continue
        if baseline_value != current_value:
            mismatch(field, f"{field} does not exactly match the baseline")

    for field, path, key_field, key_type in (
        (
            "identity.client.wads",
            ("identity", "client", "wads"),
            "championId",
            int,
        ),
        (
            "identity.client.lcuWads",
            ("identity", "client", "lcuWads"),
            "path",
            str,
        ),
        ("identity.tools", ("identity", "tools"), "path", str),
    ):
        try:
            baseline_items = _identity_items(
                baseline,
                path,
                key_field,
                key_type,
                owner="baseline",
            )
            current_items = _identity_items(
                current,
                path,
                key_field,
                key_type,
                owner="current",
            )
        except ValueError as exc:
            mismatch(field, str(exc))
            continue
        if baseline_items != current_items:
            mismatch(field, f"{field} does not exactly match the baseline")

    baseline_runs: dict[tuple[int, str], dict[str, Any]] | None = None
    current_runs: dict[tuple[int, str], dict[str, Any]] | None = None
    try:
        baseline_runs = _benchmark_run_map(baseline, owner="baseline")
    except ValueError as exc:
        mismatch("runs.keys", str(exc))
    try:
        current_runs = _benchmark_run_map(current, owner="current")
    except ValueError as exc:
        mismatch("runs.keys", str(exc))

    expected_run_keys: set[tuple[int, str]] | None = None
    selected = current.get("selectedChampionIds")
    scenarios = current.get("scenarios")
    if (
        isinstance(selected, list)
        and all(
            isinstance(champion_id, int)
            and not isinstance(champion_id, bool)
            and champion_id > 0
            for champion_id in selected
        )
        and len(selected) == len(set(selected))
        and isinstance(scenarios, list)
        and all(isinstance(scenario, str) and scenario for scenario in scenarios)
        and len(scenarios) == len(set(scenarios))
    ):
        expected_run_keys = {
            (champion_id, scenario)
            for champion_id in selected
            for scenario in scenarios
        }
    else:
        mismatch(
            "runs.keys",
            "current selectedChampionIds/scenarios cannot define exact run keys",
        )

    if expected_run_keys is not None:
        for owner, run_map in (
            ("baseline", baseline_runs),
            ("current", current_runs),
        ):
            if run_map is None:
                continue
            run_keys = set(run_map)
            missing = expected_run_keys - run_keys
            unexpected = run_keys - expected_run_keys
            if missing or unexpected:
                mismatch(
                    "runs.keys",
                    f"{owner} run keys do not match the selected "
                    "champion/scenario product",
                    owner=owner,
                    missing=_run_key_payload(missing),
                    unexpected=_run_key_payload(unexpected),
                )
    if baseline_runs is not None and current_runs is not None:
        baseline_keys = set(baseline_runs)
        current_keys = set(current_runs)
        if baseline_keys != current_keys:
            mismatch(
                "runs.keys",
                "current run keys do not exactly match the baseline",
                missing=_run_key_payload(baseline_keys - current_keys),
                unexpected=_run_key_payload(current_keys - baseline_keys),
            )

    gate: dict[str, Any] = {
        "status": "failed",
        "baseline": baseline_identity,
        "excludedOperationNames": sorted(BYTE_VOLUME_OPERATION_NAMES),
        "comparability": {
            "status": (
                "failed"
                if comparability_mismatches
                else "passed"
            ),
            "mismatches": comparability_mismatches,
        },
        "runComparisons": [],
        "summary": {
            "expectedRuns": (
                len(expected_run_keys)
                if expected_run_keys is not None
                else None
            ),
            "comparedRuns": 0,
            "failedRuns": 0,
        },
    }
    if (
        comparability_mismatches
        or baseline_runs is None
        or current_runs is None
    ):
        return gate

    comparisons: list[dict[str, Any]] = []
    for key in sorted(baseline_runs):
        baseline_run = baseline_runs[key]
        current_run = current_runs[key]
        baseline_status = baseline_run.get("status")
        current_status = current_run.get("status")
        validation_errors: list[str] = []
        try:
            baseline_operations = _operation_map(
                baseline_run,
                owner="baseline",
            )
        except ValueError as exc:
            validation_errors.append(str(exc))
            baseline_operations = {}
        try:
            current_operations = _operation_map(
                current_run,
                owner="current",
            )
        except ValueError as exc:
            validation_errors.append(str(exc))
            current_operations = {}

        baseline_keys = set(baseline_operations)
        current_keys = set(current_operations)
        missing_keys = baseline_keys - current_keys
        unexpected_keys = current_keys - baseline_keys
        changed_keys = {
            operation_key
            for operation_key in baseline_keys & current_keys
            if baseline_operations[operation_key]
            != current_operations[operation_key]
        }
        comparison_status = (
            "passed"
            if (
                baseline_status == current_status
                and not validation_errors
                and not missing_keys
                and not unexpected_keys
                and not changed_keys
            )
            else "failed"
        )
        comparisons.append(
            {
                "championId": key[0],
                "scenario": key[1],
                "status": comparison_status,
                "baselineRunStatus": baseline_status,
                "currentRunStatus": current_status,
                "missingOperations": [
                    _operation_payload(
                        operation_key,
                        baselineValue=baseline_operations[operation_key],
                    )
                    for operation_key in sorted(missing_keys)
                ],
                "unexpectedOperations": [
                    _operation_payload(
                        operation_key,
                        currentValue=current_operations[operation_key],
                    )
                    for operation_key in sorted(unexpected_keys)
                ],
                "changedOperations": [
                    _operation_payload(
                        operation_key,
                        baselineValue=baseline_operations[operation_key],
                        currentValue=current_operations[operation_key],
                    )
                    for operation_key in sorted(changed_keys)
                ],
                "validationErrors": validation_errors,
            }
        )

    failed_runs = sum(
        comparison["status"] == "failed"
        for comparison in comparisons
    )
    gate["runComparisons"] = comparisons
    gate["summary"]["comparedRuns"] = len(comparisons)
    gate["summary"]["failedRuns"] = failed_runs
    gate["status"] = "failed" if failed_runs else "passed"
    return gate


def compare_operation_baseline_file(
    current: dict[str, Any],
    baseline_path: Path,
    baseline: Any,
    baseline_identity: dict[str, Any],
) -> dict[str, Any]:
    """Revalidate the on-disk baseline before publishing its comparison Gate."""

    try:
        ending_baseline, ending_identity = read_json_with_identity(
            baseline_path
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return {
            "status": "failed",
            "baseline": baseline_identity,
            "excludedOperationNames": sorted(BYTE_VOLUME_OPERATION_NAMES),
            "sourceStability": {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            },
            "comparability": {
                "status": "not_evaluated",
                "mismatches": [],
            },
            "runComparisons": [],
        }
    if ending_identity != baseline_identity or ending_baseline != baseline:
        return {
            "status": "failed",
            "baseline": baseline_identity,
            "excludedOperationNames": sorted(BYTE_VOLUME_OPERATION_NAMES),
            "sourceStability": {
                "status": "failed",
                "endingIdentity": ending_identity,
                "error": "operation baseline changed during benchmark run",
            },
            "comparability": {
                "status": "not_evaluated",
                "mismatches": [],
            },
            "runComparisons": [],
        }
    gate = compare_operation_baseline(
        current,
        baseline,
        baseline_identity,
    )
    gate["sourceStability"] = {
        "status": "passed",
        "endingIdentity": ending_identity,
    }
    return gate


def operation_baseline_blocked_by_current_inputs(
    baseline_identity: dict[str, Any],
    current_input_gate: Any,
) -> dict[str, Any]:
    return {
        "status": "failed",
        "baseline": baseline_identity,
        "excludedOperationNames": sorted(BYTE_VOLUME_OPERATION_NAMES),
        "sourceStability": {
            "status": "not_evaluated",
        },
        "comparability": {
            "status": "not_evaluated",
            "mismatches": [],
        },
        "runComparisons": [],
        "reason": (
            "operation baseline was not evaluated because "
            "currentInputStability did not pass"
        ),
        "currentInputStabilityStatus": (
            current_input_gate.get("status")
            if isinstance(current_input_gate, dict)
            else None
        ),
    }


def benchmark_result_failed(result: dict[str, Any]) -> bool:
    if any(
        run.get("status") in {"failure", "timeout", "skipped"}
        for run in result.get("runs", [])
    ):
        return True
    current_input_gate = result.get("currentInputStability")
    if (
        not isinstance(current_input_gate, dict)
        or current_input_gate.get("status") != "passed"
    ):
        return True
    gate = result.get("operationBaselineGate")
    if gate is None:
        return False
    if not isinstance(gate, dict):
        return True
    return gate.get("status") not in {"not_requested", "passed"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    parser.add_argument(
        "--scenarios",
        default="app-cold-build,output-cache-hit",
        help=(
            "canonical prefix: app-cold-build or "
            "app-cold-build,output-cache-hit"
        ),
    )
    parser.add_argument(
        "--include-derived-warm",
        action="store_true",
        help=(
            "append the derived-warm-build scenario after the "
            "standard cold/cache-hit pair"
        ),
    )
    parser.add_argument("--champion", action="append", default=[])
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    parser.add_argument("--result", type=Path)
    parser.add_argument(
        "--operation-baseline",
        type=Path,
        help=(
            "require exact non-byte operation structure from a comparable "
            "benchmark result"
        ),
    )
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")
    phase = validate_phase(args.phase)
    pool_path = args.pool.resolve()
    pool, pool_identity = load_pool_with_identity(pool_path)
    requested_scenarios = resolve_scenarios(
        args.scenarios,
        include_derived_warm=args.include_derived_warm,
    )

    champions = pool["champions"]
    if args.champion:
        wanted = {item.casefold() for item in args.champion}
        champions = [
            champion
            for champion in champions
            if champion["query"].casefold() in wanted
        ]
        missing = wanted - {
            champion["query"].casefold()
            for champion in champions
        }
        if missing:
            raise ValueError(f"champions are not in the fixed pool: {sorted(missing)}")

    requested_work_root = args.work_root.resolve()
    work_root = ensure_scratch_root(
        requested_work_root,
        allow_initialize=requested_work_root == DEFAULT_WORK_ROOT.resolve(),
    )
    series_root = work_root / "work" / phase
    raw_root = work_root / "raw" / phase
    result_path = (
        args.result.resolve()
        if args.result is not None
        else work_root / "results" / f"{phase}.json"
    )
    for reset_root in (work_root / "work", work_root / "raw"):
        if result_path == reset_root or result_path.is_relative_to(reset_root):
            raise ValueError(
                f"benchmark result cannot be stored under reset root: {result_path}"
            )
    baseline_result: Any = None
    baseline_identity: dict[str, Any] | None = None
    baseline_path: Path | None = None
    if args.operation_baseline is not None:
        baseline_path = args.operation_baseline.resolve()
        if baseline_path == result_path:
            raise ValueError(
                "--operation-baseline and --result must be different files"
            )
        baseline_result, baseline_identity = read_json_with_identity(baseline_path)
    league_root = configured_league_root()
    child_python = args.python.resolve()
    selected_champion_ids = [
        champion["championId"]
        for champion in champions
    ]
    identity = source_identity(
        pool_path,
        league_root,
        pool,
        child_python,
        pool_identity=pool_identity,
    )
    starting_input_snapshot = current_input_snapshot(identity)
    result: dict[str, Any] = {
        "schemaVersion": RESULT_SCHEMA_VERSION,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "pool": pool,
        "scenarios": requested_scenarios,
        "selectedChampionIds": selected_champion_ids,
        "timeoutSeconds": args.timeout_seconds,
        "environment": {
            "runnerPython": sys.version,
            "platform": platform.platform(),
            "processor": platform.processor(),
            "osPageCachePolicy": pool["osPageCachePolicy"],
        },
        "identity": identity,
        "currentInputStability": {
            "status": "pending",
            "starting": starting_input_snapshot,
        },
        "runs": [],
        "aggregate": {},
        "operationBaselineGate": (
            {
                "status": "pending",
                "baseline": baseline_identity,
                "excludedOperationNames": sorted(
                    BYTE_VOLUME_OPERATION_NAMES
                ),
            }
            if args.operation_baseline is not None
            else {"status": "not_requested"}
        ),
    }
    write_json_atomically(result_path, result)

    for champion in champions:
        champion_root = series_root / str(champion["championId"])
        safe_reset_directory(champion_root, work_root)
        champion_raw = raw_root / str(champion["championId"])
        safe_reset_directory(champion_raw, work_root)
        cold_status: str | None = None
        for scenario in requested_scenarios:
            if scenario != "app-cold-build" and cold_status != "success":
                expected_skip = cold_status == "expected_unsupported"
                skipped = {
                    "championId": champion["championId"],
                    "champion": champion["query"],
                    "scenario": scenario,
                    "status": (
                        "expected_skipped"
                        if expected_skip
                        else "skipped"
                    ),
                    "reason": "app-cold-build did not succeed",
                }
                result["runs"].append(skipped)
                result["aggregate"] = aggregate_runs(
                    pool,
                    result["runs"],
                    selected_champion_ids=selected_champion_ids,
                    scenarios=requested_scenarios,
                )
                write_json_atomically(result_path, result)
                continue
            run = run_scenario(
                python=child_python,
                champion=champion,
                scenario=scenario,
                champion_root=champion_root,
                raw_root=champion_raw,
                timeout_seconds=args.timeout_seconds,
            )
            result["runs"].append(run)
            if scenario == "app-cold-build":
                cold_status = run["status"]
            result["aggregate"] = aggregate_runs(
                pool,
                result["runs"],
                selected_champion_ids=selected_champion_ids,
                scenarios=requested_scenarios,
            )
            write_json_atomically(result_path, result)
            print(
                f"{champion['query']:<14} {scenario:<22} "
                f"{run['status']:<20} {run['wallMs'] / 1000:>9.2f}s",
                flush=True,
            )

    result["aggregate"] = aggregate_runs(
        pool,
        result["runs"],
        selected_champion_ids=selected_champion_ids,
        scenarios=requested_scenarios,
    )
    try:
        ending_pool, ending_pool_identity = load_pool_with_identity(pool_path)
        ending_identity = source_identity(
            pool_path,
            league_root,
            ending_pool,
            child_python,
            pool_identity=ending_pool_identity,
        )
        ending_input_snapshot = current_input_snapshot(ending_identity)
    except (OSError, ValueError, RuntimeError) as exc:
        result["currentInputStability"] = {
            "status": "failed",
            "starting": starting_input_snapshot,
            "error": f"{type(exc).__name__}: {exc}",
        }
    else:
        changed_sections = [
            field
            for field in starting_input_snapshot
            if starting_input_snapshot.get(field)
            != ending_input_snapshot.get(field)
        ]
        result["currentInputStability"] = {
            "status": "failed" if changed_sections else "passed",
            "starting": starting_input_snapshot,
            "ending": ending_input_snapshot,
            "changedSections": changed_sections,
        }
    if args.operation_baseline is not None:
        assert baseline_identity is not None
        assert baseline_path is not None
        if result["currentInputStability"].get("status") == "passed":
            result["operationBaselineGate"] = compare_operation_baseline_file(
                result,
                baseline_path,
                baseline_result,
                baseline_identity,
            )
        else:
            result["operationBaselineGate"] = (
                operation_baseline_blocked_by_current_inputs(
                    baseline_identity,
                    result["currentInputStability"],
                )
            )
    write_json_atomically(result_path, result)
    return 1 if benchmark_result_failed(result) else 0


if __name__ == "__main__":
    raise SystemExit(main())
