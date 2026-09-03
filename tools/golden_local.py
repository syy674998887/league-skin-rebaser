"""Build a strict local direct/legacy Golden for the fixed champion pool."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from tools import golden_oracle  # noqa: E402
import script  # noqa: E402
from rebaser import wad_access  # noqa: E402


SKIN_PATH_RE = re.compile(
    r"^data/characters/(?P<unit>[a-z0-9_]+)/skins/skin(?P<skin>\d+)\.bin$",
    re.IGNORECASE,
)
ANNIE_XXH64_VECTOR_PATH = "data/characters/annie/skins/skin0.bin"
ANNIE_XXH64_VECTOR = 0x599C1DD4B0FE6EF4
GAME_VERSION_RE = re.compile(r"^(?P<core>\d+\.\d+\.\d+)")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_file_identity(path: Path) -> dict[str, object]:
    resolved = path.resolve(strict=True)
    before = resolved.stat()
    digest = sha256_file(resolved)
    after = resolved.stat()
    before_key = (before.st_size, before.st_mtime_ns)
    after_key = (after.st_size, after.st_mtime_ns)
    if before_key != after_key:
        raise OSError(f"file changed while hashing: {resolved}")
    return {
        "path": str(resolved),
        "size": after.st_size,
        "modifiedNs": after.st_mtime_ns,
        "sha256": digest,
    }


def require_file_unchanged(path: Path, identity: dict[str, object]) -> None:
    current = stable_file_identity(path)
    if current != identity:
        raise OSError(f"file changed during Golden run: {path}")


def copy_verified_input_snapshot(
    source: Path,
    destination: Path,
    expected: dict[str, object],
) -> dict[str, object]:
    """Copy one identity-bound input and verify both ends by full SHA-256."""

    if source.is_symlink():
        raise OSError(f"Golden input must not be a symlink: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    snapshot = stable_file_identity(destination)
    require_file_unchanged(source, expected)
    for field in ("size", "sha256"):
        if snapshot[field] != expected[field]:
            raise OSError(
                f"Golden input snapshot {field} mismatch for {source}: "
                f"{snapshot[field]!r} != {expected[field]!r}"
            )
    return snapshot


def require_execution_inputs_unchanged(
    inputs: tuple[tuple[str, Path, dict[str, object]], ...],
) -> list[str]:
    errors: list[str] = []
    for label, path, identity in inputs:
        try:
            require_file_unchanged(path, identity)
        except OSError as exc:
            errors.append(f"{label}: {exc}")
    return errors


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def bundled_hashes_path() -> Path:
    return script.WAD_EXTRACT.with_name("hashes.game.txt").resolve()


def require_bundled_hash_source(requested: Path) -> Path:
    expected = bundled_hashes_path()
    actual = requested.resolve(strict=True)
    if actual != expected:
        raise ValueError(
            "Phase 0 Golden must use the hashes.game.txt bundled beside "
            f"wad-extract.exe: expected {expected}, got {actual}"
        )
    if script.WAD_EXTRACT.resolve(strict=True).parent != actual.parent:
        raise ValueError("wad-extract.exe and hashes.game.txt are not co-located")
    return actual


def comparable_game_version(version: str) -> str:
    core = version.split("+", 1)[0]
    parts = core.split(".")
    if len(parts) == 4 and all(part.isdecimal() for part in parts):
        return ".".join((parts[0], parts[1], parts[2] + parts[3]))
    match = GAME_VERSION_RE.match(core)
    if match is None:
        raise ValueError(f"unrecognized game version: {version!r}")
    return match.group("core")


def installed_game_identity(
    league_root: Path,
    expected_version: str,
) -> dict[str, object]:
    metadata_path = league_root / "Game" / "content-metadata.json"
    metadata_identity = stable_file_identity(metadata_path)
    metadata = read_json(metadata_path)
    actual_version = metadata.get("version")
    if not isinstance(actual_version, str) or not actual_version:
        raise ValueError(f"missing version in {metadata_path}")
    expected_comparable = comparable_game_version(expected_version)
    actual_comparable = comparable_game_version(actual_version)
    if actual_comparable != expected_comparable:
        raise ValueError(
            f"pool gameVersion {expected_version} does not match installed "
            f"content version {actual_version}"
        )
    return {
        "expectedVersion": expected_version,
        "actualVersion": actual_version,
        "comparableVersion": actual_comparable,
        "source": metadata_identity,
    }


def scan_known_skin_paths(
    hashes_path: Path,
    wanted_hashes: set[int],
    *,
    validate_annie_vector: bool = False,
) -> dict[int, str]:
    found: dict[int, str] = {}
    hashes_by_path: dict[str, int] = {}
    paths_by_hash: dict[int, str] = {}
    annie_vector_seen = False
    with hashes_path.open("r", encoding="utf-8", errors="strict") as handle:
        for line_number, line in enumerate(handle, start=1):
            parts = line.rstrip("\r\n").split(maxsplit=1)
            if len(parts) != 2:
                continue
            normalized = wad_access.normalize_wad_path(parts[1])
            if SKIN_PATH_RE.fullmatch(normalized) is None:
                continue
            try:
                path_hash = int(parts[0], 16)
            except ValueError as exc:
                raise ValueError(
                    f"invalid declared hash for relevant skin path on line "
                    f"{line_number}: {parts[0]!r}"
                ) from exc
            if validate_annie_vector and normalized == ANNIE_XXH64_VECTOR_PATH:
                if path_hash != ANNIE_XXH64_VECTOR:
                    raise ValueError(
                        "Annie Phase 0 XXH64 fixed vector mismatch: "
                        f"expected {ANNIE_XXH64_VECTOR:016x}, got "
                        f"{path_hash:016x}"
                    )
                annie_vector_seen = True
            computed_hash = wad_access.wad_path_hash(normalized)
            if path_hash != computed_hash:
                raise ValueError(
                    f"declared hash mismatch for relevant skin path on line "
                    f"{line_number}: {normalized!r} declares "
                    f"{path_hash:016x}, computed {computed_hash:016x}"
                )
            previous_path = paths_by_hash.get(path_hash)
            if previous_path is not None and previous_path != normalized:
                raise ValueError(
                    f"hash collision in source: {path_hash:016x} maps to "
                    f"{previous_path!r} and {normalized!r}"
                )
            previous_hash = hashes_by_path.get(normalized)
            if previous_hash is not None and previous_hash != path_hash:
                raise ValueError(
                    f"path collision in source: {normalized!r} maps to "
                    f"{previous_hash:016x} and {path_hash:016x}"
                )
            paths_by_hash[path_hash] = normalized
            hashes_by_path[normalized] = path_hash
            if path_hash not in wanted_hashes:
                continue
            found[path_hash] = normalized
    if validate_annie_vector and not annie_vector_seen:
        raise ValueError(
            "bundled hashes.game.txt is missing the Annie Phase 0 XXH64 "
            f"fixed vector {ANNIE_XXH64_VECTOR_PATH!r}"
        )
    return found


def expand_skin_set(champion: dict[str, Any]) -> tuple[int, ...]:
    skin_set = champion.get("skinSet")
    if not isinstance(skin_set, dict):
        raise ValueError(f"{champion.get('query')}: missing skinSet")
    ranges = skin_set.get("ranges")
    excluded = skin_set.get("exclude")
    if not isinstance(ranges, list) or not isinstance(excluded, list):
        raise ValueError(f"{champion.get('query')}: invalid skinSet")
    excluded_set: set[int] = set()
    for value in excluded:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{champion.get('query')}: invalid excluded skin {value!r}")
        if value in excluded_set:
            raise ValueError(f"{champion.get('query')}: duplicate excluded skin {value}")
        excluded_set.add(value)

    declared: list[int] = []
    seen: set[int] = set()
    for item in ranges:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in item
            )
        ):
            raise ValueError(f"{champion.get('query')}: invalid skin range {item!r}")
        first, last = item
        if first <= 0 or last < first or last > 999:
            raise ValueError(f"{champion.get('query')}: invalid skin range {item!r}")
        for skin_number in range(first, last + 1):
            if skin_number in seen:
                raise ValueError(
                    f"{champion.get('query')}: overlapping skinSet at skin"
                    f"{skin_number}"
                )
            seen.add(skin_number)
            if skin_number not in excluded_set:
                declared.append(skin_number)
    if not excluded_set.issubset(seen):
        unknown = sorted(excluded_set - seen)
        raise ValueError(
            f"{champion.get('query')}: exclusions outside ranges: {unknown}"
        )
    expected_count = champion.get("skinCount")
    if expected_count != len(declared):
        raise ValueError(
            f"{champion.get('query')}: skinSet expands to {len(declared)}, "
            f"expected skinCount={expected_count}"
        )
    return tuple(sorted(declared))


def classify_skin_paths(
    paths_by_hash: dict[int, str],
    skin_numbers: tuple[int, ...],
) -> list[dict[str, object]]:
    by_unit: dict[str, dict[int, tuple[int, str]]] = {}
    for path_hash, path in paths_by_hash.items():
        match = SKIN_PATH_RE.fullmatch(path)
        if match is None:
            continue
        unit = match.group("unit").lower()
        skin_number = int(match.group("skin"))
        previous = by_unit.setdefault(unit, {}).get(skin_number)
        current = (path_hash, path)
        if previous is not None and previous != current:
            raise ValueError(
                f"multiple hashes for {unit} skin{skin_number}: "
                f"{previous[0]:016x}, {path_hash:016x}"
            )
        by_unit[unit][skin_number] = current

    selected = set(skin_numbers)
    relevant_units = [
        unit
        for unit, skins in sorted(by_unit.items())
        if 0 in skins or selected.intersection(skins)
    ]
    layouts: list[dict[str, object]] = []
    for skin_number in skin_numbers:
        paired: list[dict[str, object]] = []
        base_only: list[dict[str, object]] = []
        target_only: list[dict[str, object]] = []
        absent: list[str] = []
        for unit in relevant_units:
            skins = by_unit[unit]
            base = skins.get(0)
            target = skins.get(skin_number)
            if base is not None and target is not None:
                paired.append(
                    {
                        "unit": unit,
                        "basePathHash": base[0],
                        "basePath": base[1],
                        "targetPathHash": target[0],
                        "targetPath": target[1],
                    }
                )
            elif base is not None:
                base_only.append(
                    {
                        "unit": unit,
                        "basePathHash": base[0],
                        "basePath": base[1],
                    }
                )
            elif target is not None:
                target_only.append(
                    {
                        "unit": unit,
                        "targetPathHash": target[0],
                        "targetPath": target[1],
                    }
                )
            else:
                absent.append(unit)
        layouts.append(
            {
                "skinNumber": skin_number,
                "paired": paired,
                "baseOnly": base_only,
                "targetOnly": target_only,
                "absent": absent,
            }
        )
    return layouts


def serialized_states(
    states: list[dict[str, object]],
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for state in states:
        item = dict(state)
        for field in ("basePathHash", "targetPathHash"):
            value = item.get(field)
            if isinstance(value, int):
                item[field] = f"{value:016x}"
        out.append(item)
    return out


def validate_success_layouts(
    champion: dict[str, Any],
    paths_by_hash: dict[int, str],
    layouts: list[dict[str, object]],
) -> dict[str, int]:
    skin_numbers = expand_skin_set(champion)
    layout_skin_numbers = tuple(
        int(layout["skinNumber"])
        for layout in layouts
    )
    if layout_skin_numbers != skin_numbers:
        raise ValueError(
            f"{champion['query']}: classified skinSet {layout_skin_numbers} "
            f"does not match declared {skin_numbers}"
        )

    main_unit = champion["mainUnit"].lower()
    actual_main_skins: set[int] = set()
    for path in paths_by_hash.values():
        match = SKIN_PATH_RE.fullmatch(path)
        if match is None or match.group("unit").lower() != main_unit:
            continue
        skin_number = int(match.group("skin"))
        if skin_number != 0:
            actual_main_skins.add(skin_number)
    if actual_main_skins != set(skin_numbers):
        raise ValueError(
            f"{champion['query']}: main unit {main_unit!r} skinSet mismatch; "
            f"missing={sorted(set(skin_numbers) - actual_main_skins)}, "
            f"unexpected={sorted(actual_main_skins - set(skin_numbers))}"
        )

    pairs = [
        pair
        for layout in layouts
        for pair in layout["paired"]
    ]
    expected_pair_count = champion.get("pairedCount")
    if len(pairs) != expected_pair_count:
        raise ValueError(
            f"{champion['query']}: pairedCount={len(pairs)}, "
            f"expected {expected_pair_count}"
        )
    unique_bases = {
        (str(pair["unit"]), int(pair["basePathHash"]))
        for pair in pairs
    }
    expected_unique_bases = champion.get("uniqueBaseCount")
    if len(unique_bases) != expected_unique_bases:
        raise ValueError(
            f"{champion['query']}: uniqueBaseCount={len(unique_bases)}, "
            f"expected {expected_unique_bases}"
        )

    missing_main = [
        int(layout["skinNumber"])
        for layout in layouts
        if main_unit not in {
            str(pair["unit"])
            for pair in layout["paired"]
        }
    ]
    if missing_main:
        raise ValueError(
            f"{champion['query']}: main unit {main_unit!r} is not paired for "
            f"skins {missing_main}"
        )
    return {
        "skinCount": len(skin_numbers),
        "pairedCount": len(pairs),
        "uniqueBaseCount": len(unique_bases),
    }


def extract_legacy_for_golden(
    wad_path: Path,
    destination: Path,
    hashes_path: Path,
    wad_extract_path: Path,
) -> Path:
    command = [
        str(wad_extract_path.resolve(strict=True)),
        str(wad_path.resolve(strict=True)),
        str(destination.resolve()),
        str(hashes_path.resolve(strict=True)),
    ]
    try:
        script.run_external_process(
            command,
            tool="wad-extract-golden",
            timing_phase="golden.wad_extract",
        )
    except script.ExternalProcessFailed as exc:
        tail = "\n".join(
            (exc.result.stdout + exc.result.stderr).splitlines()[-30:]
        )
        raise ValueError(
            "wad-extract failed while building Golden with code "
            f"{exc.result.returncode}\n{tail}"
        ) from exc
    if not destination.is_dir():
        raise ValueError(f"wad-extract did not create {destination}")
    return destination


def collect_required_pair_paths(
    layouts: list[dict[str, object]],
) -> tuple[str, ...]:
    required: list[str] = []
    seen: set[str] = set()
    for layout in layouts:
        for pair in layout["paired"]:
            for path_field, hash_field in (
                ("basePath", "basePathHash"),
                ("targetPath", "targetPathHash"),
            ):
                path = wad_access.normalize_wad_path(str(pair[path_field]))
                declared_hash = int(pair[hash_field])
                computed_hash = wad_access.wad_path_hash(path)
                if declared_hash != computed_hash:
                    raise ValueError(
                        f"layout path hash mismatch for {path!r}: "
                        f"declared {declared_hash:016x}, "
                        f"computed {computed_hash:016x}"
                    )
                if path not in seen:
                    seen.add(path)
                    required.append(path)
    return tuple(required)


def build_champion_golden(
    champion: dict[str, Any],
    prepared: wad_access.PreparedChampionWad,
    paths_by_hash: dict[int, str],
    hashes_path: Path,
    wad_extract_path: Path,
) -> dict[str, Any]:
    wad_path = prepared.wad_path
    skin_numbers = expand_skin_set(champion)
    layouts = classify_skin_paths(paths_by_hash, skin_numbers)
    coverage = validate_success_layouts(champion, paths_by_hash, layouts)
    required_paths = collect_required_pair_paths(layouts)
    direct_by_path = prepared.read_many(required_paths, validate_bin=True)

    records: list[dict[str, object]] = []
    skin_records: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(
        prefix=".golden-legacy-",
        dir=REPO_ROOT,
    ) as temp_name:
        extracted = extract_legacy_for_golden(
            wad_path,
            Path(temp_name) / "legacy.wad",
            hashes_path,
            wad_extract_path,
        )
        legacy_index = golden_oracle.LegacyExtractIndex(extracted)
        for layout in layouts:
            skin_number = int(layout["skinNumber"])
            pair_records: list[dict[str, object]] = []
            for pair in layout["paired"]:
                unit = str(pair["unit"])
                base_hash = int(pair["basePathHash"])
                base_path = str(pair["basePath"])
                target_hash = int(pair["targetPathHash"])
                target_path = str(pair["targetPath"])
                context = golden_oracle.GoldenContext(
                    champion=champion["query"],
                    skin_number=skin_number,
                    unit=unit,
                    stage="phase0-direct-legacy-bytes",
                )
                record = golden_oracle.build_pair_golden(
                    index=legacy_index,
                    context=context,
                    base_path=base_path,
                    base_hash=base_hash,
                    base_direct=direct_by_path[
                        wad_access.normalize_wad_path(base_path)
                    ],
                    target_path=target_path,
                    target_hash=target_hash,
                    target_direct=direct_by_path[
                        wad_access.normalize_wad_path(target_path)
                    ],
                ).as_dict()
                record["basePathHash"] = f"{base_hash:016x}"
                record["targetPathHash"] = f"{target_hash:016x}"
                pair_records.append(record)
                records.append(record)
            skin_records.append(
                {
                    "skinNumber": skin_number,
                    "pairs": pair_records,
                    "baseOnly": serialized_states(layout["baseOnly"]),
                    "targetOnly": serialized_states(layout["targetOnly"]),
                    "absent": list(layout["absent"]),
                }
            )

    return {
        "championId": champion["championId"],
        "champion": champion["query"],
        "status": "success",
        "skinSet": list(skin_numbers),
        **coverage,
        "pairs": records,
        "skins": skin_records,
    }


def prepare_champion_wads(
    champions: list[dict[str, Any]],
    champions_dir: Path,
) -> tuple[
    dict[int, wad_access.PreparedChampionWad],
    dict[int, dict[str, object]],
    set[int],
]:
    prepared_by_champion: dict[int, wad_access.PreparedChampionWad] = {}
    wad_identities: dict[int, dict[str, object]] = {}
    all_hashes: set[int] = set()
    for champion in champions:
        champion_id = champion["championId"]
        wad_path = champions_dir / champion["wadName"]
        wad_identities[champion_id] = stable_file_identity(wad_path)
        prepared = wad_access.PreparedChampionWad(wad_path)
        prepared_by_champion[champion_id] = prepared
        all_hashes.update(prepared.chunks_by_hash)
    return prepared_by_champion, wad_identities, all_hashes


def validate_expected_legacy_failure(
    champion: dict[str, Any],
    wad_path: Path,
    hashes_path: Path,
    wad_extract_path: Path,
) -> dict[str, Any]:
    skin_numbers = expand_skin_set(champion)
    expected_type = champion.get("legacyFailureType")
    expected_message = champion.get("legacyFailureMessage")
    if not isinstance(expected_type, str) or not expected_type:
        raise ValueError(
            f"{champion['query']}: expected unsupported champion is missing "
            "legacyFailureType"
        )
    if "legacyFailureContains" in champion:
        raise ValueError(
            f"{champion['query']}: legacyFailureContains is not supported; "
            "use exact legacyFailureMessage"
        )
    if not isinstance(expected_message, str) or not expected_message:
        raise ValueError(
            f"{champion['query']}: expected unsupported champion is missing "
            "legacyFailureMessage"
        )

    try:
        script.build_local_catalog(
            champion["query"],
            wad_path,
            wad_extract_path=wad_extract_path,
            hashes_path=hashes_path,
        )
    except BaseException as exc:
        actual_type = type(exc).__name__
        actual_text = str(exc)
        if actual_type != expected_type:
            raise ValueError(
                f"{champion['query']}: legacy failure type {actual_type!r}, "
                f"expected {expected_type!r}"
            ) from exc
        if actual_text != expected_message:
            raise ValueError(
                f"{champion['query']}: legacy failure message "
                f"{actual_text!r}, expected {expected_message!r}"
            ) from exc
    else:
        raise ValueError(
            f"{champion['query']}: legacy catalog unexpectedly succeeded"
        )

    return {
        "championId": champion["championId"],
        "champion": champion["query"],
        "status": "expected_unsupported",
        "skinSet": list(skin_numbers),
        "declaredPairCount": champion["pairedCount"],
        "declaredUniqueBaseCount": champion["uniqueBaseCount"],
        "legacyFailure": {
            "type": actual_type,
            "message": actual_text,
            "validated": True,
        },
        "coverage": {
            "status": "unsupported-by-legacy",
            "reason": (
                "Legacy extraction cannot resolve this champion's paths, so "
                "this Source Golden intentionally records no direct/legacy "
                "comparison"
            ),
        },
    }


def computed_pool_skin_paths(
    champion: dict[str, Any],
    prepared: wad_access.PreparedChampionWad,
    known_paths: dict[int, str],
) -> dict[int, str]:
    """Complete a frozen pool's paths with XXH64 lookups against the TOC."""

    skin_numbers = expand_skin_set(champion)
    main_unit = str(champion["mainUnit"]).lower()
    relevant_units = {main_unit}
    for path in known_paths.values():
        match = SKIN_PATH_RE.fullmatch(path)
        if match is not None:
            relevant_units.add(match.group("unit").lower())
    paths = tuple(
        f"data/characters/{unit}/skins/skin{skin_number}.bin"
        for unit in sorted(relevant_units)
        for skin_number in (0, *skin_numbers)
    )
    inspected = prepared.inspect_paths(paths)
    missing_main = [
        path
        for path in paths
        if f"/{main_unit}/" in path and inspected[path] is None
    ]
    if missing_main:
        raise ValueError(
            f"{champion['query']}: Direct main-unit paths are missing: "
            f"{missing_main}"
        )
    paths_by_hash: dict[int, str] = {}
    for path in paths:
        if inspected[path] is None:
            continue
        path_hash = wad_access.wad_path_hash(path)
        previous = paths_by_hash.get(path_hash)
        if previous is not None and previous != path:
            raise ValueError(
                f"{champion['query']}: computed path-hash collision "
                f"{path_hash:016x} for {previous!r} and {path!r}"
            )
        paths_by_hash[path_hash] = path
    return paths_by_hash


def build_direct_supported_legacy_unsupported_golden(
    champion: dict[str, Any],
    prepared: wad_access.PreparedChampionWad,
    known_paths: dict[int, str],
    hashes_path: Path,
    wad_extract_path: Path,
) -> dict[str, Any]:
    """Prove Direct bytes while retaining the exact legacy Catalog failure."""

    legacy = validate_expected_legacy_failure(
        champion,
        prepared.wad_path,
        hashes_path,
        wad_extract_path,
    )
    record = build_champion_golden(
        champion,
        prepared,
        computed_pool_skin_paths(champion, prepared, known_paths),
        hashes_path,
        wad_extract_path,
    )
    record["legacyFailure"] = legacy["legacyFailure"]
    record["legacyCatalog"] = {
        "status": "expected_unsupported",
        "validated": True,
    }
    record["directSupport"] = {
        "status": "success",
        "pathDiscovery": "computed-main-unit-xxh64",
        "oracleVerified": True,
    }
    return record


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pool",
        type=Path,
        default=REPO_ROOT / "benchmarks" / "pools" / "upgrade-v2.json",
    )
    parser.add_argument(
        "--hashes-game",
        type=Path,
        default=REPO_ROOT / "cslol-tools" / "hashes.game.txt",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--champion", action="append", default=[])
    return parser.parse_args(argv)


def finalize_golden_lifecycle(
    result: dict[str, Any],
    *,
    unexpected_failure: bool,
) -> bool:
    expected = result.get("expectedChampionCount")
    processed = result.get("processedChampionCount")
    count_mismatch = (
        isinstance(expected, bool)
        or not isinstance(expected, int)
        or isinstance(processed, bool)
        or not isinstance(processed, int)
        or processed != expected
    )
    failed = unexpected_failure or count_mismatch
    result["complete"] = True
    result["status"] = "failed" if failed else "passed"
    return failed


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    created_at = datetime.now(timezone.utc).isoformat()
    lifecycle: dict[str, Any] = {
        "schemaVersion": 2,
        "createdAt": created_at,
        "status": "running",
        "complete": False,
        "expectedChampionCount": 0,
        "processedChampionCount": 0,
        "champions": [],
    }
    script.write_json_atomically(args.output, lifecycle)
    pool_path = args.pool.resolve(strict=True)
    pool = read_json(pool_path)
    champions = pool["champions"]
    if args.champion:
        wanted = {name.casefold() for name in args.champion}
        available = {
            champion["query"].casefold()
            for champion in champions
        }
        missing = sorted(wanted - available)
        if missing:
            raise ValueError(f"champions not present in pool: {missing}")
        champions = [
            champion
            for champion in champions
            if champion["query"].casefold() in wanted
        ]
    if not champions:
        raise ValueError("Golden champion selection is empty")
    lifecycle["expectedChampionCount"] = len(champions)
    script.write_json_atomically(args.output, lifecycle)

    hashes_path = require_bundled_hash_source(args.hashes_game)
    config = read_json(REPO_ROOT / "config.json")
    league_root = Path(config["lol_path"]).resolve()
    champions_dir = league_root / "Game" / "DATA" / "FINAL" / "Champions"
    client_identity = installed_game_identity(
        league_root,
        pool["gameVersion"],
    )
    pool_identity = stable_file_identity(pool_path)
    hashes_identity = stable_file_identity(hashes_path)
    wad_extract_identity = stable_file_identity(script.WAD_EXTRACT)
    metadata_path = league_root / "Game" / "content-metadata.json"
    metadata_identity = client_identity["source"]

    with tempfile.TemporaryDirectory(
        prefix=".golden-inputs-",
        dir=REPO_ROOT,
    ) as snapshot_name:
        snapshot_root = Path(snapshot_name) / "legacy"
        snapshot_tool = snapshot_root / script.WAD_EXTRACT.name
        snapshot_hashes = snapshot_root / hashes_path.name
        snapshot_tool_identity = copy_verified_input_snapshot(
            script.WAD_EXTRACT,
            snapshot_tool,
            wad_extract_identity,
        )
        snapshot_hashes_identity = copy_verified_input_snapshot(
            hashes_path,
            snapshot_hashes,
            hashes_identity,
        )
        execution_inputs = (
            ("pool", pool_path, pool_identity),
            ("client metadata", metadata_path, metadata_identity),
            ("original legacy tool", script.WAD_EXTRACT, wad_extract_identity),
            ("original hash source", hashes_path, hashes_identity),
            (
                "private legacy tool snapshot",
                snapshot_tool,
                snapshot_tool_identity,
            ),
            (
                "private hash source snapshot",
                snapshot_hashes,
                snapshot_hashes_identity,
            ),
        )
        initial_input_errors = require_execution_inputs_unchanged(
            execution_inputs
        )
        if initial_input_errors:
            raise OSError("; ".join(initial_input_errors))

        prepared_by_champion, wad_identities, all_hashes = (
            prepare_champion_wads(
                champions,
                champions_dir,
            )
        )
        known_paths = scan_known_skin_paths(
            snapshot_hashes,
            all_hashes,
            validate_annie_vector=True,
        )
        snapshot_scan_errors = require_execution_inputs_unchanged(
            execution_inputs
        )
        if snapshot_scan_errors:
            raise OSError("; ".join(snapshot_scan_errors))

        result: dict[str, Any] = {
            "schemaVersion": 2,
            "createdAt": created_at,
            "status": "running",
            "complete": False,
            "expectedChampionCount": len(champions),
            "processedChampionCount": 0,
            "poolId": pool["poolId"],
            "gameVersion": pool["gameVersion"],
            "pool": pool_identity,
            "client": client_identity,
            "hashSource": {
                **hashes_identity,
                "boundToWadExtractDirectory": True,
                "validation": {
                    "scope": "phase1-all-relevant-skin-path-lines",
                    "algorithm": "XXH64",
                    "seed": 0,
                    "fixedVector": {
                        "path": ANNIE_XXH64_VECTOR_PATH,
                        "expected": f"{ANNIE_XXH64_VECTOR:016x}",
                        "validated": True,
                    },
                    "fullRelevantLineValidation": "validated",
                },
            },
            "legacyTool": wad_extract_identity,
            "inputStability": {
                "status": "pending",
                "executionMode": "private-verified-copies",
                "executionSnapshot": {
                    "legacyTool": {
                        "size": snapshot_tool_identity["size"],
                        "sha256": snapshot_tool_identity["sha256"],
                    },
                    "hashSource": {
                        "size": snapshot_hashes_identity["size"],
                        "sha256": snapshot_hashes_identity["sha256"],
                    },
                },
            },
            "champions": [],
        }
        unexpected_failure = False
        for champion in champions:
            wad_path = champions_dir / champion["wadName"]
            prepared = prepared_by_champion[champion["championId"]]
            intersection = {
                path_hash: known_paths[path_hash]
                for path_hash in prepared.chunks_by_hash
                if path_hash in known_paths
            }
            input_errors = require_execution_inputs_unchanged(
                execution_inputs
            )
            try:
                if input_errors:
                    raise OSError("; ".join(input_errors))
                if champion["legacyExpectation"] == "unsupported":
                    if champion.get("directExpectation") == "success":
                        record = (
                            build_direct_supported_legacy_unsupported_golden(
                                champion,
                                prepared,
                                intersection,
                                snapshot_hashes,
                                snapshot_tool,
                            )
                        )
                    else:
                        record = validate_expected_legacy_failure(
                            champion,
                            wad_path,
                            snapshot_hashes,
                            snapshot_tool,
                        )
                elif champion["legacyExpectation"] == "success":
                    if champion.get("directExpectation") == "success":
                        intersection = computed_pool_skin_paths(
                            champion,
                            prepared,
                            intersection,
                        )
                    record = build_champion_golden(
                        champion,
                        prepared,
                        intersection,
                        snapshot_hashes,
                        snapshot_tool,
                    )
                else:
                    raise ValueError(
                        f"{champion['query']}: unknown legacyExpectation "
                        f"{champion['legacyExpectation']!r}"
                    )
            except (OSError, ValueError, SystemExit) as exc:
                record = {
                    "championId": champion["championId"],
                    "champion": champion["query"],
                    "status": "failure",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            post_input_errors = require_execution_inputs_unchanged(
                execution_inputs
            )
            if post_input_errors:
                record = {
                    "championId": champion["championId"],
                    "champion": champion["query"],
                    "status": "failure",
                    "error": "OSError: " + "; ".join(post_input_errors),
                }
            identity = wad_identities[champion["championId"]]
            try:
                require_file_unchanged(wad_path, identity)
            except OSError as exc:
                record = {
                    "championId": champion["championId"],
                    "champion": champion["query"],
                    "status": "failure",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            record["wad"] = identity
            result["champions"].append(record)
            result["processedChampionCount"] = len(result["champions"])
            if record["status"] == "failure":
                unexpected_failure = True
            print(
                f"{champion['query']:<14} {record['status']:<22} "
                f"pairs={record.get('pairedCount', 0)}",
                flush=True,
            )
            script.write_json_atomically(args.output, result)

        final_input_errors = require_execution_inputs_unchanged(
            execution_inputs
        )
        result["inputStability"]["status"] = (
            "failed" if final_input_errors else "passed"
        )
        result["inputStability"]["checkedInputs"] = [
            label for label, _, _ in execution_inputs
        ]
        if final_input_errors:
            result["inputStability"]["errors"] = final_input_errors
            unexpected_failure = True
        unexpected_failure = finalize_golden_lifecycle(
            result,
            unexpected_failure=unexpected_failure,
        )
        script.write_json_atomically(args.output, result)
        return 1 if unexpected_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
