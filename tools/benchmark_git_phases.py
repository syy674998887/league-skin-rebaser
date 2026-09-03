"""Run normalized old/new Phase benchmarks in an isolated Git worktree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POOL = REPO_ROOT / "benchmarks" / "pools" / "hash-upgrade-fast5.json"
DEFAULT_FIXTURE = (
    REPO_ROOT / "benchmarks" / "fixtures" / "hash-upgrade-units.json"
)
DEFAULT_RUNNER = REPO_ROOT / ".benchmarks" / "git-phase-runner"
DEFAULT_DATA_ROOT = REPO_ROOT / ".benchmarks" / "git-phase-data"
DEFAULT_RESULT_ROOT = REPO_ROOT / ".benchmarks" / "git-phase-results"
DEFAULT_SUMMARY = (
    REPO_ROOT / ".cache" / "benchmarks" / "git-phase-comparison.json"
)
SCRATCH_SENTINEL = ".league-skin-rebaser-benchmark-v1"


@dataclass(frozen=True)
class PhaseNode:
    cohort: str
    phase: int
    commit: str

    @property
    def slug(self) -> str:
        return f"hash-{self.cohort}-phase-{self.phase}"


OLD_COMMITS = {
    1: "5a11a9a",
    2: "76d4f06",
    3: "d5f5786",
    4: "25f11f2",
    5: "504216a",
    6: "5a4b966",
    7: "c466a5e",
}

NEW_COMMITS = {
    1: "543bbe9",
    2: "3c78b2f",
    3: "f9f2e9e",
    4: "52e3fcc",
    5: "2d7050e",
    6: "5854f7c",
    7: "800a513",
}

RUNTIME_DIRECTORIES = ("bin", "cslol-tools")
RUNTIME_FILES = ("config.json",)
TIMING_NAMES = (
    "pipeline.noninteractive",
    "run.wall",
    "catalog.total",
    "catalog.direct",
    "prepare.total",
    "prepare.session",
    "conversion.batch.total",
    "skin.total",
)
CACHE_OPERATION_NAMES = (
    "cache.archive.hits",
    "cache.archive.misses",
    "cache.catalog.persistent_hits",
    "cache.catalog.persistent_misses",
    "cache.layout.persistent_hits",
    "cache.layout.persistent_misses",
    "cache.base_parse.persistent_hits",
    "cache.base_parse.persistent_misses",
)


class GitPhaseBenchmarkError(RuntimeError):
    """A normalized Git Phase benchmark could not be established."""


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_sha256(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def run_git(
    *args: str,
    cwd: Path = REPO_ROOT,
    text: bool = True,
) -> str | bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=text,
    )
    return result.stdout.strip() if text else result.stdout


def resolve_commit(commit: str) -> str:
    value = run_git("rev-parse", f"{commit}^{{commit}}")
    assert isinstance(value, str)
    if len(value) != 40:
        raise GitPhaseBenchmarkError(f"invalid resolved commit: {value!r}")
    return value


def selected_nodes(
    cohorts: Iterable[str],
    phases: Iterable[int],
) -> list[PhaseNode]:
    selected_cohorts = tuple(cohorts)
    nodes: list[PhaseNode] = []
    for phase in sorted(set(phases)):
        if "old" in selected_cohorts:
            nodes.append(PhaseNode("old", phase, OLD_COMMITS[phase]))
        if "new" in selected_cohorts:
            nodes.append(PhaseNode("new", phase, NEW_COMMITS[phase]))
    return nodes


def validate_inputs(
    pool_path: Path,
    fixture_path: Path,
    nodes: Iterable[PhaseNode],
    python: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    pool = read_json(pool_path)
    fixture = read_json(fixture_path)
    if pool.get("schemaVersion") != 1:
        raise GitPhaseBenchmarkError("benchmark pool schemaVersion must be 1")
    if fixture.get("schemaVersion") != 1:
        raise GitPhaseBenchmarkError("unit fixture schemaVersion must be 1")
    if fixture.get("poolId") != pool.get("poolId"):
        raise GitPhaseBenchmarkError("unit fixture belongs to another pool")
    fixture_champions = fixture.get("champions")
    if not isinstance(fixture_champions, dict):
        raise GitPhaseBenchmarkError("unit fixture champions must be an object")
    pool_ids = {
        str(champion["championId"])
        for champion in pool.get("champions", [])
    }
    if set(fixture_champions) != pool_ids:
        raise GitPhaseBenchmarkError(
            "unit fixture champion ids do not match the benchmark pool"
        )
    if not python.is_file():
        raise GitPhaseBenchmarkError(f"benchmark Python is missing: {python}")
    for relative in (*RUNTIME_DIRECTORIES, *RUNTIME_FILES):
        if not (REPO_ROOT / relative).exists():
            raise GitPhaseBenchmarkError(
                f"shared runtime input is missing: {REPO_ROOT / relative}"
            )
    for node in nodes:
        resolve_commit(node.commit)
    return pool, fixture


def tracked_status(runner: Path) -> tuple[str, ...]:
    output = run_git(
        "status",
        "--short",
        "--untracked-files=no",
        cwd=runner,
    )
    assert isinstance(output, str)
    return tuple(line for line in output.splitlines() if line)


def recover_fixture_change(runner: Path) -> None:
    status = tracked_status(runner)
    allowed = {" M champion-units.generated.json"}
    if status and set(status) == allowed:
        original = run_git(
            "show",
            "HEAD:champion-units.generated.json",
            cwd=runner,
            text=False,
        )
        assert isinstance(original, bytes)
        (runner / "champion-units.generated.json").write_bytes(original)
        status = tracked_status(runner)
    if status:
        raise GitPhaseBenchmarkError(
            f"benchmark worktree has unexpected tracked changes: {status}"
        )


def hardlink_tree(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        copy_function=os.link,
    )


def ensure_runner(runner: Path, first_commit: str) -> None:
    if runner.exists():
        try:
            common = run_git(
                "rev-parse",
                "--git-common-dir",
                cwd=runner,
            )
        except subprocess.CalledProcessError as exc:
            raise GitPhaseBenchmarkError(
                f"runner exists but is not a Git worktree: {runner}"
            ) from exc
        expected = (REPO_ROOT / ".git").resolve()
        actual = (runner / str(common)).resolve()
        if actual != expected:
            raise GitPhaseBenchmarkError(
                f"runner belongs to another repository: {actual}"
            )
        recover_fixture_change(runner)
    else:
        runner.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "--detach",
                str(runner),
                first_commit,
            ],
            cwd=REPO_ROOT,
            check=True,
        )

    for relative in RUNTIME_DIRECTORIES:
        destination = runner / relative
        if not destination.exists():
            hardlink_tree(REPO_ROOT / relative, destination)
    for relative in RUNTIME_FILES:
        destination = runner / relative
        if not destination.exists():
            shutil.copy2(REPO_ROOT / relative, destination)


def safe_remove_cache(runner: Path) -> None:
    cache = runner / ".cache"
    if not cache.exists():
        return
    if cache.is_symlink() or cache.resolve().parent != runner.resolve():
        raise GitPhaseBenchmarkError(
            f"refusing to remove unsafe runner cache: {cache}"
        )
    shutil.rmtree(cache)


def switch_runner(runner: Path, commit: str) -> str:
    recover_fixture_change(runner)
    subprocess.run(
        ["git", "switch", "--detach", "--quiet", commit],
        cwd=runner,
        check=True,
    )
    safe_remove_cache(runner)
    resolved = run_git("rev-parse", "HEAD", cwd=runner)
    assert isinstance(resolved, str)
    return resolved


def apply_unit_fixture(
    runner: Path,
    fixture: dict[str, Any],
) -> bytes | None:
    registry_path = runner / "champion-units.generated.json"
    if not registry_path.is_file():
        return None
    original = registry_path.read_bytes()
    registry = json.loads(original.decode("utf-8"))
    champions = registry.get("champions")
    fixture_champions = fixture.get("champions")
    if not isinstance(champions, dict) or not isinstance(
        fixture_champions,
        dict,
    ):
        raise GitPhaseBenchmarkError("invalid champion-unit registry fixture")
    for champion_id, expected in fixture_champions.items():
        current = champions.get(champion_id)
        if not isinstance(current, dict) or not isinstance(expected, dict):
            raise GitPhaseBenchmarkError(
                f"missing champion-unit entry {champion_id}"
            )
        for field in ("alias", "wadBase", "mainUnit"):
            if current.get(field) != expected.get(field):
                raise GitPhaseBenchmarkError(
                    f"champion-unit identity mismatch for {champion_id} "
                    f"field {field}"
                )
        auxiliary = expected.get("auxiliaryUnits")
        if (
            not isinstance(auxiliary, list)
            or not all(isinstance(unit, str) and unit for unit in auxiliary)
            or auxiliary != sorted(set(auxiliary))
        ):
            raise GitPhaseBenchmarkError(
                f"invalid auxiliary-unit fixture for {champion_id}"
            )
        current["auxiliaryUnits"] = auxiliary
    registry_path.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return original


def restore_unit_fixture(runner: Path, original: bytes | None) -> None:
    if original is None:
        return
    (runner / "champion-units.generated.json").write_bytes(original)
    if tracked_status(runner):
        raise GitPhaseBenchmarkError(
            "champion-unit fixture did not restore cleanly"
        )


def ensure_work_root(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    sentinel = path / SCRATCH_SENTINEL
    if not sentinel.exists():
        sentinel.write_text(
            "league-skin-rebaser benchmark scratch directory\n",
            encoding="utf-8",
            newline="\n",
        )
    if not sentinel.is_file():
        raise GitPhaseBenchmarkError(
            f"benchmark sentinel is invalid: {sentinel}"
        )


def inspect_materialized_workload(
    work_root: Path,
    phase_slug: str,
    pool: dict[str, Any],
) -> dict[str, Any]:
    skin_count = 0
    paired_count = 0
    units: set[tuple[int, str]] = set()
    missing_roots: list[str] = []
    for champion in pool["champions"]:
        champion_id = champion["championId"]
        input_root = (
            work_root
            / "work"
            / phase_slug
            / str(champion_id)
            / "input"
        )
        if not input_root.is_dir():
            missing_roots.append(str(input_root))
            continue
        for skin_dir in input_root.iterdir():
            if not skin_dir.is_dir():
                continue
            unit_count = 0
            for unit_dir in skin_dir.iterdir():
                if not unit_dir.is_dir() or unit_dir.name.startswith("step"):
                    continue
                base = unit_dir / "skin0.bin"
                targets = tuple(
                    path
                    for path in unit_dir.glob("skin*.bin")
                    if path.name.casefold() != "skin0.bin"
                )
                if base.is_file() and len(targets) == 1:
                    unit_count += 1
                    units.add((champion_id, unit_dir.name.casefold()))
            if unit_count:
                skin_count += 1
                paired_count += unit_count
    return {
        "skinCount": skin_count,
        "pairedCount": paired_count,
        "uniqueChampionUnits": len(units),
        "missingInputRoots": missing_roots,
        "valid": (
            not missing_roots
            and skin_count == pool["totals"]["skins"]
            and paired_count == pool["totals"]["paired"]
            and len(units) == pool["totals"]["uniqueBase"]
        ),
    }


def result_is_complete(
    result_path: Path,
    workload_path: Path,
    resolved_commit: str,
    expected_phase: str,
    expected_scenarios: list[str],
    expected_pool_sha256: str,
    allow_mixed_input_series: bool,
) -> bool:
    if not result_path.is_file() or not workload_path.is_file():
        return False
    try:
        result = read_json(result_path)
        workload = read_json(workload_path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    head = (
        result.get("identity", {})
        .get("git", {})
        .get("head")
    )
    result_pool_sha256 = (
        result.get("identity", {})
        .get("poolSource", {})
        .get("sha256")
    )
    return bool(
        head == resolved_commit
        and result.get("phase") == expected_phase
        and result.get("scenarios") == expected_scenarios
        and (
            result_pool_sha256 == expected_pool_sha256
            or allow_mixed_input_series
        )
        and result.get("currentInputStability", {}).get("status") == "passed"
        and result.get("runs")
        and all(run.get("status") == "success" for run in result["runs"])
        and workload.get("valid") is True
    )


def selected_scenarios(
    scenarios: str,
    include_derived_warm: bool,
) -> list[str]:
    selected = scenarios.split(",")
    if include_derived_warm:
        selected.append("derived-warm-build")
    return selected


def run_node(
    node: PhaseNode,
    *,
    runner: Path,
    python: Path,
    pool_path: Path,
    pool: dict[str, Any],
    fixture: dict[str, Any],
    data_root: Path,
    result_root: Path,
    scenarios: str,
    timeout_seconds: int,
    include_derived_warm: bool,
    resume: bool,
    allow_mixed_input_series: bool,
) -> dict[str, Any]:
    resolved = switch_runner(runner, node.commit)
    work_root = data_root / node.slug
    ensure_work_root(work_root)
    result_path = result_root / f"{node.slug}.json"
    workload_path = result_root / f"{node.slug}.workload.json"
    expected_scenarios = selected_scenarios(
        scenarios,
        include_derived_warm,
    )
    expected_pool_sha256 = sha256_file(pool_path)
    if resume and result_is_complete(
        result_path,
        workload_path,
        resolved,
        node.slug,
        expected_scenarios,
        expected_pool_sha256,
        allow_mixed_input_series,
    ):
        print(f"{node.slug}: resume hit")
        return {
            "node": node,
            "resolved": resolved,
            "resultPath": result_path,
            "workloadPath": workload_path,
            "returnCode": 0,
            "resumed": True,
        }

    original = apply_unit_fixture(runner, fixture)
    try:
        command = [
            str(python),
            "-B",
            str(runner / "tools" / "benchmark_pipeline.py"),
            "--phase",
            node.slug,
            "--pool",
            str(pool_path),
            "--scenarios",
            scenarios,
            "--python",
            str(python),
            "--work-root",
            str(work_root),
            "--result",
            str(result_path),
            "--timeout-seconds",
            str(timeout_seconds),
        ]
        if include_derived_warm:
            command.append("--include-derived-warm")
        print(
            f"{node.slug}: {resolved[:7]} "
            f"fixture={'yes' if original is not None else 'dictionary'}"
        )
        completed = subprocess.run(
            command,
            cwd=runner,
            check=False,
        )
        workload = inspect_materialized_workload(
            work_root,
            node.slug,
            pool,
        )
        workload.update(
            {
                "schemaVersion": 1,
                "cohort": node.cohort,
                "phase": node.phase,
                "requestedCommit": node.commit,
                "resolvedCommit": resolved,
            }
        )
        write_json(workload_path, workload)
    finally:
        restore_unit_fixture(runner, original)

    return {
        "node": node,
        "resolved": resolved,
        "resultPath": result_path,
        "workloadPath": workload_path,
        "returnCode": completed.returncode,
        "resumed": False,
    }


def operation_totals(
    runs: list[dict[str, Any]],
    scenario: str,
) -> tuple[dict[str, int], dict[str, int]]:
    process_attempts: dict[str, int] = defaultdict(int)
    cache_operations: dict[str, int] = defaultdict(int)
    for run in runs:
        if run.get("scenario") != scenario:
            continue
        operations = run.get("metrics", {}).get("operations", [])
        for operation in operations:
            name = operation.get("name")
            value = operation.get("value")
            labels = operation.get("labels", {})
            if not isinstance(value, int):
                continue
            if name == "process.attempts":
                process_attempts[str(labels.get("tool", "unknown"))] += value
            if name in CACHE_OPERATION_NAMES:
                cache_operations[str(name)] += value
    return dict(sorted(process_attempts.items())), dict(
        sorted(cache_operations.items())
    )


def timing_totals(
    runs: list[dict[str, Any]],
    scenario: str,
) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for run in runs:
        if run.get("scenario") != scenario:
            continue
        timing = run.get("metrics", {}).get("timing", {})
        if not isinstance(timing, dict):
            continue
        for name in TIMING_NAMES:
            item = timing.get(name)
            if isinstance(item, dict) and isinstance(
                item.get("total_ms"),
                (int, float),
            ):
                totals[name] += float(item["total_ms"])
    return {
        name: round(value, 4)
        for name, value in sorted(totals.items())
    }


def scenario_wall(result: dict[str, Any], scenario: str) -> float | None:
    aggregate = result.get("aggregate", {}).get(scenario, {})
    cohort = aggregate.get("fixedComparableCohort")
    if not isinstance(cohort, dict):
        cohort = aggregate.get("allSelectedCohort")
    value = cohort.get("wallMs") if isinstance(cohort, dict) else None
    return float(value) if isinstance(value, (int, float)) else None


def summarize_node(record: dict[str, Any]) -> dict[str, Any]:
    node: PhaseNode = record["node"]
    result_path: Path = record["resultPath"]
    workload_path: Path = record["workloadPath"]
    result = read_json(result_path)
    workload = read_json(workload_path)
    identity = result.get("identity", {})
    client_identity = identity.get("client")
    runs = result.get("runs", [])
    scenarios = result.get("scenarios", [])
    scenario_summary: dict[str, Any] = {}
    for scenario in scenarios:
        attempts, cache = operation_totals(runs, scenario)
        scenario_summary[scenario] = {
            "wallMs": scenario_wall(result, scenario),
            "processAttempts": attempts,
            "cacheOperations": cache,
            "timingMs": timing_totals(runs, scenario),
        }
    return {
        "cohort": node.cohort,
        "phase": node.phase,
        "requestedCommit": node.commit,
        "resolvedCommit": record["resolved"],
        "resultSha256": sha256_file(result_path),
        "returnCode": record["returnCode"],
        "resumed": record["resumed"],
        "inputStability": result.get("currentInputStability", {}).get("status"),
        "gitStatus": identity.get("git", {}).get("status"),
        "poolSource": identity.get("poolSource"),
        "clientActualVersion": (
            client_identity.get("actualVersion")
            if isinstance(client_identity, dict)
            else None
        ),
        "clientInputSha256": (
            canonical_json_sha256(client_identity)
            if isinstance(client_identity, dict)
            else None
        ),
        "workload": workload,
        "scenarios": scenario_summary,
    }


def comparison_row(
    phase: int,
    old: dict[str, Any],
    new: dict[str, Any],
    scenario: str,
) -> dict[str, Any]:
    same_input = (
        old.get("clientInputSha256")
        and old.get("clientInputSha256") == new.get("clientInputSha256")
    )
    old_wall = old["scenarios"].get(scenario, {}).get("wallMs")
    new_wall = new["scenarios"].get(scenario, {}).get("wallMs")
    delta = (
        None
        if not same_input or old_wall is None or new_wall is None
        else new_wall - old_wall
    )
    percent = (
        None
        if delta is None or not old_wall
        else 100.0 * delta / old_wall
    )
    return {
        "phase": phase,
        "scenario": scenario,
        "sameClientInput": bool(same_input),
        "clientInputSha256": (
            old.get("clientInputSha256") if same_input else None
        ),
        "oldWallMs": old_wall,
        "newWallMs": new_wall,
        "deltaMs": None if delta is None else round(delta, 4),
        "deltaPercent": None if percent is None else round(percent, 4),
    }


def adjacent_comparison_row(
    cohort: str,
    from_phase: int,
    before: dict[str, Any],
    after: dict[str, Any],
    scenario: str,
) -> dict[str, Any]:
    same_input = (
        before.get("clientInputSha256")
        and before.get("clientInputSha256")
        == after.get("clientInputSha256")
    )
    before_wall = before["scenarios"].get(scenario, {}).get("wallMs")
    after_wall = after["scenarios"].get(scenario, {}).get("wallMs")
    delta = (
        None
        if not same_input
        or before_wall is None
        or after_wall is None
        else after_wall - before_wall
    )
    percent = (
        None
        if delta is None or not before_wall
        else 100.0 * delta / before_wall
    )
    return {
        "cohort": cohort,
        "fromPhase": from_phase,
        "toPhase": from_phase + 1,
        "scenario": scenario,
        "sameClientInput": bool(same_input),
        "clientInputSha256": (
            before.get("clientInputSha256") if same_input else None
        ),
        "beforeWallMs": before_wall,
        "afterWallMs": after_wall,
        "deltaMs": None if delta is None else round(delta, 4),
        "deltaPercent": None if percent is None else round(percent, 4),
    }


def build_summary(
    records: list[dict[str, Any]],
    *,
    pool_path: Path,
    fixture_path: Path,
) -> dict[str, Any]:
    nodes = [summarize_node(record) for record in records]
    by_key = {
        (node["cohort"], node["phase"]): node
        for node in nodes
    }
    comparisons: list[dict[str, Any]] = []
    for phase in range(1, 8):
        old = by_key.get(("old", phase))
        new = by_key.get(("new", phase))
        if old is None or new is None:
            continue
        for scenario in sorted(
            set(old["scenarios"]) & set(new["scenarios"])
        ):
            comparisons.append(
                comparison_row(phase, old, new, scenario)
            )
    adjacent_comparisons: list[dict[str, Any]] = []
    for cohort in ("old", "new"):
        for to_phase in range(2, 8):
            before = by_key.get((cohort, to_phase - 1))
            after = by_key.get((cohort, to_phase))
            if before is None or after is None:
                continue
            for scenario in sorted(
                set(before["scenarios"]) & set(after["scenarios"])
            ):
                adjacent_comparisons.append(
                    adjacent_comparison_row(
                        cohort,
                        to_phase - 1,
                        before,
                        after,
                        scenario,
                    )
                )
    input_series: dict[str, dict[str, Any]] = {}
    for node in nodes:
        digest = node.get("clientInputSha256")
        if not isinstance(digest, str):
            continue
        series = input_series.setdefault(
            digest,
            {
                "clientInputSha256": digest,
                "clientActualVersion": node.get("clientActualVersion"),
                "nodes": [],
                "poolSha256": [],
            },
        )
        series["nodes"].append(
            f"{node['cohort']}-phase-{node['phase']}"
        )
        pool_sha = (node.get("poolSource") or {}).get("sha256")
        if isinstance(pool_sha, str) and pool_sha not in series["poolSha256"]:
            series["poolSha256"].append(pool_sha)
    return {
        "schemaVersion": 2,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "method": {
            "kind": "historical-vs-dictionary-phase-milestones",
            "poolPath": str(pool_path),
            "poolSha256": sha256_file(pool_path),
            "unitFixturePath": str(fixture_path),
            "unitFixtureSha256": sha256_file(fixture_path),
            "dictionaryPath": str(
                REPO_ROOT / "cslol-tools" / "hashes.game.txt"
            ),
            "dictionarySha256": sha256_file(
                REPO_ROOT / "cslol-tools" / "hashes.game.txt"
            ),
            "osPageCachePolicy": "uncontrolled",
            "interpretation": (
                "Pairwise rows compare cumulative historical and new "
                "milestones at equal materialized workload; they are not "
                "isolated single-patch microbenchmarks. Adjacent rows compare "
                "successive milestones within one cohort and publish no delta "
                "when the exact client input differs."
            ),
        },
        "inputSeries": list(input_series.values()),
        "nodes": nodes,
        "comparisons": comparisons,
        "adjacentComparisons": adjacent_comparisons,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run historical and dictionary-backed Phase commits against one "
            "normalized five-champion workload."
        )
    )
    parser.add_argument(
        "--cohort",
        choices=("old", "new", "all"),
        default="all",
    )
    parser.add_argument(
        "--phase",
        action="append",
        type=int,
        choices=range(1, 8),
        default=[],
    )
    parser.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument(
        "--python",
        type=Path,
        default=REPO_ROOT / ".venv" / "Scripts" / "python.exe",
    )
    parser.add_argument("--runner", type=Path, default=DEFAULT_RUNNER)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--result-root", type=Path, default=DEFAULT_RESULT_ROOT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument(
        "--scenarios",
        default="app-cold-build,output-cache-hit",
    )
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument(
        "--no-phase7-derived-warm",
        action="store_true",
    )
    parser.add_argument(
        "--derived-warm-phase",
        action="append",
        type=int,
        choices=range(1, 8),
        default=[],
        help="add the derived-warm scenario to this Phase number",
    )
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument(
        "--allow-mixed-input-series",
        action="store_true",
        help=(
            "resume passed nodes captured with another pool identity; "
            "pairwise comparisons still require exact client identity"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cohorts = ("old", "new") if args.cohort == "all" else (args.cohort,)
    phases = args.phase or list(range(1, 8))
    nodes = selected_nodes(cohorts, phases)
    pool_path = args.pool.resolve(strict=True)
    fixture_path = args.fixture.resolve(strict=True)
    python = args.python.resolve(strict=True)
    pool, fixture = validate_inputs(
        pool_path,
        fixture_path,
        nodes,
        python,
    )
    print(
        f"pool={pool['poolId']} skins={pool['totals']['skins']} "
        f"paired={pool['totals']['paired']} nodes={len(nodes)}"
    )
    for node in nodes:
        print(
            f"  {node.slug}: {node.commit} -> "
            f"{resolve_commit(node.commit)[:12]}"
        )
    if args.dry_run:
        return 0

    runner = args.runner.resolve()
    data_root = args.data_root.resolve()
    result_root = args.result_root.resolve()
    derived_warm_phases = set(args.derived_warm_phase)
    if not args.no_phase7_derived_warm:
        derived_warm_phases.add(7)
    ensure_runner(runner, nodes[0].commit)
    records: list[dict[str, Any]] = []
    failures: list[str] = []
    for node in nodes:
        try:
            record = run_node(
                node,
                runner=runner,
                python=python,
                pool_path=pool_path,
                pool=pool,
                fixture=fixture,
                data_root=data_root,
                result_root=result_root,
                scenarios=args.scenarios,
                timeout_seconds=args.timeout_seconds,
                include_derived_warm=(
                    node.phase in derived_warm_phases
                ),
                resume=not args.no_resume,
                allow_mixed_input_series=args.allow_mixed_input_series,
            )
            if (
                not record["resultPath"].is_file()
                or not record["workloadPath"].is_file()
            ):
                failures.append(node.slug)
                print(
                    f"{node.slug}: result or workload artifact is missing",
                    file=sys.stderr,
                )
                continue
            records.append(record)
            workload = read_json(record["workloadPath"])
            if record["returnCode"] != 0 or not workload.get("valid"):
                failures.append(node.slug)
        except (
            GitPhaseBenchmarkError,
            OSError,
            ValueError,
            subprocess.CalledProcessError,
        ) as exc:
            failures.append(node.slug)
            print(f"{node.slug}: ERROR: {exc}", file=sys.stderr)

    if records:
        summary = build_summary(
            records,
            pool_path=pool_path,
            fixture_path=fixture_path,
        )
        write_json(args.summary.resolve(), summary)
        print(f"summary={args.summary.resolve()}")
    if failures:
        print(f"failed nodes: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
