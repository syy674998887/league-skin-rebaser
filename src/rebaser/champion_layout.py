"""Validated champion identities, unit candidates, and per-skin WAD layouts.

This module is deliberately independent from the interactive application
flow.  Local LCU JSON establishes the official champion scope, repository
data supplies candidate units, and the mounted WAD TOC decides which paths
exist for a particular skin.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .hash_skin_index import HashSkinIndex, HashSkinRecord
from .wad_access import (
    PreparedChampionWad,
    WadChangedDuringRead,
    WadChunk,
    WadFileIdentity,
    preflight_wad_chunk,
    wad_path_hash,
)


REGISTRY_SCHEMA_VERSION = 1
LAYOUT_ALGORITHM_VERSION = 1
UNIT_NAME_RE = re.compile(r"[a-z0-9_]+\Z")
SKIN_PATH_RE = re.compile(
    r"data/characters/(?P<unit>[a-z0-9_]+)/skins/skin(?P<skin>0|[1-9]\d{0,2})\.bin\Z"
)
WAD_CLIENT_SUFFIX = ".wad.client"


class ChampionDataError(ValueError):
    """Repository or local official champion data is invalid or inconsistent."""


class ChampionIdentityError(ChampionDataError):
    """An official champion cannot be mapped to one exact resource identity."""


class CandidateRegistryError(ChampionDataError):
    """The generated candidate registry violates its schema."""


class ChampionLayoutError(ChampionDataError):
    """A per-skin layout cannot be built without guessing or dropping data."""


@dataclass(frozen=True)
class ChampionIdentity:
    champion_id: int
    display_name: str
    alias: str
    wad_base: str
    main_unit: str


@dataclass(frozen=True)
class CandidateRegistryEntry:
    champion_id: int
    alias: str
    wad_base: str
    main_unit: str
    auxiliary_units: tuple[str, ...]


@dataclass(frozen=True)
class CandidateRegistry:
    entries: Mapping[int, CandidateRegistryEntry]

    def require(self, champion_id: int) -> CandidateRegistryEntry:
        entry = self.entries.get(champion_id)
        if entry is None:
            raise CandidateRegistryError(
                f"candidate registry has no entry for official champion id {champion_id}"
            )
        return entry


@dataclass(frozen=True)
class HashSkinCandidateSet:
    """Per-WAD candidate units proven by dictionary records and its TOC."""

    champion_id: int
    candidates: tuple[str, ...]
    matched_records: tuple[HashSkinRecord, ...]
    digest: str
    source_sha256: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.champion_id, bool)
            or not isinstance(self.champion_id, int)
            or self.champion_id <= 0
        ):
            raise ValueError("candidate champion_id must be positive")
        if (
            not self.candidates
            or self.candidates != tuple(sorted(set(self.candidates)))
        ):
            raise ValueError("dictionary candidates must be sorted and unique")
        for unit in self.candidates:
            validate_unit_name(unit)
        if (
            not self.matched_records
            or self.matched_records != tuple(sorted(self.matched_records))
        ):
            raise ValueError("matched dictionary records must be sorted")
        if {
            record.unit
            for record in self.matched_records
        } != set(self.candidates):
            raise ValueError(
                "matched dictionary records must cover exactly the candidates"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", self.digest):
            raise ValueError("candidate digest must be lowercase SHA-256")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256):
            raise ValueError("candidate source digest must be lowercase SHA-256")
        if self.digest != hash_skin_candidate_digest(
            self.champion_id,
            self.candidates,
            self.matched_records,
        ):
            raise ValueError("dictionary candidate digest does not match")

    def fact(self) -> dict[str, object]:
        return {
            "championId": self.champion_id,
            "candidates": list(self.candidates),
            "records": len(self.matched_records),
            "digest": self.digest,
            "sourceSha256": self.source_sha256,
        }

    def record_for(
        self,
        unit: str,
        skin_number: int,
    ) -> HashSkinRecord | None:
        validate_unit_name(unit)
        if (
            isinstance(skin_number, bool)
            or not isinstance(skin_number, int)
            or not 0 <= skin_number <= 999
        ):
            raise ValueError(
                f"skin number must be an integer from 0 through 999: "
                f"{skin_number!r}"
            )
        for record in self.matched_records:
            if record.unit == unit and record.skin_number == skin_number:
                return record
        return None


@dataclass(frozen=True)
class ChunkIdentity:
    path_hash: int
    compressed_size: int
    decompressed_size: int
    compression_type: int
    subchunk_count: int
    subchunk_index: int
    duplicated: bool
    checksum: int | None
    checksum_kind: str

    @classmethod
    def from_chunk(cls, chunk: WadChunk) -> ChunkIdentity:
        return cls(
            path_hash=chunk.path_hash,
            compressed_size=chunk.compressed_size,
            decompressed_size=chunk.decompressed_size,
            compression_type=chunk.compression_type,
            subchunk_count=chunk.subchunk_count,
            subchunk_index=chunk.subchunk_index,
            duplicated=chunk.duplicated,
            checksum=chunk.checksum,
            checksum_kind=chunk.checksum_kind.value,
        )


@dataclass(frozen=True)
class LayoutUnit:
    unit: str
    base_path: str | None
    target_path: str | None
    base_chunk: ChunkIdentity | None
    target_chunk: ChunkIdentity | None


@dataclass(frozen=True)
class SkinLayout:
    skin_number: int
    paired: tuple[LayoutUnit, ...]
    base_only: tuple[LayoutUnit, ...]
    target_only: tuple[LayoutUnit, ...]
    absent_candidates: tuple[str, ...]


@dataclass(frozen=True)
class ChampionLayout:
    identity: ChampionIdentity
    wad_path: Path
    wad_file_identity: WadFileIdentity
    wad_version: str
    toc_digest: str
    candidates: tuple[str, ...]
    skins: tuple[SkinLayout, ...]


@dataclass(frozen=True)
class RequiredChunkIdentity:
    """Content-bound identity for one chunk that contributes to an output."""

    normalized_path: str
    chunk: ChunkIdentity
    content_sha256: str | None


def normalize_identity_lookup(value: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", value.casefold())


def validate_unit_name(value: object, label: str = "unit") -> str:
    if not isinstance(value, str) or UNIT_NAME_RE.fullmatch(value) is None:
        raise ChampionDataError(
            f"{label} must match ^[a-z0-9_]+$: {value!r}"
        )
    return value


def parse_official_champion_identities(
    summary: Any,
    skins: Any,
) -> tuple[ChampionIdentity, ...]:
    """Return the prime official champion scope from local LCU data.

    ``champion-summary.json`` also contains mode-specific/Jade records.  The
    prime roster is therefore the same strict intersection used by the
    Phase-1 LCU Gate, with the ID-0 placeholder excluded: a positive summary
    id must own a valid ``isBase``
    record in ``skins.json``.  Missing base records are excluded; malformed
    records for an otherwise matching id fail closed.
    """

    if not isinstance(summary, list):
        raise ChampionIdentityError("champion-summary.json must be a JSON array")
    if not isinstance(skins, dict):
        raise ChampionIdentityError("skins.json must be a JSON object")

    identities: list[ChampionIdentity] = []
    seen_summary_ids: set[int] = set()
    for item in summary:
        if not isinstance(item, dict):
            continue
        champion_id = item.get("id")
        if (
            isinstance(champion_id, bool)
            or not isinstance(champion_id, int)
            or champion_id <= 0
        ):
            continue
        if champion_id in seen_summary_ids:
            raise ChampionIdentityError(
                f"duplicate champion-summary id {champion_id}"
            )
        seen_summary_ids.add(champion_id)

        base_skin_id = champion_id * 1000
        base_skin = skins.get(str(base_skin_id))
        if base_skin is None:
            continue
        if (
            not isinstance(base_skin, dict)
            or base_skin.get("id") != base_skin_id
            or base_skin.get("isBase") is not True
        ):
            raise ChampionIdentityError(
                f"invalid base skin record {base_skin_id} for champion id "
                f"{champion_id}"
            )

        display_name = item.get("name")
        alias = item.get("alias")
        if not isinstance(display_name, str) or not display_name.strip():
            raise ChampionIdentityError(
                f"official champion id {champion_id} has no display name"
            )
        if not isinstance(alias, str) or not alias.strip():
            raise ChampionIdentityError(
                f"official champion id {champion_id} has no resource alias"
            )

        # The current prime roster uses the canonical Riot alias for both the
        # WAD basename and main character unit.  Every result is independently
        # checked against the installed WAD before it is used.
        main_unit = alias.casefold()
        try:
            validate_unit_name(
                main_unit,
                f"official champion id {champion_id} mainUnit",
            )
        except ChampionDataError as exc:
            raise ChampionIdentityError(str(exc)) from exc
        identities.append(
            ChampionIdentity(
                champion_id=champion_id,
                display_name=display_name,
                alias=alias,
                wad_base=alias,
                main_unit=main_unit,
            )
        )

    if not identities:
        raise ChampionIdentityError(
            "local LCU data contains no prime official champion identities"
        )
    identities.sort(key=lambda identity: identity.champion_id)
    return tuple(identities)


def find_champion_identity(
    identities: Iterable[ChampionIdentity],
    query: str,
) -> ChampionIdentity:
    wanted = normalize_identity_lookup(query)
    if not wanted:
        raise ChampionIdentityError("champion identity query is empty")
    matches = [
        identity
        for identity in identities
        if wanted
        in {
            normalize_identity_lookup(identity.display_name),
            normalize_identity_lookup(identity.alias),
            normalize_identity_lookup(identity.wad_base),
        }
    ]
    if not matches:
        raise ChampionIdentityError(
            f"official champion identity not found for {query!r}"
        )
    if len(matches) != 1:
        raise ChampionIdentityError(
            f"official champion identity is ambiguous for {query!r}: "
            f"{[identity.champion_id for identity in matches]}"
        )
    return matches[0]


def champion_skin_path(unit: str, skin_number: int) -> str:
    validate_unit_name(unit)
    if (
        isinstance(skin_number, bool)
        or not isinstance(skin_number, int)
        or not 0 <= skin_number <= 999
    ):
        raise ChampionDataError(
            f"skin number must be an integer from 0 through 999: {skin_number!r}"
        )
    return f"data/characters/{unit}/skins/skin{skin_number}.bin"


def wad_client_base_name(path: Path) -> str:
    name = path.name
    if not name.casefold().endswith(WAD_CLIENT_SUFFIX):
        raise ChampionIdentityError(
            f"official champion WAD must end in {WAD_CLIENT_SUFFIX}: {path}"
        )
    return name[: -len(WAD_CLIENT_SUFFIX)]


def validate_identity_wad(
    identity: ChampionIdentity,
    prepared: PreparedChampionWad,
) -> None:
    actual_base = wad_client_base_name(prepared.wad_path)
    if actual_base.casefold() != identity.wad_base.casefold():
        raise ChampionIdentityError(
            f"official champion id {identity.champion_id} expects "
            f"{identity.wad_base}{WAD_CLIENT_SUFFIX}, got {prepared.wad_path.name}"
        )
    main_path = champion_skin_path(identity.main_unit, 0)
    if not prepared.contains_path(main_path):
        raise ChampionIdentityError(
            f"official champion id {identity.champion_id} mainUnit "
            f"{identity.main_unit!r} has no skin0 in {prepared.wad_path.name}"
        )


def select_main_unit_directory(
    unit_directories: Iterable[Path],
    identity: ChampionIdentity,
) -> Path:
    directories = tuple(unit_directories)
    matches = [
        directory
        for directory in directories
        if directory.name.casefold() == identity.main_unit
    ]
    if len(matches) != 1:
        available = sorted(directory.name for directory in directories)
        raise ChampionIdentityError(
            f"official mainUnit {identity.main_unit!r} expected exactly once; "
            f"found {len(matches)} among {available}"
        )
    return matches[0]


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CandidateRegistryError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def parse_json_without_duplicate_keys(
    raw: bytes | str,
    path: Path,
) -> Any:
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CandidateRegistryError(
                f"failed decoding {path} as UTF-8: {exc}"
            ) from exc
    else:
        text = raw
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except json.JSONDecodeError as exc:
        raise CandidateRegistryError(f"failed parsing {path}: {exc}") from exc


def load_json_without_duplicate_keys(path: Path) -> Any:
    try:
        raw = path.read_bytes()
    except (OSError, UnicodeError) as exc:
        raise CandidateRegistryError(f"failed reading {path}: {exc}") from exc
    return parse_json_without_duplicate_keys(raw, path)


def _require_document_root(data: Any, path: Path) -> Mapping[str, Any]:
    if not isinstance(data, dict):
        raise CandidateRegistryError(f"{path} must contain a JSON object")
    if set(data) != {"schemaVersion", "champions"}:
        raise CandidateRegistryError(
            f"{path} must contain only schemaVersion and champions"
        )
    schema_version = data.get("schemaVersion")
    if (
        type(schema_version) is not int
        or schema_version != REGISTRY_SCHEMA_VERSION
    ):
        raise CandidateRegistryError(
            f"{path} uses unsupported schemaVersion "
            f"{schema_version!r}"
        )
    champions = data.get("champions")
    if not isinstance(champions, dict):
        raise CandidateRegistryError(f"{path} champions must be a JSON object")
    return champions


def _parse_champion_id_key(raw_id: object, path: Path) -> int:
    if (
        not isinstance(raw_id, str)
        or re.fullmatch(r"(?:0|[1-9]\d*)", raw_id) is None
    ):
        raise CandidateRegistryError(
            f"{path} has a non-canonical champion id key: {raw_id!r}"
        )
    return int(raw_id)


def _parse_sorted_units(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CandidateRegistryError(f"{label} must be a JSON array")
    try:
        units = tuple(
            validate_unit_name(unit, f"{label} item")
            for unit in value
        )
    except ChampionDataError as exc:
        raise CandidateRegistryError(str(exc)) from exc
    canonical = tuple(sorted(set(units)))
    if units != canonical:
        raise CandidateRegistryError(
            f"{label} must be sorted and contain no duplicates"
        )
    return units


def load_candidate_registry(
    path: Path,
    identities: Iterable[ChampionIdentity] | None = None,
    *,
    raw_bytes: bytes | None = None,
) -> CandidateRegistry:
    champions = _require_document_root(
        (
            load_json_without_duplicate_keys(path)
            if raw_bytes is None
            else parse_json_without_duplicate_keys(raw_bytes, path)
        ),
        path,
    )
    official = (
        None
        if identities is None
        else {identity.champion_id: identity for identity in identities}
    )
    entries: dict[int, CandidateRegistryEntry] = {}
    for raw_id, value in champions.items():
        champion_id = _parse_champion_id_key(raw_id, path)
        if not isinstance(value, dict):
            raise CandidateRegistryError(
                f"{path} champion {champion_id} must be a JSON object"
            )
        expected_fields = {
            "alias",
            "wadBase",
            "mainUnit",
            "auxiliaryUnits",
        }
        if set(value) != expected_fields:
            raise CandidateRegistryError(
                f"{path} champion {champion_id} must contain exactly "
                f"{sorted(expected_fields)}"
            )
        alias = value.get("alias")
        wad_base = value.get("wadBase")
        if not isinstance(alias, str) or not alias:
            raise CandidateRegistryError(
                f"{path} champion {champion_id} has invalid alias"
            )
        if not isinstance(wad_base, str) or not wad_base:
            raise CandidateRegistryError(
                f"{path} champion {champion_id} has invalid wadBase"
            )
        try:
            main_unit = validate_unit_name(
                value.get("mainUnit"),
                f"{path} champion {champion_id} mainUnit",
            )
        except ChampionDataError as exc:
            raise CandidateRegistryError(str(exc)) from exc
        auxiliary = _parse_sorted_units(
            value.get("auxiliaryUnits"),
            f"{path} champion {champion_id} auxiliaryUnits",
        )
        if main_unit in auxiliary:
            raise CandidateRegistryError(
                f"{path} champion {champion_id} lists mainUnit as auxiliary"
            )
        entry = CandidateRegistryEntry(
            champion_id=champion_id,
            alias=alias,
            wad_base=wad_base,
            main_unit=main_unit,
            auxiliary_units=auxiliary,
        )
        if official is not None:
            identity = official.get(champion_id)
            if identity is None:
                raise CandidateRegistryError(
                    f"{path} champion {champion_id} is not in the prime "
                    "official LCU roster"
                )
            actual = (entry.alias, entry.wad_base, entry.main_unit)
            expected = (
                identity.alias,
                identity.wad_base,
                identity.main_unit,
            )
            if actual != expected:
                raise CandidateRegistryError(
                    f"{path} champion {champion_id} identity differs from "
                    f"local LCU data: {actual!r} != {expected!r}"
                )
        entries[champion_id] = entry
    return CandidateRegistry(entries=entries)


def candidate_units_for(
    identity: ChampionIdentity,
    registry: CandidateRegistry,
) -> tuple[str, ...]:
    entry = registry.require(identity.champion_id)
    actual = (entry.alias, entry.wad_base, entry.main_unit)
    expected = (identity.alias, identity.wad_base, identity.main_unit)
    if actual != expected:
        raise CandidateRegistryError(
            f"candidate registry identity for champion {identity.champion_id} "
            f"does not match local LCU identity: {actual!r} != {expected!r}"
        )
    units = {identity.main_unit} | set(entry.auxiliary_units)
    return tuple(sorted(units))


def derive_hash_skin_candidates(
    identity: ChampionIdentity,
    available_path_hashes: Iterable[int],
    index: HashSkinIndex,
) -> HashSkinCandidateSet:
    """Derive one champion's units from current dictionary/WAD intersection."""

    available = frozenset(available_path_hashes)
    if not available:
        raise CandidateRegistryError(
            f"champion {identity.champion_id} WAD has no path hashes"
        )
    if any(
        isinstance(path_hash, bool)
        or not isinstance(path_hash, int)
        or not 0 <= path_hash <= 0xFFFFFFFFFFFFFFFF
        for path_hash in available
    ):
        raise CandidateRegistryError("WAD path hashes must be unsigned 64-bit values")

    matched_records: list[HashSkinRecord] = []
    for path_hash in available:
        record = index.record_for_hash(path_hash)
        if record is not None:
            matched_records.append(record)
    matched = tuple(sorted(matched_records))
    records_by_unit: dict[str, list[HashSkinRecord]] = {}
    for record in matched:
        records_by_unit.setdefault(record.unit, []).append(record)

    main_skin0 = index.record_for(identity.main_unit, 0)
    if main_skin0 is None or main_skin0.path_hash not in available:
        raise CandidateRegistryError(
            f"newest dictionary and {identity.wad_base}.wad.client do not "
            f"jointly prove mainUnit {identity.main_unit!r} skin0"
        )

    candidates = tuple(sorted(records_by_unit))
    if identity.main_unit not in candidates:
        raise CandidateRegistryError(
            f"dictionary candidates do not contain mainUnit "
            f"{identity.main_unit!r}"
        )
    candidate_units = set(candidates)
    selected_records = tuple(
        record
        for record in matched
        if record.unit in candidate_units
    )
    digest = hash_skin_candidate_digest(
        identity.champion_id,
        candidates,
        selected_records,
    )
    return HashSkinCandidateSet(
        champion_id=identity.champion_id,
        candidates=candidates,
        matched_records=selected_records,
        digest=digest,
        source_sha256=index.source_sha256,
    )


def hash_skin_candidate_digest(
    champion_id: int,
    candidates: tuple[str, ...],
    records: tuple[HashSkinRecord, ...],
) -> str:
    payload = {
        "schemaVersion": 1,
        "championId": champion_id,
        "candidates": list(candidates),
        "records": [
            [record.unit, record.skin_number, f"{record.path_hash:016x}"]
            for record in records
        ],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def candidate_registry_from_hash_candidates(
    identity: ChampionIdentity,
    candidate_set: HashSkinCandidateSet,
) -> CandidateRegistry:
    if candidate_set.champion_id != identity.champion_id:
        raise CandidateRegistryError(
            "dictionary candidate set belongs to a different champion"
        )
    if identity.main_unit not in candidate_set.candidates:
        raise CandidateRegistryError(
            f"dictionary candidate set has no mainUnit {identity.main_unit!r}"
        )
    return CandidateRegistry(
        entries={
            identity.champion_id: CandidateRegistryEntry(
                champion_id=identity.champion_id,
                alias=identity.alias,
                wad_base=identity.wad_base,
                main_unit=identity.main_unit,
                auxiliary_units=tuple(
                    unit
                    for unit in candidate_set.candidates
                    if unit != identity.main_unit
                ),
            )
        }
    )


def _validate_layout_request(
    identity: ChampionIdentity,
    prepared: PreparedChampionWad,
    skin_numbers: Iterable[int],
    *,
    validate_wad_identity: bool = True,
) -> tuple[int, ...]:
    if validate_wad_identity:
        validate_identity_wad(identity, prepared)
    selected = tuple(skin_numbers)
    if any(
        isinstance(skin, bool)
        or not isinstance(skin, int)
        or not 1 <= skin <= 999
        for skin in selected
    ):
        raise ChampionLayoutError(
            f"target skin numbers must be integers from 1 through 999: {selected!r}"
        )
    if selected != tuple(sorted(set(selected))):
        raise ChampionLayoutError(
            "target skin numbers must be sorted and contain no duplicates"
        )
    if not selected:
        raise ChampionLayoutError("at least one target skin number is required")
    return selected


def _assemble_champion_layout(
    identity: ChampionIdentity,
    prepared: PreparedChampionWad,
    selected: tuple[int, ...],
    candidates: tuple[str, ...],
    resolve_chunk: Callable[
        [str, int],
        tuple[str | None, WadChunk | None],
    ],
) -> ChampionLayout:
    layouts: list[SkinLayout] = []
    for skin_number in selected:
        paired: list[LayoutUnit] = []
        base_only: list[LayoutUnit] = []
        target_only: list[LayoutUnit] = []
        absent: list[str] = []
        for unit in candidates:
            base_source_path, base_chunk = resolve_chunk(unit, 0)
            target_source_path, target_chunk = resolve_chunk(
                unit,
                skin_number,
            )
            if (
                (base_source_path is None) != (base_chunk is None)
                or (target_source_path is None) != (target_chunk is None)
            ):
                raise ChampionLayoutError(
                    f"chunk resolver returned inconsistent path presence for "
                    f"{unit!r} skin{skin_number}"
                )
            if base_chunk is None and target_chunk is None:
                absent.append(unit)
                continue
            state = LayoutUnit(
                unit=unit,
                base_path=base_source_path,
                target_path=target_source_path,
                base_chunk=(
                    None
                    if base_chunk is None
                    else ChunkIdentity.from_chunk(base_chunk)
                ),
                target_chunk=(
                    None
                    if target_chunk is None
                    else ChunkIdentity.from_chunk(target_chunk)
                ),
            )
            if base_chunk is not None and target_chunk is not None:
                paired.append(state)
            elif base_chunk is not None:
                base_only.append(state)
            else:
                target_only.append(state)

        paired_units = {state.unit for state in paired}
        if identity.main_unit not in paired_units:
            raise ChampionLayoutError(
                f"mainUnit {identity.main_unit!r} is not paired for "
                f"skin{skin_number} in {prepared.wad_path.name}"
            )
        layouts.append(
            SkinLayout(
                skin_number=skin_number,
                paired=tuple(paired),
                base_only=tuple(base_only),
                target_only=tuple(target_only),
                absent_candidates=tuple(absent),
            )
        )

    return ChampionLayout(
        identity=identity,
        wad_path=prepared.wad_path.resolve(),
        wad_file_identity=prepared.file_identity,
        wad_version=str(prepared.version),
        toc_digest=prepared.toc_digest,
        candidates=candidates,
        skins=tuple(layouts),
    )


def build_champion_layout(
    identity: ChampionIdentity,
    prepared: PreparedChampionWad,
    skin_numbers: Iterable[int],
    registry: CandidateRegistry,
) -> ChampionLayout:
    selected = _validate_layout_request(
        identity,
        prepared,
        skin_numbers,
    )

    candidates = candidate_units_for(identity, registry)
    paths = [
        champion_skin_path(unit, skin_number)
        for unit in candidates
        for skin_number in (0, *selected)
    ]
    inspected = prepared.inspect_paths(paths)

    def resolve_chunk(
        unit: str,
        skin_number: int,
    ) -> tuple[str | None, WadChunk | None]:
        normalized_path = champion_skin_path(unit, skin_number)
        chunk = inspected[normalized_path]
        return (
            normalized_path if chunk is not None else None,
            chunk,
        )

    return _assemble_champion_layout(
        identity,
        prepared,
        selected,
        candidates,
        resolve_chunk,
    )


def build_hash_skin_champion_layout(
    identity: ChampionIdentity,
    prepared: PreparedChampionWad,
    skin_numbers: Iterable[int],
    candidate_set: HashSkinCandidateSet,
) -> ChampionLayout:
    """Build Layout from dictionary-bound hashes without recomputing paths."""

    selected = _validate_layout_request(
        identity,
        prepared,
        skin_numbers,
        validate_wad_identity=False,
    )
    actual_base = wad_client_base_name(prepared.wad_path)
    if actual_base.casefold() != identity.wad_base.casefold():
        raise ChampionIdentityError(
            f"official champion id {identity.champion_id} expects "
            f"{identity.wad_base}{WAD_CLIENT_SUFFIX}, got "
            f"{prepared.wad_path.name}"
        )
    if candidate_set.champion_id != identity.champion_id:
        raise ChampionLayoutError(
            "dictionary candidate set belongs to a different champion"
        )
    candidates = candidate_set.candidates
    if identity.main_unit not in candidates:
        raise ChampionLayoutError(
            f"dictionary candidate set has no mainUnit "
            f"{identity.main_unit!r}"
        )
    if not any(
        record.unit == identity.main_unit and record.skin_number == 0
        for record in candidate_set.matched_records
    ):
        raise ChampionIdentityError(
            f"official champion id {identity.champion_id} mainUnit "
            f"{identity.main_unit!r} has no dictionary-bound skin0 in "
            f"{prepared.wad_path.name}"
        )

    records_by_key = {
        (record.unit, record.skin_number): record
        for record in candidate_set.matched_records
    }
    selected_records = tuple(
        record
        for record in candidate_set.matched_records
        if record.skin_number == 0 or record.skin_number in selected
    )
    inspected = prepared.inspect_hashes(
        record.path_hash
        for record in selected_records
    )
    for record in selected_records:
        chunk = inspected[record.path_hash]
        if chunk is None:
            raise ChampionLayoutError(
                f"dictionary-proven path disappeared from "
                f"{prepared.wad_path.name}: {record.path}"
            )
        if chunk.path_hash != record.path_hash:
            raise ChampionLayoutError(
                f"dictionary hash identity differs for {record.path}"
            )

    def resolve_chunk(
        unit: str,
        skin_number: int,
    ) -> tuple[str | None, WadChunk | None]:
        record = records_by_key.get((unit, skin_number))
        if record is None:
            return None, None
        chunk = inspected.get(record.path_hash)
        if chunk is None:
            raise ChampionLayoutError(
                f"dictionary record was not inspected for {record.path}"
            )
        return record.path, chunk

    return _assemble_champion_layout(
        identity,
        prepared,
        selected,
        candidates,
        resolve_chunk,
    )


def _serialized_chunk(chunk: ChunkIdentity | None) -> dict[str, Any] | None:
    if chunk is None:
        return None
    return {
        "pathHash": f"{chunk.path_hash:016x}",
        "compressedSize": chunk.compressed_size,
        "decompressedSize": chunk.decompressed_size,
        "compressionType": chunk.compression_type,
        "subchunkCount": chunk.subchunk_count,
        "subchunkIndex": chunk.subchunk_index,
        "duplicated": chunk.duplicated,
        "checksum": (
            None if chunk.checksum is None else f"{chunk.checksum:016x}"
        ),
        "checksumKind": chunk.checksum_kind,
    }


def _serialized_unit(state: LayoutUnit) -> dict[str, Any]:
    return {
        "unit": state.unit,
        "basePath": state.base_path,
        "targetPath": state.target_path,
        "baseChunk": _serialized_chunk(state.base_chunk),
        "targetChunk": _serialized_chunk(state.target_chunk),
    }


def serialize_skin_layout(skin: SkinLayout) -> dict[str, Any]:
    return {
        "skinNumber": skin.skin_number,
        "paired": [
            _serialized_unit(state)
            for state in skin.paired
        ],
        "baseOnly": [
            _serialized_unit(state)
            for state in skin.base_only
        ],
        "targetOnly": [
            _serialized_unit(state)
            for state in skin.target_only
        ],
        "absentCandidates": list(skin.absent_candidates),
    }


def serialize_champion_layout(layout: ChampionLayout) -> dict[str, Any]:
    identity = layout.identity
    wad_identity = layout.wad_file_identity
    return {
        "championId": identity.champion_id,
        "displayName": identity.display_name,
        "alias": identity.alias,
        "wadBase": identity.wad_base,
        "mainUnit": identity.main_unit,
        "wad": {
            "path": str(layout.wad_path),
            "version": layout.wad_version,
            "size": wad_identity.size,
            "modifiedNs": wad_identity.mtime_ns,
            "tocDigest": layout.toc_digest,
        },
        "candidates": list(layout.candidates),
        "skins": [
            serialize_skin_layout(skin)
            for skin in layout.skins
        ],
    }


def _parse_serialized_hex(
    value: object,
    label: str,
    *,
    optional: bool = False,
) -> int | None:
    if optional and value is None:
        return None
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-f]{16}", value) is None
    ):
        raise ChampionLayoutError(
            f"{label} must be a 16-character lowercase hex string"
        )
    return int(value, 16)


def _parse_serialized_chunk(
    value: object,
    label: str,
) -> ChunkIdentity | None:
    if value is None:
        return None
    expected_fields = {
        "pathHash",
        "compressedSize",
        "decompressedSize",
        "compressionType",
        "subchunkCount",
        "subchunkIndex",
        "duplicated",
        "checksum",
        "checksumKind",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ChampionLayoutError(
            f"{label} has an invalid serialized chunk schema"
        )
    integer_fields = (
        "compressedSize",
        "decompressedSize",
        "compressionType",
        "subchunkCount",
        "subchunkIndex",
    )
    for field_name in integer_fields:
        field_value = value[field_name]
        if (
            isinstance(field_value, bool)
            or not isinstance(field_value, int)
            or field_value < 0
        ):
            raise ChampionLayoutError(
                f"{label}.{field_name} must be a non-negative integer"
            )
    if not isinstance(value["duplicated"], bool):
        raise ChampionLayoutError(
            f"{label}.duplicated must be a boolean"
        )
    if not isinstance(value["checksumKind"], str):
        raise ChampionLayoutError(
            f"{label}.checksumKind must be a string"
        )
    path_hash = _parse_serialized_hex(
        value["pathHash"],
        f"{label}.pathHash",
    )
    checksum = _parse_serialized_hex(
        value["checksum"],
        f"{label}.checksum",
        optional=True,
    )
    assert path_hash is not None
    return ChunkIdentity(
        path_hash=path_hash,
        compressed_size=value["compressedSize"],
        decompressed_size=value["decompressedSize"],
        compression_type=value["compressionType"],
        subchunk_count=value["subchunkCount"],
        subchunk_index=value["subchunkIndex"],
        duplicated=value["duplicated"],
        checksum=checksum,
        checksum_kind=value["checksumKind"],
    )


def _parse_serialized_unit(
    value: object,
    *,
    skin_number: int,
    candidates: tuple[str, ...],
    group: str,
) -> LayoutUnit:
    expected_fields = {
        "unit",
        "basePath",
        "targetPath",
        "baseChunk",
        "targetChunk",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ChampionLayoutError(
            f"skin{skin_number} {group} unit has an invalid schema"
        )
    try:
        unit = validate_unit_name(
            value["unit"],
            f"skin{skin_number} {group} unit",
        )
    except ChampionDataError as exc:
        raise ChampionLayoutError(str(exc)) from exc
    if unit not in candidates:
        raise ChampionLayoutError(
            f"skin{skin_number} cached unit {unit!r} is not a candidate"
        )
    base_path = value["basePath"]
    target_path = value["targetPath"]
    if base_path is not None and not isinstance(base_path, str):
        raise ChampionLayoutError(
            f"skin{skin_number} {unit} basePath must be a string or null"
        )
    if target_path is not None and not isinstance(target_path, str):
        raise ChampionLayoutError(
            f"skin{skin_number} {unit} targetPath must be a string or null"
        )
    base_chunk = _parse_serialized_chunk(
        value["baseChunk"],
        f"skin{skin_number} {unit} baseChunk",
    )
    target_chunk = _parse_serialized_chunk(
        value["targetChunk"],
        f"skin{skin_number} {unit} targetChunk",
    )
    expected_base = champion_skin_path(unit, 0)
    expected_target = champion_skin_path(unit, skin_number)
    if base_path is not None and base_path != expected_base:
        raise ChampionLayoutError(
            f"skin{skin_number} {unit} has an unexpected base path"
        )
    if target_path is not None and target_path != expected_target:
        raise ChampionLayoutError(
            f"skin{skin_number} {unit} has an unexpected target path"
        )
    if (base_path is None) != (base_chunk is None):
        raise ChampionLayoutError(
            f"skin{skin_number} {unit} base path/chunk presence differs"
        )
    if (target_path is None) != (target_chunk is None):
        raise ChampionLayoutError(
            f"skin{skin_number} {unit} target path/chunk presence differs"
        )
    if (
        base_chunk is not None
        and base_chunk.path_hash != wad_path_hash(expected_base)
    ):
        raise ChampionLayoutError(
            f"skin{skin_number} {unit} base path hash differs"
        )
    if (
        target_chunk is not None
        and target_chunk.path_hash != wad_path_hash(expected_target)
    ):
        raise ChampionLayoutError(
            f"skin{skin_number} {unit} target path hash differs"
        )
    expected_presence = {
        "paired": (True, True),
        "baseOnly": (True, False),
        "targetOnly": (False, True),
    }[group]
    if (
        (base_path is not None, target_path is not None)
        != expected_presence
    ):
        raise ChampionLayoutError(
            f"skin{skin_number} {unit} does not match {group} presence"
        )
    return LayoutUnit(
        unit=unit,
        base_path=base_path,
        target_path=target_path,
        base_chunk=base_chunk,
        target_chunk=target_chunk,
    )


def deserialize_skin_layout(
    value: object,
    *,
    skin_number: int,
    candidates: tuple[str, ...],
    main_unit: str,
) -> SkinLayout:
    expected_fields = {
        "skinNumber",
        "paired",
        "baseOnly",
        "targetOnly",
        "absentCandidates",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ChampionLayoutError(
            f"skin{skin_number} cached Layout has an invalid schema"
        )
    if value["skinNumber"] != skin_number:
        raise ChampionLayoutError(
            f"cached Layout skin number differs from skin{skin_number}"
        )
    parsed_groups: dict[str, tuple[LayoutUnit, ...]] = {}
    observed: list[str] = []
    for group in ("paired", "baseOnly", "targetOnly"):
        rows = value[group]
        if not isinstance(rows, list):
            raise ChampionLayoutError(
                f"skin{skin_number} {group} must be an array"
            )
        parsed = tuple(
            _parse_serialized_unit(
                row,
                skin_number=skin_number,
                candidates=candidates,
                group=group,
            )
            for row in rows
        )
        units = tuple(state.unit for state in parsed)
        if units != tuple(sorted(set(units))):
            raise ChampionLayoutError(
                f"skin{skin_number} {group} units are not canonical"
            )
        parsed_groups[group] = parsed
        observed.extend(units)

    absent_value = value["absentCandidates"]
    if (
        not isinstance(absent_value, list)
        or any(not isinstance(unit, str) for unit in absent_value)
    ):
        raise ChampionLayoutError(
            f"skin{skin_number} absentCandidates must be a string array"
        )
    absent = tuple(absent_value)
    if absent != tuple(sorted(set(absent))):
        raise ChampionLayoutError(
            f"skin{skin_number} absentCandidates are not canonical"
        )
    observed.extend(absent)
    if tuple(sorted(observed)) != candidates:
        raise ChampionLayoutError(
            f"skin{skin_number} cached Layout does not partition candidates"
        )
    if main_unit not in {
        state.unit
        for state in parsed_groups["paired"]
    }:
        raise ChampionLayoutError(
            f"mainUnit {main_unit!r} is not paired for skin{skin_number}"
        )
    return SkinLayout(
        skin_number=skin_number,
        paired=parsed_groups["paired"],
        base_only=parsed_groups["baseOnly"],
        target_only=parsed_groups["targetOnly"],
        absent_candidates=absent,
    )


def serialize_required_chunk_identity(
    identity: RequiredChunkIdentity,
) -> dict[str, Any]:
    serialized = _serialized_chunk(identity.chunk)
    assert serialized is not None
    return {
        "path": identity.normalized_path,
        **serialized,
        "contentSha256": identity.content_sha256,
    }


def _assert_layout_is_current(
    layout: ChampionLayout,
    prepared: PreparedChampionWad,
) -> None:
    actual = prepared.file_identity
    if actual != layout.wad_file_identity:
        raise WadChangedDuringRead(
            prepared.wad_path,
            layout.wad_file_identity,
            actual,
        )
    if prepared.toc_digest != layout.toc_digest:
        raise ChampionLayoutError(
            f"prepared TOC digest changed for {prepared.wad_path.name}"
        )


def ensure_required_chunk_identities(
    layout: ChampionLayout,
    prepared: PreparedChampionWad,
) -> Mapping[str, RequiredChunkIdentity]:
    """Bind every paired output path to safe metadata and, when needed, bytes.

    A current non-zero XXH3 entry checksum is sufficient together with the
    WAD version and canonical TOC digest carried by ``ChampionLayout``. Older
    checksum semantics and zero checksums are completed with a decompressed
    content SHA-256. Those reads populate ``PreparedChampionWad``'s decoded
    cache so Direct Prepare does not decompress the same chunk again.
    """

    if layout.wad_path.resolve() != prepared.wad_path.resolve():
        raise ChampionLayoutError(
            "ChampionLayout and PreparedChampionWad refer to different WADs"
        )
    _assert_layout_is_current(layout, prepared)

    expected_by_path: dict[str, ChunkIdentity] = {}
    path_hashes: dict[str, int] = {}
    for skin in layout.skins:
        for state in skin.paired:
            pairs = (
                (state.base_path, state.base_chunk),
                (state.target_path, state.target_chunk),
            )
            for normalized_path, expected in pairs:
                if normalized_path is None or expected is None:
                    raise ChampionLayoutError(
                        f"paired unit {state.unit!r} has an incomplete chunk identity"
                    )
                previous = expected_by_path.setdefault(normalized_path, expected)
                if previous != expected:
                    raise ChampionLayoutError(
                        f"required path {normalized_path!r} has conflicting identities"
                    )
                previous_hash = path_hashes.setdefault(
                    normalized_path,
                    expected.path_hash,
                )
                if previous_hash != expected.path_hash:
                    raise ChampionLayoutError(
                        f"required path {normalized_path!r} has conflicting hashes"
                    )

    identities = ensure_hash_chunk_identities(
        prepared,
        path_hashes,
        expected_chunks=expected_by_path,
    )
    _assert_layout_is_current(layout, prepared)
    return identities


def ensure_hash_chunk_identities(
    prepared: PreparedChampionWad,
    path_hashes: Mapping[str, int],
    *,
    expected_chunks: Mapping[str, ChunkIdentity] | None = None,
) -> Mapping[str, RequiredChunkIdentity]:
    """Build complete identities using already validated dictionary hashes."""

    ordered_paths = tuple(sorted(path_hashes))
    if not ordered_paths:
        return {}
    hashes = tuple(path_hashes[path] for path in ordered_paths)
    if len(set(hashes)) != len(hashes):
        raise ChampionLayoutError(
            "multiple required paths resolve to the same dictionary hash"
        )
    if expected_chunks is not None and set(expected_chunks) != set(ordered_paths):
        raise ChampionLayoutError(
            "expected chunk identity paths differ from requested paths"
        )

    inspected = prepared.inspect_hashes(hashes)
    actual_by_path: dict[str, WadChunk] = {}
    content_hashes: list[int] = []
    for normalized_path in ordered_paths:
        path_hash = path_hashes[normalized_path]
        chunk = inspected[path_hash]
        if chunk is None:
            raise ChampionLayoutError(
                f"required dictionary hash disappeared from WAD: "
                f"{normalized_path}"
            )
        actual = ChunkIdentity.from_chunk(chunk)
        if (
            expected_chunks is not None
            and actual != expected_chunks[normalized_path]
        ):
            raise ChampionLayoutError(
                f"required chunk identity changed for {normalized_path}"
            )
        preflight_wad_chunk(
            chunk,
            wad_path=prepared.wad_path,
            limits=prepared.limits,
        )
        actual_by_path[normalized_path] = chunk
        if not chunk.has_reliable_checksum:
            content_hashes.append(path_hash)

    content = (
        prepared.read_hashes(content_hashes, validate_bin=True)
        if content_hashes
        else {}
    )
    return {
        normalized_path: RequiredChunkIdentity(
            normalized_path=normalized_path,
            chunk=ChunkIdentity.from_chunk(chunk),
            content_sha256=(
                hashlib.sha256(content[path_hashes[normalized_path]]).hexdigest()
                if path_hashes[normalized_path] in content
                else None
            ),
        )
        for normalized_path, chunk in sorted(actual_by_path.items())
    }


def candidate_registry_document(
    identities: Iterable[ChampionIdentity],
    auxiliary_units: Mapping[int, Iterable[str]],
) -> dict[str, Any]:
    entries: list[CandidateRegistryEntry] = []
    for identity in sorted(identities, key=lambda item: item.champion_id):
        units = tuple(sorted(set(auxiliary_units.get(identity.champion_id, ()))))
        for unit in units:
            validate_unit_name(
                unit,
                f"champion {identity.champion_id} auxiliary unit",
            )
        if identity.main_unit in units:
            raise CandidateRegistryError(
                f"champion {identity.champion_id} auxiliary units contain "
                f"mainUnit {identity.main_unit!r}"
            )
        entries.append(
            CandidateRegistryEntry(
                champion_id=identity.champion_id,
                alias=identity.alias,
                wad_base=identity.wad_base,
                main_unit=identity.main_unit,
                auxiliary_units=units,
            )
        )
    return candidate_registry_entries_document(entries)


def candidate_registry_entries_document(
    entries: Iterable[CandidateRegistryEntry],
) -> dict[str, Any]:
    champions: dict[str, Any] = {}
    seen: set[int] = set()
    for entry in sorted(entries, key=lambda item: item.champion_id):
        if entry.champion_id in seen:
            raise CandidateRegistryError(
                f"duplicate candidate registry champion id {entry.champion_id}"
            )
        seen.add(entry.champion_id)
        validate_unit_name(
            entry.main_unit,
            f"champion {entry.champion_id} mainUnit",
        )
        auxiliary = tuple(sorted(set(entry.auxiliary_units)))
        if auxiliary != entry.auxiliary_units:
            raise CandidateRegistryError(
                f"champion {entry.champion_id} auxiliary units are not canonical"
            )
        for unit in auxiliary:
            validate_unit_name(
                unit,
                f"champion {entry.champion_id} auxiliary unit",
            )
        if entry.main_unit in auxiliary:
            raise CandidateRegistryError(
                f"champion {entry.champion_id} auxiliary units contain "
                f"mainUnit {entry.main_unit!r}"
            )
        champions[str(entry.champion_id)] = {
            "alias": entry.alias,
            "wadBase": entry.wad_base,
            "mainUnit": entry.main_unit,
            "auxiliaryUnits": list(auxiliary),
        }
    return {
        "schemaVersion": REGISTRY_SCHEMA_VERSION,
        "champions": champions,
    }
