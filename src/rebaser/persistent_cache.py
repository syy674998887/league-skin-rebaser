"""Crash-safe content-addressed JSON cache primitives.

The cache is deliberately advisory: an unavailable, stale, or corrupt entry is
always a miss. Callers remain responsible for validating domain payloads before
using them and may invalidate an entry when that validation fails.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CACHE_ENVELOPE_SCHEMA_VERSION = 1
DEFAULT_CACHE_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_CACHE_MAX_ENTRY_BYTES = 64 * 1024 * 1024
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_NAMESPACE_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}\Z")


class PersistentCacheValueError(ValueError):
    """A caller supplied a non-canonical key or unsupported JSON value."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PersistentCacheValueError(
            f"value is not canonical JSON: {exc}"
        ) from exc
    return encoded.encode("utf-8")


@dataclass(frozen=True)
class PersistentCacheKey:
    digest: str
    manifest: bytes

    @classmethod
    def from_manifest(cls, manifest: Any) -> PersistentCacheKey:
        payload = canonical_json_bytes(manifest)
        return cls(
            digest=hashlib.sha256(payload).hexdigest(),
            manifest=payload,
        )

    @classmethod
    def from_canonical_bytes(
        cls,
        digest: str,
        manifest: bytes,
    ) -> PersistentCacheKey:
        payload = bytes(manifest)
        if _DIGEST_RE.fullmatch(digest) is None:
            raise PersistentCacheValueError(
                f"invalid cache key digest: {digest!r}"
            )
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PersistentCacheValueError(
                f"cache key manifest is not UTF-8 JSON: {exc}"
            ) from exc
        if canonical_json_bytes(decoded) != payload:
            raise PersistentCacheValueError(
                "cache key manifest is not canonically encoded"
            )
        if hashlib.sha256(payload).hexdigest() != digest:
            raise PersistentCacheValueError(
                "cache key digest does not match its manifest"
            )
        return cls(digest=digest, manifest=payload)


@dataclass(frozen=True)
class PersistentCacheLookup:
    status: str
    payload: Any | None = None

    @property
    def hit(self) -> bool:
        return self.status == "hit"


class PersistentJsonCache:
    """Best-effort atomic JSON cache with bounded on-disk size."""

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = DEFAULT_CACHE_MAX_BYTES,
        max_entry_bytes: int = DEFAULT_CACHE_MAX_ENTRY_BYTES,
    ) -> None:
        if max_bytes <= 0 or max_entry_bytes <= 0:
            raise ValueError("persistent cache size limits must be positive")
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self.max_entry_bytes = max_entry_bytes
        self._counters = {
            "hits": 0,
            "misses": 0,
            "corruptions": 0,
            "errors": 0,
            "stores": 0,
            "storeFailures": 0,
            "invalidations": 0,
            "prunedFiles": 0,
            "prunedBytes": 0,
        }

    def key(self, manifest: Any) -> PersistentCacheKey:
        return PersistentCacheKey.from_manifest(manifest)

    def lookup(
        self,
        namespace: str,
        key: PersistentCacheKey,
    ) -> PersistentCacheLookup:
        path = self._entry_path(namespace, key)
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            self._counters["misses"] += 1
            return PersistentCacheLookup("miss")
        except OSError:
            self._counters["errors"] += 1
            return PersistentCacheLookup("error")

        try:
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_size <= 0
                or file_stat.st_size > self.max_entry_bytes
            ):
                raise PersistentCacheValueError(
                    "cache entry is not a bounded regular file"
                )
            raw = path.read_bytes()
            if len(raw) != file_stat.st_size:
                raise PersistentCacheValueError(
                    "cache entry changed while being read"
                )
            decoded = json.loads(raw.decode("utf-8"))
            if canonical_json_bytes(decoded) != raw:
                raise PersistentCacheValueError(
                    "cache entry is not canonically encoded"
                )
            expected_fields = {
                "schemaVersion",
                "namespace",
                "keyDigest",
                "keyManifest",
                "payloadSha256",
                "payload",
            }
            if not isinstance(decoded, dict) or set(decoded) != expected_fields:
                raise PersistentCacheValueError(
                    "cache entry has an invalid envelope"
                )
            if (
                decoded["schemaVersion"] != CACHE_ENVELOPE_SCHEMA_VERSION
                or decoded["namespace"] != namespace
                or decoded["keyDigest"] != key.digest
            ):
                raise PersistentCacheValueError(
                    "cache entry envelope identity does not match"
                )
            key_manifest = canonical_json_bytes(decoded["keyManifest"])
            if key_manifest != key.manifest:
                raise PersistentCacheValueError(
                    "cache entry key manifest does not match"
                )
            payload_bytes = canonical_json_bytes(decoded["payload"])
            if (
                not isinstance(decoded["payloadSha256"], str)
                or hashlib.sha256(payload_bytes).hexdigest()
                != decoded["payloadSha256"]
            ):
                raise PersistentCacheValueError(
                    "cache entry payload digest does not match"
                )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            PersistentCacheValueError,
        ):
            self._counters["corruptions"] += 1
            self._unlink_best_effort(path)
            return PersistentCacheLookup("corrupt")

        self._counters["hits"] += 1
        try:
            os.utime(path, None, follow_symlinks=False)
        except (NotImplementedError, OSError):
            pass
        return PersistentCacheLookup("hit", decoded["payload"])

    def store(
        self,
        namespace: str,
        key: PersistentCacheKey,
        payload: Any,
    ) -> bool:
        path = self._entry_path(namespace, key)
        try:
            manifest_value = json.loads(key.manifest.decode("utf-8"))
            payload_bytes = canonical_json_bytes(payload)
            encoded = canonical_json_bytes(
                {
                    "schemaVersion": CACHE_ENVELOPE_SCHEMA_VERSION,
                    "namespace": namespace,
                    "keyDigest": key.digest,
                    "keyManifest": manifest_value,
                    "payloadSha256": hashlib.sha256(
                        payload_bytes
                    ).hexdigest(),
                    "payload": payload,
                }
            )
        except (
            UnicodeError,
            json.JSONDecodeError,
            PersistentCacheValueError,
        ):
            raise
        if len(encoded) > self.max_entry_bytes:
            self._counters["storeFailures"] += 1
            return False

        temp_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_file = tempfile.NamedTemporaryFile(
                mode="w+b",
                prefix=f".{key.digest}.",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            )
            temp_path = Path(temp_file.name)
            with temp_file:
                written = temp_file.write(encoded)
                if written != len(encoded):
                    raise OSError(
                        f"short write for cache entry {temp_path}"
                    )
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, path)
            temp_path = None
            self._fsync_directory(path.parent)
        except OSError:
            self._counters["storeFailures"] += 1
            if temp_path is not None:
                self._unlink_best_effort(temp_path)
            return False

        self._counters["stores"] += 1
        self.prune()
        return True

    def invalidate(
        self,
        namespace: str,
        key: PersistentCacheKey,
    ) -> None:
        path = self._entry_path(namespace, key)
        if self._unlink_best_effort(path):
            self._counters["invalidations"] += 1

    def prune(self) -> None:
        try:
            files: list[tuple[int, int, Path]] = []
            total = 0
            if not self.root.is_dir():
                return
            for directory, dir_names, file_names in os.walk(
                self.root,
                followlinks=False,
            ):
                directory_path = Path(directory)
                dir_names[:] = [
                    name
                    for name in dir_names
                    if not (directory_path / name).is_symlink()
                ]
                for name in file_names:
                    if not name.endswith(".json"):
                        continue
                    path = directory_path / name
                    try:
                        file_stat = path.lstat()
                    except OSError:
                        continue
                    if not stat.S_ISREG(file_stat.st_mode):
                        continue
                    total += file_stat.st_size
                    files.append(
                        (
                            file_stat.st_mtime_ns,
                            file_stat.st_size,
                            path,
                        )
                    )
            if total <= self.max_bytes:
                return
            for _modified_ns, size, path in sorted(
                files,
                key=lambda item: (item[0], str(item[2]).casefold()),
            ):
                if total <= self.max_bytes:
                    break
                if self._unlink_best_effort(path):
                    total -= size
                    self._counters["prunedFiles"] += 1
                    self._counters["prunedBytes"] += size
        except OSError:
            self._counters["errors"] += 1

    def fact(self) -> dict[str, Any]:
        return {
            "schemaVersion": CACHE_ENVELOPE_SCHEMA_VERSION,
            "root": str(self.root),
            "maxBytes": self.max_bytes,
            "maxEntryBytes": self.max_entry_bytes,
            **self._counters,
        }

    def _entry_path(
        self,
        namespace: str,
        key: PersistentCacheKey,
    ) -> Path:
        if _NAMESPACE_RE.fullmatch(namespace) is None:
            raise PersistentCacheValueError(
                f"invalid cache namespace: {namespace!r}"
            )
        checked = PersistentCacheKey.from_canonical_bytes(
            key.digest,
            key.manifest,
        )
        return (
            self.root
            / namespace
            / f"v{CACHE_ENVELOPE_SCHEMA_VERSION}"
            / checked.digest[:2]
            / f"{checked.digest}.json"
        )

    @staticmethod
    def _unlink_best_effort(path: Path) -> bool:
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        return True

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
