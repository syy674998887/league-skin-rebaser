"""Verify ChampionLayout direct reads against a hash-aware legacy extraction."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = Path(__file__).resolve().parent
SRC_ROOT = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TOOLS_ROOT))

from rebaser import champion_layout  # noqa: E402
import golden_local  # noqa: E402
from tools import golden_oracle  # noqa: E402
import script  # noqa: E402
from rebaser import wad_access  # noqa: E402


DEFAULT_POOL = REPO_ROOT / "benchmarks" / "pools" / "upgrade-v2-fast5.json"
DEFAULT_REGISTRY = REPO_ROOT / "data" / "champion-units.generated.json"
DEFAULT_OUTPUT = (
    REPO_ROOT / ".cache" / "audits" / "layout-shadow.json"
)
IMPLEMENTATION_SOURCES = (
    REPO_ROOT / "tools" / "layout_shadow.py",
    REPO_ROOT / "tools" / "golden_local.py",
    REPO_ROOT / "tools" / "golden_oracle.py",
    REPO_ROOT / "src" / "rebaser" / "champion_layout.py",
    REPO_ROOT / "src" / "rebaser" / "wad_access.py",
    REPO_ROOT / "src" / "rebaser" / "app.py",
    REPO_ROOT / "script.py",
)


class LayoutShadowError(ValueError):
    """The Shadow Gate could not prove byte-for-byte equivalence."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LayoutShadowError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def read_bound_json(path: Path) -> tuple[Any, dict[str, object], bytes]:
    resolved = path.resolve(strict=True)
    if resolved.is_symlink():
        raise LayoutShadowError(f"input must not be a symlink: {resolved}")
    before = resolved.stat()
    raw = resolved.read_bytes()
    after = resolved.stat()
    before_key = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_key = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if before_key != after_key or len(raw) != after.st_size:
        raise LayoutShadowError(f"input changed while reading: {resolved}")
    try:
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LayoutShadowError(f"failed parsing {resolved}: {exc}") from exc
    return data, {
        "path": str(resolved),
        "size": after.st_size,
        "modifiedNs": after.st_mtime_ns,
        "sha256": sha256_bytes(raw),
    }, raw


def require_identity(
    expected: dict[str, Any],
    actual: dict[str, object],
    label: str,
) -> None:
    differences = [
        field
        for field in ("path", "size", "modifiedNs", "sha256")
        if expected.get(field) != actual.get(field)
    ]
    if differences:
        raise LayoutShadowError(
            f"{label} identity differs in {differences}: "
            f"expected={expected!r}, actual={actual!r}"
        )


def git_head() -> str:
    completed = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={REPO_ROOT.as_posix()}",
            "rev-parse",
            "HEAD",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    head = completed.stdout.strip()
    if completed.returncode or len(head) != 40:
        raise LayoutShadowError(
            f"failed resolving git HEAD: {completed.stderr.strip()}"
        )
    return head


def validate_pool_subset(pool: dict[str, Any], parent: dict[str, Any]) -> None:
    parent_by_id = {
        item.get("championId"): item
        for item in parent.get("champions", [])
        if isinstance(item, dict)
    }
    champions = pool.get("champions")
    if not isinstance(champions, list) or not champions:
        raise LayoutShadowError("Shadow pool has no champions")
    for champion in champions:
        if not isinstance(champion, dict):
            raise LayoutShadowError("Shadow pool champion must be an object")
        champion_id = champion.get("championId")
        if parent_by_id.get(champion_id) != champion:
            raise LayoutShadowError(
                f"Shadow champion {champion_id!r} is not an exact "
                "parent-pool record"
            )


def source_pair_key(pair: dict[str, Any]) -> tuple[int, str, str, str]:
    context = pair.get("context")
    if not isinstance(context, dict):
        raise LayoutShadowError("source pair has no context")
    skin = context.get("skin_number")
    unit = context.get("unit")
    base_path = pair.get("basePath")
    target_path = pair.get("targetPath")
    if (
        type(skin) is not int
        or not isinstance(unit, str)
        or not isinstance(base_path, str)
        or not isinstance(target_path, str)
    ):
        raise LayoutShadowError(f"malformed source pair: {pair!r}")
    return (
        skin,
        unit,
        wad_access.normalize_wad_path(base_path),
        wad_access.normalize_wad_path(target_path),
    )


def source_pair_map(
    source_champion: dict[str, Any],
) -> dict[tuple[int, str, str, str], dict[str, Any]]:
    status = source_champion.get("status")
    if status == "expected_unsupported":
        legacy_failure = source_champion.get("legacyFailure")
        if (
            not isinstance(legacy_failure, dict)
            or legacy_failure.get("validated") is not True
        ):
            raise LayoutShadowError("legacy failure source anchor is unvalidated")
        return {}
    if status != "success":
        raise LayoutShadowError(f"invalid source status {status!r}")
    pairs = source_champion.get("pairs")
    if not isinstance(pairs, list):
        raise LayoutShadowError("source success record has no pairs")
    result: dict[tuple[int, str, str, str], dict[str, Any]] = {}
    for pair in pairs:
        if not isinstance(pair, dict):
            raise LayoutShadowError("source pair must be an object")
        key = source_pair_key(pair)
        if key in result:
            raise LayoutShadowError(f"duplicate source pair {key!r}")
        result[key] = pair
    return result


def layout_pairs(
    layout: champion_layout.ChampionLayout,
) -> list[tuple[int, champion_layout.LayoutUnit]]:
    return [
        (skin.skin_number, pair)
        for skin in layout.skins
        for pair in skin.paired
    ]


def required_paths(
    pairs: Iterable[tuple[int, champion_layout.LayoutUnit]],
) -> tuple[str, ...]:
    paths: set[str] = set()
    for _, pair in pairs:
        if pair.base_path is None or pair.target_path is None:
            raise LayoutShadowError(f"paired unit {pair.unit!r} has no path")
        paths.add(wad_access.normalize_wad_path(pair.base_path))
        paths.add(wad_access.normalize_wad_path(pair.target_path))
    return tuple(sorted(paths))


def compare_pairs(
    *,
    champion_name: str,
    pairs: list[tuple[int, champion_layout.LayoutUnit]],
    direct_by_path: dict[str, bytes],
    legacy_index: golden_oracle.LegacyExtractIndex,
    source_pairs: dict[tuple[int, str, str, str], dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    direct_keys: set[tuple[int, str, str, str]] = set()
    direct_only = 0
    for skin_number, pair in pairs:
        if (
            pair.base_path is None
            or pair.target_path is None
            or pair.base_chunk is None
            or pair.target_chunk is None
        ):
            raise LayoutShadowError(f"incomplete paired state for {pair.unit}")
        base_path = wad_access.normalize_wad_path(pair.base_path)
        target_path = wad_access.normalize_wad_path(pair.target_path)
        key = (skin_number, pair.unit, base_path, target_path)
        if key in direct_keys:
            raise LayoutShadowError(f"duplicate direct pair {key!r}")
        direct_keys.add(key)
        context = golden_oracle.GoldenContext(
            champion=champion_name,
            skin_number=skin_number,
            unit=pair.unit,
            stage="phase2-layout-shadow",
        )
        golden = golden_oracle.build_pair_golden(
            index=legacy_index,
            context=context,
            base_path=base_path,
            base_hash=pair.base_chunk.path_hash,
            base_direct=direct_by_path[base_path],
            target_path=target_path,
            target_hash=pair.target_chunk.path_hash,
            target_direct=direct_by_path[target_path],
        )
        source = source_pairs.get(key)
        if source is None:
            direct_only += 1
        elif (
            source.get("baseSha256") != golden.base_sha256
            or source.get("targetSha256") != golden.target_sha256
        ):
            raise LayoutShadowError(
                f"{context.describe()}: Phase 1 source SHA differs"
            )
        records.append(
            {
                "skinNumber": skin_number,
                "unit": pair.unit,
                "classification": (
                    "legacy_anchor" if source is not None else "direct_only"
                ),
                "basePath": base_path,
                "basePathHash": f"{pair.base_chunk.path_hash:016x}",
                "baseSha256": golden.base_sha256,
                "targetPath": target_path,
                "targetPathHash": f"{pair.target_chunk.path_hash:016x}",
                "targetSha256": golden.target_sha256,
                "oracleVerified": True,
            }
        )
    missing = sorted(set(source_pairs) - direct_keys)
    if missing:
        raise LayoutShadowError(
            f"ChampionLayout omitted {len(missing)} legacy pairs: {missing[:5]!r}"
        )
    return records, direct_only


def layout_counts(layout: champion_layout.ChampionLayout) -> dict[str, int]:
    return {
        "skins": len(layout.skins),
        "paired": sum(len(skin.paired) for skin in layout.skins),
        "baseOnly": sum(len(skin.base_only) for skin in layout.skins),
        "targetOnly": sum(len(skin.target_only) for skin in layout.skins),
        "absent": sum(len(skin.absent_candidates) for skin in layout.skins),
    }


def build_result(args: argparse.Namespace) -> dict[str, Any]:
    pool, pool_identity, _ = read_bound_json(args.pool)
    source, source_identity, _ = read_bound_json(args.source_golden)
    if not isinstance(pool, dict) or pool.get("poolId") != "upgrade-v2-fast5":
        raise LayoutShadowError("Shadow Gate requires upgrade-v2-fast5")
    if (
        not isinstance(source, dict)
        or source.get("schemaVersion") != 2
        or source.get("status") != "passed"
        or source.get("complete") is not True
    ):
        raise LayoutShadowError("source Golden is not a complete schema-2 pass")

    parent_identity = source.get("pool")
    if not isinstance(parent_identity, dict):
        raise LayoutShadowError("source Golden has no parent-pool identity")
    parent_path = Path(str(parent_identity.get("path", "")))
    parent, current_parent_identity, _ = read_bound_json(parent_path)
    require_identity(parent_identity, current_parent_identity, "parent pool")
    if not isinstance(parent, dict):
        raise LayoutShadowError("parent pool is not an object")
    validate_pool_subset(pool, parent)

    source_records = source.get("champions")
    if not isinstance(source_records, list):
        raise LayoutShadowError("source Golden has no champions")
    source_by_id = {
        item.get("championId"): item
        for item in source_records
        if isinstance(item, dict)
    }

    _, registry_identity, registry_raw = read_bound_json(args.registry)
    config = golden_local.read_json(REPO_ROOT / "config.json")
    league_root = Path(config["lol_path"]).resolve(strict=True)
    champions_dir = league_root / "Game" / "DATA" / "FINAL" / "Champions"
    client = golden_local.installed_game_identity(
        league_root,
        str(pool["gameVersion"]),
    )
    metadata_path = league_root / "Game" / "content-metadata.json"
    metadata_identity = client["source"]

    generation = script.capture_lcu_wad_generation(champions_dir)
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
    identities = champion_layout.parse_official_champion_identities(
        summary_record.data,
        skins_record.data,
    )
    identity_by_id = {item.champion_id: item for item in identities}
    registry = champion_layout.load_candidate_registry(
        args.registry,
        identities,
        raw_bytes=registry_raw,
    )
    script.assert_lcu_generation_unchanged(
        champions_dir,
        generation,
        "binding Shadow LCU inputs",
    )

    hashes_path = golden_local.require_bundled_hash_source(args.hashes_game)
    hashes_identity = golden_local.stable_file_identity(hashes_path)
    tool_identity = golden_local.stable_file_identity(script.WAD_EXTRACT)
    if not isinstance(source.get("hashSource"), dict):
        raise LayoutShadowError("source Golden has no hash identity")
    if not isinstance(source.get("legacyTool"), dict):
        raise LayoutShadowError("source Golden has no legacy-tool identity")
    require_identity(source["hashSource"], hashes_identity, "hash source")
    require_identity(source["legacyTool"], tool_identity, "legacy tool")

    result: dict[str, Any] = {
        "schemaVersion": 1,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "complete": False,
        "expectedChampionCount": len(pool["champions"]),
        "processedChampionCount": 0,
        "poolId": pool["poolId"],
        "gameVersion": pool["gameVersion"],
        "inputs": {
            "implementation": {
                "gitHead": git_head(),
                "sources": [
                    golden_local.stable_file_identity(path)
                    for path in IMPLEMENTATION_SOURCES
                ],
            },
            "pool": pool_identity,
            "parentPool": current_parent_identity,
            "sourceGolden": source_identity,
            "registry": registry_identity,
            "client": client,
            "lcuWads": script.lcu_wad_generation_document(generation),
            "lcuJson": [
                script.lcu_json_source_document(summary_record.source),
                script.lcu_json_source_document(skins_record.source),
            ],
            "legacyTool": tool_identity,
            "hashSource": hashes_identity,
        },
        "inputStability": {
            "status": "pending",
            "executionMode": "private-verified-tool-and-hash-copies",
        },
        "champions": [],
    }

    with tempfile.TemporaryDirectory(
        prefix=".layout-shadow-inputs-",
        dir=REPO_ROOT,
    ) as snapshot_name:
        snapshot_root = Path(snapshot_name)
        private_tool = snapshot_root / script.WAD_EXTRACT.name
        private_hashes = snapshot_root / hashes_path.name
        private_tool_identity = golden_local.copy_verified_input_snapshot(
            script.WAD_EXTRACT,
            private_tool,
            tool_identity,
        )
        private_hashes_identity = golden_local.copy_verified_input_snapshot(
            hashes_path,
            private_hashes,
            hashes_identity,
        )
        stable_inputs = (
            ("pool", args.pool, pool_identity),
            ("parent pool", parent_path, current_parent_identity),
            ("source Golden", args.source_golden, source_identity),
            ("registry", args.registry, registry_identity),
            ("client metadata", metadata_path, metadata_identity),
            ("legacy tool", script.WAD_EXTRACT, tool_identity),
            ("hash source", hashes_path, hashes_identity),
            ("private tool", private_tool, private_tool_identity),
            ("private hashes", private_hashes, private_hashes_identity),
        )

        for champion in pool["champions"]:
            champion_id = champion["championId"]
            identity = identity_by_id.get(champion_id)
            if identity is None:
                raise LayoutShadowError(
                    f"champion {champion_id} is not in the official roster"
                )
            expected_binding = (
                champion["query"],
                champion["wadName"],
                champion["mainUnit"],
            )
            actual_binding = (
                identity.display_name,
                f"{identity.wad_base}.wad.client",
                identity.main_unit,
            )
            if expected_binding != actual_binding:
                raise LayoutShadowError(
                    f"pool/LCU binding differs for {champion_id}: "
                    f"{expected_binding!r} != {actual_binding!r}"
                )
            source_champion = source_by_id.get(champion_id)
            if not isinstance(source_champion, dict):
                raise LayoutShadowError(
                    f"source Golden has no champion {champion_id}"
                )
            wad_path = champions_dir / champion["wadName"]
            wad_identity = golden_local.stable_file_identity(wad_path)
            source_wad = source_champion.get("wad")
            if not isinstance(source_wad, dict):
                raise LayoutShadowError(f"{champion['query']} has no WAD anchor")
            require_identity(source_wad, wad_identity, f"{champion['query']} WAD")

            errors = golden_local.require_execution_inputs_unchanged(
                stable_inputs
            )
            if errors:
                raise LayoutShadowError("; ".join(errors))
            script.assert_lcu_generation_unchanged(
                champions_dir,
                generation,
                f"starting {champion['query']}",
            )
            prepared = wad_access.PreparedChampionWad(wad_path, identity=identity)
            skin_numbers = golden_local.expand_skin_set(champion)
            layout = champion_layout.build_champion_layout(
                identity,
                prepared,
                skin_numbers,
                registry,
            )
            pairs = layout_pairs(layout)
            paths = required_paths(pairs)
            direct_by_path = prepared.read_many(paths, validate_bin=True)
            with tempfile.TemporaryDirectory(
                prefix=".layout-shadow-champion-",
                dir=REPO_ROOT,
            ) as temp_name:
                extracted = script.extract_wad_to_temp_dir(
                    wad_path,
                    Path(temp_name),
                    purpose="layout-shadow",
                    wad_extract_path=private_tool,
                    hashes_path=private_hashes,
                    expected_wad_identity=prepared.file_identity,
                    expected_toc_digest=prepared.toc_digest,
                )
                legacy_index = golden_oracle.LegacyExtractIndex(extracted)
                anchors = source_pair_map(source_champion)
                pair_records, direct_only = compare_pairs(
                    champion_name=champion["query"],
                    pairs=pairs,
                    direct_by_path=direct_by_path,
                    legacy_index=legacy_index,
                    source_pairs=anchors,
                )

            golden_local.require_file_unchanged(wad_path, wad_identity)
            errors = golden_local.require_execution_inputs_unchanged(
                stable_inputs
            )
            if errors:
                raise LayoutShadowError("; ".join(errors))
            script.assert_lcu_generation_unchanged(
                champions_dir,
                generation,
                f"finishing {champion['query']}",
            )
            serialized = champion_layout.serialize_champion_layout(layout)
            serialized["wad"]["sha256"] = wad_identity["sha256"]
            record = {
                "championId": champion_id,
                "champion": champion["query"],
                "status": "passed",
                "sourceStatus": source_champion["status"],
                "skinSet": list(skin_numbers),
                "wad": {
                    **wad_identity,
                    "tocDigest": prepared.toc_digest,
                },
                "layoutSha256": canonical_sha256(serialized),
                "layout": serialized,
                "counts": {
                    **layout_counts(layout),
                    "legacyResolved": len(anchors),
                    "directOnly": direct_only,
                    "oracleVerified": len(pair_records),
                    "uniqueDirectPaths": len(paths),
                },
                "legacyResolvedSubset": True,
                "allDirectPairsOracleVerified": True,
                "pairs": pair_records,
            }
            result["champions"].append(record)
            result["processedChampionCount"] = len(result["champions"])
            script.write_json_atomically(args.output, result)
            print(
                f"{champion['query']:<14} passed "
                f"pairs={len(pair_records):>3} direct_only={direct_only:>3}",
                flush=True,
            )

        errors = golden_local.require_execution_inputs_unchanged(stable_inputs)
        if errors:
            raise LayoutShadowError("; ".join(errors))
        script.assert_lcu_generation_unchanged(
            champions_dir,
            generation,
            "finalizing Shadow result",
        )
        result["inputStability"]["status"] = "passed"

    count_fields = (
        "skins",
        "paired",
        "baseOnly",
        "targetOnly",
        "absent",
        "legacyResolved",
        "directOnly",
        "oracleVerified",
    )
    result["summary"] = {
        field: sum(
            champion["counts"][field]
            for champion in result["champions"]
        )
        for field in count_fields
    }
    result["summary"]["champions"] = len(result["champions"])
    result["status"] = "passed"
    result["complete"] = True
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    parser.add_argument(
        "--source-golden",
        type=Path,
        required=True,
    )
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument(
        "--hashes-game",
        type=Path,
        default=REPO_ROOT / "cslol-tools" / "hashes.game.txt",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for field in (
        "pool",
        "source_golden",
        "registry",
        "hashes_game",
        "output",
    ):
        setattr(args, field, getattr(args, field).resolve())
    marker: dict[str, Any] = {
        "schemaVersion": 1,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "complete": False,
        "expectedChampionCount": 0,
        "processedChampionCount": 0,
        "champions": [],
    }
    script.write_json_atomically(args.output, marker)
    try:
        result = build_result(args)
    except (LayoutShadowError, OSError, SystemExit, wad_access.WadError) as exc:
        marker["status"] = "failed"
        marker["complete"] = True
        marker["error"] = f"{type(exc).__name__}: {exc}"
        script.write_json_atomically(args.output, marker)
        print(marker["error"], file=sys.stderr)
        return 1
    script.write_json_atomically(args.output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
