"""Identity-bound base skin0 parsing cache primitives."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .persistent_cache import (
    PersistentCacheKey,
    PersistentJsonCache,
    canonical_json_bytes as persistent_canonical_json_bytes,
)


BASE_CACHE_KEY_SCHEMA_VERSION = 1
BASE_SNAPSHOT_SCHEMA_VERSION = 1
BASE_CACHE_NAMESPACE = "base-parse"
_SNAPSHOT_FIELDS = {
    "skinEntryKey",
    "championSkinName",
    "resourceResolver",
    "resolverEntryKey",
}


class BaseCacheError(ValueError):
    """A base cache identity or immutable snapshot is invalid."""


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise BaseCacheError(f"value is not canonical JSON: {exc}") from exc
    return text.encode("utf-8")


def _stat_ns(
    stat_result: os.stat_result,
    ns_name: str,
    seconds_name: str,
) -> int:
    value = getattr(stat_result, ns_name, None)
    if value is not None:
        return int(value)
    return int(getattr(stat_result, seconds_name) * 1_000_000_000)


def _stat_key(path: Path) -> tuple[int, int, int, int, int]:
    stat_result = path.stat()
    return (
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        int(stat_result.st_size),
        _stat_ns(stat_result, "st_mtime_ns", "st_mtime"),
        _stat_ns(stat_result, "st_ctime_ns", "st_ctime"),
    )


@dataclass(frozen=True)
class ToolIdentity:
    path: str
    size: int
    modified_ns: int
    changed_ns: int
    sha256: str

    def as_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "modifiedNs": self.modified_ns,
            "changedNs": self.changed_ns,
            "sha256": self.sha256,
        }


def capture_tool_identity(path: Path) -> tuple[tuple[int, int, int, int, int], ToolIdentity]:
    resolved = path.resolve()
    before = _stat_key(resolved)
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    after = _stat_key(resolved)
    if after != before:
        raise BaseCacheError(f"tool changed while hashing: {resolved}")
    return (
        after,
        ToolIdentity(
            path=str(resolved),
            size=after[2],
            modified_ns=after[3],
            changed_ns=after[4],
            sha256=digest.hexdigest(),
        ),
    )


@dataclass(frozen=True)
class BaseRebaseSnapshot:
    """Canonical immutable projection of fields copied from base skin0."""

    payload: bytes

    @classmethod
    def from_values(
        cls,
        *,
        skin_entry_key: Any,
        champion_skin_name: Any,
        resource_resolver: Any,
        resolver_entry_key: Any,
    ) -> BaseRebaseSnapshot:
        return cls(
            _canonical_json_bytes(
                {
                    "schemaVersion": BASE_SNAPSHOT_SCHEMA_VERSION,
                    "values": {
                        "skinEntryKey": skin_entry_key,
                        "championSkinName": champion_skin_name,
                        "resourceResolver": resource_resolver,
                        "resolverEntryKey": resolver_entry_key,
                    },
                }
            )
        )

    @classmethod
    def from_payload(cls, payload: bytes) -> BaseRebaseSnapshot:
        snapshot = cls(bytes(payload))
        snapshot.values()
        if snapshot.payload != _canonical_json_bytes(
            {
                "schemaVersion": BASE_SNAPSHOT_SCHEMA_VERSION,
                "values": snapshot.values(),
            }
        ):
            raise BaseCacheError("base snapshot is not canonically encoded")
        return snapshot

    def values(self) -> dict[str, Any]:
        try:
            decoded = json.loads(self.payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BaseCacheError(f"invalid base snapshot JSON: {exc}") from exc
        if (
            not isinstance(decoded, dict)
            or decoded.get("schemaVersion") != BASE_SNAPSHOT_SCHEMA_VERSION
            or not isinstance(decoded.get("values"), dict)
            or set(decoded["values"]) != _SNAPSHOT_FIELDS
        ):
            raise BaseCacheError("base snapshot has an invalid schema")
        # json.loads returns a fresh object on every call, so callers cannot
        # mutate the immutable bytes retained by the cache.
        return decoded["values"]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


@dataclass(frozen=True)
class BaseParseKey:
    digest: str
    manifest: bytes


class ProcessBaseParseCache:
    """One-process successful base parse cache with strong input identity."""

    def __init__(
        self,
        tool_path: Path,
        *,
        rebase_schema: int,
        parser_schema: int,
        persistent_cache: PersistentJsonCache | None = None,
    ) -> None:
        self.tool_path = tool_path
        self.rebase_schema = rebase_schema
        self.parser_schema = parser_schema
        self.persistent_cache = persistent_cache
        self._tool_stat: tuple[int, int, int, int, int] | None = None
        self._tool_identity: ToolIdentity | None = None
        self._entries: dict[str, BaseRebaseSnapshot] = {}
        self._persistent_counters = {
            "hits": 0,
            "misses": 0,
            "corruptions": 0,
            "stores": 0,
            "storeFailures": 0,
        }

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    def tool_identity(self) -> ToolIdentity:
        resolved = self.tool_path.resolve()
        current = _stat_key(resolved)
        if self._tool_stat != current or self._tool_identity is None:
            self._tool_stat, self._tool_identity = capture_tool_identity(resolved)
        return self._tool_identity

    def build_key(
        self,
        context: Mapping[str, Any],
        base_payload: bytes,
    ) -> BaseParseKey:
        manifest = _canonical_json_bytes(
            {
                "schemaVersion": BASE_CACHE_KEY_SCHEMA_VERSION,
                "rebaseSchema": self.rebase_schema,
                "parserSchema": self.parser_schema,
                "context": dict(context),
                "base": {
                    "size": len(base_payload),
                    "sha256": hashlib.sha256(base_payload).hexdigest(),
                },
                "ritobin": self.tool_identity().as_json(),
            }
        )
        return BaseParseKey(
            digest=hashlib.sha256(manifest).hexdigest(),
            manifest=manifest,
        )

    def get(self, key: BaseParseKey) -> BaseRebaseSnapshot | None:
        snapshot, _tier = self.get_with_tier(key)
        return snapshot

    def get_with_tier(
        self,
        key: BaseParseKey,
    ) -> tuple[BaseRebaseSnapshot | None, str]:
        memory = self._entries.get(key.digest)
        if memory is not None:
            return memory, "memory"
        if self.persistent_cache is None:
            return None, "miss"

        persistent_key = PersistentCacheKey.from_canonical_bytes(
            key.digest,
            key.manifest,
        )
        lookup = self.persistent_cache.lookup(
            BASE_CACHE_NAMESPACE,
            persistent_key,
        )
        if not lookup.hit:
            if lookup.status == "corrupt":
                self._persistent_counters["corruptions"] += 1
            else:
                self._persistent_counters["misses"] += 1
            return None, lookup.status

        try:
            payload = persistent_canonical_json_bytes(lookup.payload)
            snapshot = BaseRebaseSnapshot.from_payload(payload)
        except (BaseCacheError, ValueError):
            self.persistent_cache.invalidate(
                BASE_CACHE_NAMESPACE,
                persistent_key,
            )
            self._persistent_counters["corruptions"] += 1
            return None, "corrupt"
        self._entries[key.digest] = snapshot
        self._persistent_counters["hits"] += 1
        return snapshot, "persistent"

    def put(
        self,
        key: BaseParseKey,
        snapshot: BaseRebaseSnapshot,
    ) -> bool:
        existing = self._entries.get(key.digest)
        if existing is not None and existing != snapshot:
            raise BaseCacheError(
                f"base parse key {key.digest} produced different snapshots"
            )
        self._entries[key.digest] = snapshot
        persistent_stored = False
        if self.persistent_cache is not None:
            persistent_key = PersistentCacheKey.from_canonical_bytes(
                key.digest,
                key.manifest,
            )
            payload = json.loads(snapshot.payload.decode("utf-8"))
            persistent_stored = self.persistent_cache.store(
                BASE_CACHE_NAMESPACE,
                persistent_key,
                payload,
            )
            if persistent_stored:
                self._persistent_counters["stores"] += 1
            else:
                self._persistent_counters["storeFailures"] += 1
        return persistent_stored

    def fact(self) -> dict[str, Any]:
        return {
            "keySchemaVersion": BASE_CACHE_KEY_SCHEMA_VERSION,
            "snapshotSchemaVersion": BASE_SNAPSHOT_SCHEMA_VERSION,
            "rebaseSchema": self.rebase_schema,
            "parserSchema": self.parser_schema,
            "entries": self.entry_count,
            "ritobin": self.tool_identity().as_json(),
            "persistent": {
                "enabled": self.persistent_cache is not None,
                **self._persistent_counters,
            },
        }
