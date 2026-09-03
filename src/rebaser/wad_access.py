"""Bounded, fail-closed access to the WAD v3 layouts used by the rebaser.

The module deliberately implements only the formats which can be decoded
without interpreting satellite or subchunk tables:

* WAD 3.0, 3.1-3.3, and 3.4 table layouts
* raw (0), gzip (1), and single-frame Zstandard (3) chunks

Satellite (2), ZstdMulti (4), unknown compression types, future WAD
versions, and non-zero subchunk metadata are represented in the index but
fail with :class:`UnsupportedWadFeature` when requested.
"""

from __future__ import annotations

import hashlib
import io
import os
import struct
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, BinaryIO, Protocol

import xxhash
import zstandard as zstd


WAD_MAGIC = b"RW"
WAD_HEADER_SIZE = 272
WAD_ENTRY_SIZE = 32
_MEBIBYTE = 1024 * 1024


class WadError(ValueError):
    """Base class for errors produced by this module."""


class WadPathNotFound(WadError):
    """One or more requested normalized paths are absent from the WAD."""

    def __init__(self, wad_path: Path, paths: Iterable[str]) -> None:
        self.wad_path = wad_path
        self.paths = tuple(paths)
        rendered = ", ".join(self.paths)
        super().__init__(f"paths not found in {wad_path}: {rendered}")


class WadHashNotFound(WadError):
    """One or more requested 64-bit path hashes are absent from the WAD."""

    def __init__(self, wad_path: Path, path_hashes: Iterable[int]) -> None:
        self.wad_path = wad_path
        self.path_hashes = tuple(path_hashes)
        rendered = ", ".join(f"{value:016x}" for value in self.path_hashes)
        super().__init__(f"path hashes not found in {wad_path}: {rendered}")


class UnsupportedWadVersion(WadError):
    """The WAD table layout is not part of the explicit support contract."""

    def __init__(self, wad_path: Path, version: WadVersion) -> None:
        self.wad_path = wad_path
        self.version = version
        super().__init__(
            f"unsupported WAD version {version} in {wad_path}; "
            "supported layouts are 3.0 through 3.4"
        )


class UnsupportedWadFeature(WadError):
    """A requested chunk uses a known but deliberately unsupported feature."""

    def __init__(
        self,
        wad_path: Path,
        message: str,
        *,
        chunk: WadChunk | None = None,
    ) -> None:
        self.wad_path = wad_path
        self.chunk = chunk
        super().__init__(f"{message} in {wad_path}")


class CorruptWad(WadError):
    """The WAD header, table, offsets, or stored bytes are malformed."""

    def __init__(self, wad_path: Path, message: str) -> None:
        self.wad_path = wad_path
        super().__init__(f"{message} in {wad_path}")


class WadDecompressionFailed(WadError):
    """A supported compression stream could not be decoded safely."""

    def __init__(
        self,
        wad_path: Path,
        chunk: WadChunk,
        message: str,
    ) -> None:
        self.wad_path = wad_path
        self.chunk = chunk
        super().__init__(
            f"failed to decompress WAD chunk {chunk.path_hash:016x} "
            f"in {wad_path}: {message}"
        )


class WadSizeMismatch(WadError):
    """Stored or decoded data disagrees with the WAD size declaration."""

    def __init__(
        self,
        wad_path: Path,
        chunk: WadChunk,
        *,
        actual: int | None,
        expected: int,
        detail: str = "decoded size",
    ) -> None:
        self.wad_path = wad_path
        self.chunk = chunk
        self.actual = actual
        self.expected = expected
        self.detail = detail
        actual_text = "unknown" if actual is None else str(actual)
        super().__init__(
            f"WAD chunk {chunk.path_hash:016x} {detail} is {actual_text}; "
            f"expected {expected} in {wad_path}"
        )


class WadReadLimitExceeded(WadError):
    """A declaration or batch exceeds a configured memory/IO limit."""

    def __init__(
        self,
        wad_path: Path,
        message: str,
        *,
        chunk: WadChunk | None = None,
    ) -> None:
        self.wad_path = wad_path
        self.chunk = chunk
        super().__init__(f"{message} in {wad_path}")


class UnexpectedBinPayload(WadError):
    """A requested skin BIN did not have the expected PROP signature."""

    def __init__(self, path: str, payload: bytes) -> None:
        self.path = path
        self.prefix = payload[:8]
        super().__init__(
            f"unexpected BIN payload for {path}: "
            f"expected PROP signature, got {self.prefix.hex() or '<empty>'}"
        )


class WadChangedDuringRead(WadError):
    """The source identity changed while an index/read attempt was active."""

    def __init__(
        self,
        wad_path: Path,
        expected: WadFileIdentity | None,
        actual: WadFileIdentity | None,
    ) -> None:
        self.wad_path = wad_path
        self.expected = expected
        self.actual = actual
        super().__init__(f"WAD changed during read: {wad_path}")


@dataclass(frozen=True, order=True)
class WadVersion:
    """On-disk WAD version."""

    major: int
    minor: int

    def __post_init__(self) -> None:
        if not 0 <= self.major <= 0xFF or not 0 <= self.minor <= 0xFF:
            raise ValueError("WAD version components must fit one byte")

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"

    @property
    def is_supported(self) -> bool:
        return self.major == 3 and 0 <= self.minor <= 4


@dataclass(frozen=True)
class WadFileIdentity:
    """Filesystem identity to which parsed offsets and decoded bytes belong."""

    resolved_path: Path
    device: int
    inode: int
    size: int
    mtime_ns: int
    # Windows can report creation/change time differently through stat() and
    # fstat() immediately after a file is created. Keep it for diagnostics,
    # but bind cached offsets to stable path/handle fields instead.
    ctime_ns: int = field(compare=False)

    @classmethod
    def capture(cls, path: Path | str) -> WadFileIdentity:
        wad_path = Path(path)
        resolved = wad_path.resolve()
        return cls.from_stat(resolved, wad_path.stat())

    @classmethod
    def from_stat(
        cls,
        resolved_path: Path,
        stat_result: os.stat_result,
    ) -> WadFileIdentity:
        return cls(
            resolved_path=resolved_path,
            device=int(stat_result.st_dev),
            inode=int(stat_result.st_ino),
            size=int(stat_result.st_size),
            mtime_ns=_stat_ns(stat_result, "st_mtime_ns", "st_mtime"),
            ctime_ns=_stat_ns(stat_result, "st_ctime_ns", "st_ctime"),
        )


class WadChecksumKind(str, Enum):
    """Meaning of :attr:`WadChunk.checksum`."""

    CHECKSUM_OLD_UNTRUSTED = "checksum_old_untrusted"
    XXH3_64 = "xxh3_64"


@dataclass(frozen=True)
class WadChunk:
    """A parsed 32-byte WAD v3 table entry."""

    path_hash: int
    offset: int
    compressed_size: int
    decompressed_size: int
    compression_type: int
    subchunk_count: int
    subchunk_index: int
    duplicated: bool
    checksum: int | None
    checksum_kind: WadChecksumKind
    raw_entry_tail: bytes
    entry_index: int

    @property
    def end_offset(self) -> int:
        return self.offset + self.compressed_size

    @property
    def has_reliable_checksum(self) -> bool:
        return self.checksum_kind is WadChecksumKind.XXH3_64 and bool(self.checksum)


@dataclass(frozen=True)
class WadReadLimits:
    """Central limits for table parsing and bounded required-BIN reads."""

    max_required_bin_size: int = 64 * _MEBIBYTE
    max_compressed_chunk_size: int = 64 * _MEBIBYTE
    max_read_batch_bytes: int = 256 * _MEBIBYTE
    max_retained_output_bytes: int = 512 * _MEBIBYTE
    max_zstd_window_size: int = 64 * _MEBIBYTE
    stream_buffer_size: int = 64 * 1024
    decompressor_buffer_size: int = 256 * 1024
    max_toc_entries: int = 2_000_000

    def __post_init__(self) -> None:
        for field_name in (
            "max_required_bin_size",
            "max_compressed_chunk_size",
            "max_read_batch_bytes",
            "max_retained_output_bytes",
            "max_zstd_window_size",
            "stream_buffer_size",
            "decompressor_buffer_size",
            "max_toc_entries",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")


DEFAULT_WAD_READ_LIMITS = WadReadLimits()


class WadReadObserver(Protocol):
    """Optional, non-authoritative metrics hook."""

    def __call__(self, event: str, /, **fields: object) -> None: ...


def noop_wad_observer(event: str, /, **fields: object) -> None:
    """Default observer; intentionally performs no work."""


NOOP_WAD_OBSERVER: WadReadObserver = noop_wad_observer


@dataclass(frozen=True)
class WadIndex:
    """A stable WAD index produced from one source identity."""

    wad_path: Path
    version: WadVersion
    file_identity: WadFileIdentity
    chunks_by_hash: Mapping[int, WadChunk]
    toc_digest: str


def normalize_wad_path(path: str) -> str:
    """Return Riot's canonical path spelling used for WAD path hashing."""

    if not isinstance(path, str):
        raise TypeError("WAD path must be a string")
    if "\x00" in path:
        raise ValueError("WAD path must not contain NUL")
    return path.replace("\\", "/").lstrip("/").lower()


def wad_path_hash(path: str) -> int:
    """Return XXH64(seed=0) of a normalized UTF-8 WAD path."""

    normalized = normalize_wad_path(path)
    return xxhash.xxh64(normalized.encode("utf-8"), seed=0).intdigest()


xxh64_wad_path = wad_path_hash


def validate_bin_payload(payload: bytes, path: str = "<unknown>") -> None:
    """Apply the lightweight signature check used before BIN conversion."""

    if not payload.startswith(b"PROP"):
        raise UnexpectedBinPayload(path, payload)


def capture_wad_file_identity(path: Path | str) -> WadFileIdentity:
    """Public wrapper used by cache and source-change code."""

    return WadFileIdentity.capture(path)


def preflight_wad_chunk(
    chunk: WadChunk,
    *,
    wad_path: Path | str = Path("<memory>"),
    limits: WadReadLimits | None = None,
) -> None:
    """Reject unsupported metadata and unsafe declared sizes before IO."""

    path = Path(wad_path)
    effective_limits = limits or DEFAULT_WAD_READ_LIMITS
    _check_supported_chunk(path, chunk)
    _check_chunk_limits(path, chunk, effective_limits)


def decode_wad_chunk(
    raw: bytes,
    chunk: WadChunk,
    *,
    wad_path: Path | str = Path("<memory>"),
    limits: WadReadLimits | None = None,
) -> bytes:
    """Decode one already-read chunk using the same bounded core as Prepared.

    This compatibility entry point lets older callers keep their index/cache
    instrumentation while sharing the strict feature checks and decoders.
    New code should prefer :class:`PreparedChampionWad`, which also binds the
    read to a stable source identity and batches file access.
    """

    path = Path(wad_path)
    effective_limits = limits or DEFAULT_WAD_READ_LIMITS
    if len(raw) != chunk.compressed_size:
        raise WadSizeMismatch(
            path,
            chunk,
            actual=len(raw),
            expected=chunk.compressed_size,
            detail="stored size",
        )
    preflight_wad_chunk(
        chunk,
        wad_path=path,
        limits=effective_limits,
    )
    memory_chunk = replace(chunk, offset=0)
    return _read_and_decode_chunk(
        io.BytesIO(raw),
        path,
        memory_chunk,
        effective_limits,
    )


def parse_wad_index(
    wad_path: Path | str,
    *,
    limits: WadReadLimits | None = None,
    observer: WadReadObserver | None = None,
) -> WadIndex:
    """Parse one stable index, retrying once if the source changes."""

    path = Path(wad_path)
    effective_limits = limits or DEFAULT_WAD_READ_LIMITS
    effective_observer = observer or NOOP_WAD_OBSERVER
    last_change: WadChangedDuringRead | None = None
    for attempt in range(2):
        try:
            return _parse_wad_index_once(path, effective_limits, effective_observer)
        except WadChangedDuringRead as exc:
            last_change = exc
            _emit(effective_observer, "wad.index.retry", attempt=attempt + 1)
    assert last_change is not None
    raise last_change


class PreparedChampionWad:
    """One mounted champion WAD with identity-bound index and decoded cache."""

    def __init__(
        self,
        wad_path: Path | str,
        *,
        identity: object | None = None,
        limits: WadReadLimits | None = None,
        observer: WadReadObserver | None = None,
    ) -> None:
        self.wad_path = Path(wad_path)
        self.identity = identity
        self.champion_identity = identity
        self.limits = limits or DEFAULT_WAD_READ_LIMITS
        self.observer = observer or NOOP_WAD_OBSERVER
        self._decoded_cache: dict[int, bytes] = {}
        self._install_index(
            parse_wad_index(
                self.wad_path,
                limits=self.limits,
                observer=self.observer,
            )
        )

    @property
    def decoded_cache_size(self) -> int:
        return len(self._decoded_cache)

    def contains_path(self, path: str) -> bool:
        normalized = normalize_wad_path(path)
        return self.inspect_paths((normalized,))[normalized] is not None

    contains = contains_path

    def contains_hash(self, path_hash: int) -> bool:
        normalized = _normalize_unique_hashes((path_hash,))
        return self.inspect_hashes(normalized)[normalized[0]] is not None

    def inspect_paths(self, paths: Iterable[str]) -> dict[str, WadChunk | None]:
        normalized = _normalize_unique_paths(paths)
        if not normalized:
            return {}
        path_to_hash = {path: wad_path_hash(path) for path in normalized}
        chunks = self.inspect_hashes(path_to_hash.values())
        return {
            path: chunks[path_hash]
            for path, path_hash in path_to_hash.items()
        }

    def inspect_hashes(
        self,
        path_hashes: Iterable[int],
    ) -> dict[int, WadChunk | None]:
        normalized = _normalize_unique_hashes(path_hashes)
        if not normalized:
            return {}
        last_change: WadChangedDuringRead | None = None
        for attempt in range(2):
            try:
                current = _capture_identity_or_changed(
                    self.wad_path,
                    expected=self.file_identity,
                )
                if current != self.file_identity:
                    raise WadChangedDuringRead(
                        self.wad_path,
                        self.file_identity,
                        current,
                    )
                result = {
                    path_hash: self.chunks_by_hash.get(path_hash)
                    for path_hash in normalized
                }
                ending = _capture_identity_or_changed(
                    self.wad_path,
                    expected=self.file_identity,
                )
                if ending != self.file_identity:
                    raise WadChangedDuringRead(
                        self.wad_path,
                        self.file_identity,
                        ending,
                    )
                return result
            except WadChangedDuringRead as exc:
                last_change = exc
                if attempt:
                    break
                _emit(self.observer, "wad.inspect.retry", attempt=attempt + 1)
                self._install_index(
                    parse_wad_index(
                        self.wad_path,
                        limits=self.limits,
                        observer=self.observer,
                    )
                )
        assert last_change is not None
        raise last_change

    inspect_many = inspect_paths
    inspect_many_hashes = inspect_hashes

    def read_many(
        self,
        paths: Iterable[str],
        *,
        validate_bin: bool = False,
    ) -> dict[str, bytes]:
        """Read all requested paths atomically from one stable WAD identity.

        Input paths are normalized and de-duplicated. Unique uncached chunks
        are read in offset order using one file handle and bounded batches.
        If any requested path fails, this method raises and returns no partial
        mapping. A source-identity change discards the whole attempt, rebuilds
        the index/cache, and retries once.
        """

        normalized = _normalize_unique_paths(paths)
        if not normalized:
            return {}

        last_change: WadChangedDuringRead | None = None
        for attempt in range(2):
            try:
                return self._read_many_once(normalized, validate_bin=validate_bin)
            except WadChangedDuringRead as exc:
                last_change = exc
                if attempt:
                    break
                _emit(self.observer, "wad.read.retry", attempt=attempt + 1)
                self._install_index(
                    _parse_wad_index_once(
                        self.wad_path,
                        self.limits,
                        self.observer,
                    )
                )
        assert last_change is not None
        raise last_change

    def read_hashes(
        self,
        path_hashes: Iterable[int],
        *,
        validate_bin: bool = False,
    ) -> dict[int, bytes]:
        normalized = _normalize_unique_hashes(path_hashes)
        if not normalized:
            return {}

        last_change: WadChangedDuringRead | None = None
        for attempt in range(2):
            try:
                return self._read_hashes_once(
                    normalized,
                    validate_bin=validate_bin,
                    missing_error=lambda missing: WadHashNotFound(
                        self.wad_path,
                        missing,
                    ),
                    validation_names={
                        path_hash: f"<{path_hash:016x}>"
                        for path_hash in normalized
                    },
                )
            except WadChangedDuringRead as exc:
                last_change = exc
                if attempt:
                    break
                _emit(self.observer, "wad.read.retry", attempt=attempt + 1)
                self._install_index(
                    _parse_wad_index_once(
                        self.wad_path,
                        self.limits,
                        self.observer,
                    )
                )
        assert last_change is not None
        raise last_change

    def read_path(self, path: str, *, validate_bin: bool = False) -> bytes:
        normalized = normalize_wad_path(path)
        return self.read_many((normalized,), validate_bin=validate_bin)[normalized]

    def read_hash(self, path_hash: int, *, validate_bin: bool = False) -> bytes:
        normalized = _normalize_unique_hashes((path_hash,))
        return self.read_hashes(
            normalized,
            validate_bin=validate_bin,
        )[normalized[0]]

    def _install_index(self, index: WadIndex) -> None:
        self.version = index.version
        self.file_identity = index.file_identity
        self.wad_identity = index.file_identity
        self.chunks_by_hash = dict(index.chunks_by_hash)
        self.toc_digest = index.toc_digest
        self._decoded_cache.clear()

    def _read_many_once(
        self,
        normalized: tuple[str, ...],
        *,
        validate_bin: bool,
    ) -> dict[str, bytes]:
        path_to_hash = {path: wad_path_hash(path) for path in normalized}
        hash_payloads = self._read_hashes_once(
            tuple(path_to_hash.values()),
            validate_bin=validate_bin,
            missing_error=lambda missing: WadPathNotFound(
                self.wad_path,
                (
                    path
                    for path, path_hash in path_to_hash.items()
                    if path_hash in set(missing)
                ),
            ),
            validation_names={
                path_hash: path
                for path, path_hash in path_to_hash.items()
            },
        )
        return {
            path: hash_payloads[path_hash]
            for path, path_hash in path_to_hash.items()
        }

    def _read_hashes_once(
        self,
        path_hashes: tuple[int, ...],
        *,
        validate_bin: bool,
        missing_error: Callable[[tuple[int, ...]], WadError],
        validation_names: Mapping[int, str],
    ) -> dict[int, bytes]:
        current = _capture_identity_or_changed(
            self.wad_path,
            expected=self.file_identity,
        )
        if current != self.file_identity:
            raise WadChangedDuringRead(
                self.wad_path,
                self.file_identity,
                current,
            )

        missing = tuple(
            path_hash
            for path_hash in path_hashes
            if path_hash not in self.chunks_by_hash
        )
        if missing:
            error = missing_error(missing)
            _raise_changed_if_needed(
                self.wad_path,
                self.file_identity,
                cause=error,
            )
            _emit(self.observer, "wad.read.missing", count=len(missing))
            raise error

        requested_chunks = {
            path_hash: self.chunks_by_hash[path_hash]
            for path_hash in path_hashes
        }
        try:
            for chunk in requested_chunks.values():
                _check_supported_chunk(self.wad_path, chunk)
                _check_chunk_limits(self.wad_path, chunk, self.limits)
        except WadError as exc:
            _raise_changed_if_needed(
                self.wad_path,
                self.file_identity,
                cause=exc,
            )
            raise

        pending = sorted(
            (
                chunk
                for path_hash, chunk in requested_chunks.items()
                if path_hash not in self._decoded_cache
            ),
            key=lambda chunk: (chunk.offset, chunk.path_hash),
        )
        retained_bytes = sum(map(len, self._decoded_cache.values())) + sum(
            chunk.decompressed_size for chunk in pending
        )
        if retained_bytes > self.limits.max_retained_output_bytes:
            raise WadReadLimitExceeded(
                self.wad_path,
                f"decoded cache would retain {retained_bytes} bytes; "
                f"limit is {self.limits.max_retained_output_bytes}",
            )
        batches = _build_batches(self.wad_path, pending, self.limits)
        decoded_this_attempt: dict[int, bytes] = {}

        if pending:
            _emit(
                self.observer,
                "wad.read.open",
                requested=len(requested_chunks),
                pending=len(pending),
                batches=len(batches),
            )
            try:
                with self.wad_path.open("rb") as handle:
                    opened_identity = _identity_from_handle(
                        handle,
                        self.file_identity.resolved_path,
                    )
                    if opened_identity != self.file_identity:
                        raise WadChangedDuringRead(
                            self.wad_path,
                            self.file_identity,
                            opened_identity,
                        )

                    for batch_index, batch in enumerate(batches):
                        _emit(
                            self.observer,
                            "wad.read.batch",
                            index=batch_index,
                            chunks=len(batch),
                            estimated_bytes=sum(
                                _chunk_batch_cost(chunk, self.limits)
                                for chunk in batch
                            ),
                        )
                        for chunk in batch:
                            chunk_fields = {
                                "path_hash": f"{chunk.path_hash:016x}",
                                "compression_type": chunk.compression_type,
                                "compressed_size": chunk.compressed_size,
                                "decompressed_size": chunk.decompressed_size,
                            }
                            _emit(
                                self.observer,
                                "wad.read.chunk_attempt",
                                **chunk_fields,
                            )
                            try:
                                decoded = _read_and_decode_chunk(
                                    handle,
                                    self.wad_path,
                                    chunk,
                                    self.limits,
                                )
                            except Exception as exc:
                                _emit(
                                    self.observer,
                                    "wad.read.chunk_failure",
                                    **chunk_fields,
                                    error_type=type(exc).__name__,
                                )
                                raise
                            decoded_this_attempt[chunk.path_hash] = decoded
                            _emit(
                                self.observer,
                                "wad.read.chunk",
                                **chunk_fields,
                            )

                    _assert_handle_and_path_identity(
                        handle,
                        self.wad_path,
                        self.file_identity,
                    )
            except WadChangedDuringRead:
                raise
            except WadError as exc:
                _raise_changed_if_needed(
                    self.wad_path,
                    self.file_identity,
                    cause=exc,
                )
                raise
            except OSError as exc:
                raise WadChangedDuringRead(
                    self.wad_path,
                    self.file_identity,
                    _try_capture_identity(self.wad_path),
                ) from exc
        else:
            ending = _capture_identity_or_changed(
                self.wad_path,
                expected=self.file_identity,
            )
            if ending != self.file_identity:
                raise WadChangedDuringRead(
                    self.wad_path,
                    self.file_identity,
                    ending,
                )

        combined = self._decoded_cache | decoded_this_attempt
        result = {
            path_hash: combined[path_hash]
            for path_hash in path_hashes
        }
        if validate_bin:
            for path_hash, payload in result.items():
                validate_bin_payload(payload, validation_names[path_hash])

        # Commit cache entries only after the entire read and optional
        # lightweight validation have succeeded against the same identity.
        self._decoded_cache.update(decoded_this_attempt)
        _emit(
            self.observer,
            "wad.read.complete",
            requested=len(result),
            physical_chunks=len(decoded_this_attempt),
            cache_hits=len(requested_chunks) - len(decoded_this_attempt),
        )
        return result


def _stat_ns(
    stat_result: os.stat_result,
    ns_name: str,
    seconds_name: str,
) -> int:
    value = getattr(stat_result, ns_name, None)
    if value is not None:
        return int(value)
    return int(getattr(stat_result, seconds_name) * 1_000_000_000)


def _capture_identity_or_changed(
    path: Path,
    *,
    expected: WadFileIdentity | None,
) -> WadFileIdentity:
    try:
        return WadFileIdentity.capture(path)
    except OSError as exc:
        raise WadChangedDuringRead(path, expected, None) from exc


def _try_capture_identity(path: Path) -> WadFileIdentity | None:
    try:
        return WadFileIdentity.capture(path)
    except OSError:
        return None


def _identity_from_handle(
    handle: BinaryIO,
    resolved_path: Path,
) -> WadFileIdentity:
    return WadFileIdentity.from_stat(resolved_path, os.fstat(handle.fileno()))


def _assert_handle_and_path_identity(
    handle: BinaryIO,
    wad_path: Path,
    expected: WadFileIdentity,
) -> None:
    handle_identity = _identity_from_handle(handle, expected.resolved_path)
    path_identity = _try_capture_identity(wad_path)
    if handle_identity != expected or path_identity != expected:
        raise WadChangedDuringRead(wad_path, expected, path_identity)


def _raise_changed_if_needed(
    wad_path: Path,
    expected: WadFileIdentity,
    *,
    cause: BaseException,
) -> None:
    actual = _try_capture_identity(wad_path)
    if actual != expected:
        raise WadChangedDuringRead(wad_path, expected, actual) from cause


def _read_exact(
    handle: BinaryIO,
    size: int,
    wad_path: Path,
    what: str,
) -> bytes:
    data = bytearray()
    while len(data) < size:
        part = handle.read(size - len(data))
        if not part:
            raise CorruptWad(
                wad_path,
                f"unexpected end of file while reading {what}",
            )
        data.extend(part)
    return bytes(data)


def _parse_wad_index_once(
    wad_path: Path,
    limits: WadReadLimits,
    observer: WadReadObserver,
) -> WadIndex:
    before = _capture_identity_or_changed(wad_path, expected=None)
    try:
        with wad_path.open("rb") as handle:
            opened = _identity_from_handle(handle, before.resolved_path)
            if opened != before:
                raise WadChangedDuringRead(wad_path, before, opened)

            header = _read_exact(
                handle,
                WAD_HEADER_SIZE,
                wad_path,
                "WAD v3 header",
            )
            if header[:2] != WAD_MAGIC:
                raise CorruptWad(
                    wad_path,
                    f"invalid WAD signature {header[:2]!r}",
                )
            version = WadVersion(header[2], header[3])
            if not version.is_supported:
                raise UnsupportedWadVersion(wad_path, version)

            chunk_count = struct.unpack_from("<I", header, 268)[0]
            toc_size = chunk_count * WAD_ENTRY_SIZE
            toc_end = WAD_HEADER_SIZE + toc_size
            if toc_end > before.size:
                raise CorruptWad(
                    wad_path,
                    f"WAD table with {chunk_count} entries exceeds file size",
                )
            if chunk_count > limits.max_toc_entries:
                raise WadReadLimitExceeded(
                    wad_path,
                    f"WAD table has {chunk_count} entries; "
                    f"limit is {limits.max_toc_entries}",
                )

            digest = hashlib.sha256()
            digest.update(header)
            chunks: dict[int, WadChunk] = {}
            for entry_index in range(chunk_count):
                entry = _read_exact(
                    handle,
                    WAD_ENTRY_SIZE,
                    wad_path,
                    f"WAD table entry {entry_index}",
                )
                digest.update(entry)
                chunk = _parse_chunk_entry(
                    wad_path,
                    version,
                    entry,
                    entry_index,
                )
                if chunk.path_hash in chunks:
                    raise CorruptWad(
                        wad_path,
                        f"duplicate path hash {chunk.path_hash:016x} "
                        f"at table entry {entry_index}",
                    )
                if chunk.offset < toc_end:
                    raise CorruptWad(
                        wad_path,
                        f"chunk {chunk.path_hash:016x} offset {chunk.offset} "
                        f"overlaps header/table ending at {toc_end}",
                    )
                if (
                    chunk.offset > before.size
                    or chunk.compressed_size > before.size - chunk.offset
                ):
                    raise CorruptWad(
                        wad_path,
                        f"chunk {chunk.path_hash:016x} range "
                        f"[{chunk.offset}, {chunk.end_offset}) exceeds file size "
                        f"{before.size}",
                    )
                chunks[chunk.path_hash] = chunk

            stored_spans = sorted(
                (
                    chunk.offset,
                    chunk.end_offset,
                    chunk.path_hash,
                )
                for chunk in chunks.values()
                if chunk.compressed_size
            )
            previous: tuple[int, int, int] | None = None
            for span in stored_spans:
                if previous is not None and span[0] < previous[1]:
                    if span[:2] != previous[:2]:
                        raise CorruptWad(
                            wad_path,
                            f"chunk {span[2]:016x} stored range "
                            f"[{span[0]}, {span[1]}) partially overlaps "
                            f"chunk {previous[2]:016x} range "
                            f"[{previous[0]}, {previous[1]})",
                        )
                elif previous is None or span[1] > previous[1]:
                    previous = span

            _assert_handle_and_path_identity(handle, wad_path, before)
    except WadChangedDuringRead:
        raise
    except WadError as exc:
        _raise_changed_if_needed(wad_path, before, cause=exc)
        raise
    except OSError as exc:
        raise WadChangedDuringRead(
            wad_path,
            before,
            _try_capture_identity(wad_path),
        ) from exc

    _emit(
        observer,
        "wad.index.complete",
        version=str(version),
        chunks=len(chunks),
        toc_digest=digest.hexdigest(),
    )
    return WadIndex(
        wad_path=wad_path,
        version=version,
        file_identity=before,
        chunks_by_hash=MappingProxyType(chunks),
        toc_digest=digest.hexdigest(),
    )


def _parse_chunk_entry(
    wad_path: Path,
    version: WadVersion,
    entry: bytes,
    entry_index: int,
) -> WadChunk:
    path_hash, offset, compressed_size, decompressed_size = struct.unpack_from(
        "<QIII",
        entry,
    )
    flags = entry[20]
    compression_type = flags & 0x0F
    subchunk_count = flags >> 4
    raw_checksum = struct.unpack_from("<Q", entry, 24)[0]
    if version.minor <= 3:
        duplicated = bool(entry[21])
        subchunk_index = struct.unpack_from("<H", entry, 22)[0]
    else:
        duplicated = False
        subchunk_index = (entry[21] << 16) | (entry[23] << 8) | entry[22]

    if version.minor == 0:
        checksum: int | None = None
        checksum_kind = WadChecksumKind.CHECKSUM_OLD_UNTRUSTED
    else:
        checksum = raw_checksum
        checksum_kind = WadChecksumKind.XXH3_64

    return WadChunk(
        path_hash=path_hash,
        offset=offset,
        compressed_size=compressed_size,
        decompressed_size=decompressed_size,
        compression_type=compression_type,
        subchunk_count=subchunk_count,
        subchunk_index=subchunk_index,
        duplicated=duplicated,
        checksum=checksum,
        checksum_kind=checksum_kind,
        raw_entry_tail=entry[20:],
        entry_index=entry_index,
    )


def _normalize_unique_paths(paths: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    normalized: list[str] = []
    for path in paths:
        canonical = normalize_wad_path(path)
        if canonical not in seen:
            seen.add(canonical)
            normalized.append(canonical)
    return tuple(normalized)


def _normalize_unique_hashes(path_hashes: Iterable[int]) -> tuple[int, ...]:
    seen: set[int] = set()
    normalized: list[int] = []
    for path_hash in path_hashes:
        if (
            isinstance(path_hash, bool)
            or not isinstance(path_hash, int)
            or not 0 <= path_hash <= 0xFFFFFFFFFFFFFFFF
        ):
            raise ValueError(
                f"WAD path hash must be an unsigned 64-bit integer: {path_hash!r}"
            )
        if path_hash not in seen:
            seen.add(path_hash)
            normalized.append(path_hash)
    return tuple(normalized)


def _check_supported_chunk(wad_path: Path, chunk: WadChunk) -> None:
    if chunk.compression_type == 2:
        raise UnsupportedWadFeature(
            wad_path,
            f"requested WAD chunk {chunk.path_hash:016x} uses Satellite "
            "compression type 2",
            chunk=chunk,
        )
    if chunk.compression_type == 4:
        raise UnsupportedWadFeature(
            wad_path,
            f"requested WAD chunk {chunk.path_hash:016x} uses ZstdMulti "
            "compression type 4",
            chunk=chunk,
        )
    if chunk.compression_type not in (0, 1, 3):
        raise UnsupportedWadFeature(
            wad_path,
            f"requested WAD chunk {chunk.path_hash:016x} uses unknown "
            f"compression type {chunk.compression_type}",
            chunk=chunk,
        )
    if chunk.subchunk_count or chunk.subchunk_index:
        raise UnsupportedWadFeature(
            wad_path,
            f"requested WAD chunk {chunk.path_hash:016x} has unsupported "
            f"subchunk metadata count={chunk.subchunk_count}, "
            f"index={chunk.subchunk_index}",
            chunk=chunk,
        )


def _check_chunk_limits(
    wad_path: Path,
    chunk: WadChunk,
    limits: WadReadLimits,
) -> None:
    if chunk.decompressed_size > limits.max_required_bin_size:
        raise WadReadLimitExceeded(
            wad_path,
            f"chunk {chunk.path_hash:016x} declares "
            f"{chunk.decompressed_size} decompressed bytes; "
            f"limit is {limits.max_required_bin_size}",
            chunk=chunk,
        )
    if chunk.compressed_size > limits.max_compressed_chunk_size:
        raise WadReadLimitExceeded(
            wad_path,
            f"chunk {chunk.path_hash:016x} stores "
            f"{chunk.compressed_size} compressed bytes; "
            f"limit is {limits.max_compressed_chunk_size}",
            chunk=chunk,
        )
    cost = _chunk_batch_cost(chunk, limits)
    if cost > limits.max_read_batch_bytes:
        raise WadReadLimitExceeded(
            wad_path,
            f"chunk {chunk.path_hash:016x} requires an estimated "
            f"{cost}-byte read/decode batch; limit is "
            f"{limits.max_read_batch_bytes}",
            chunk=chunk,
        )


def _chunk_batch_cost(chunk: WadChunk, limits: WadReadLimits) -> int:
    return (
        chunk.compressed_size
        + chunk.decompressed_size
        + limits.decompressor_buffer_size
    )


def _build_batches(
    wad_path: Path,
    chunks: list[WadChunk],
    limits: WadReadLimits,
) -> tuple[tuple[WadChunk, ...], ...]:
    if not chunks:
        return ()
    batches: list[tuple[WadChunk, ...]] = []
    current: list[WadChunk] = []
    current_cost = 0
    for chunk in chunks:
        cost = _chunk_batch_cost(chunk, limits)
        if cost > limits.max_read_batch_bytes:
            raise WadReadLimitExceeded(
                wad_path,
                f"chunk {chunk.path_hash:016x} exceeds the read batch limit",
                chunk=chunk,
            )
        if current and current_cost + cost > limits.max_read_batch_bytes:
            batches.append(tuple(current))
            current = []
            current_cost = 0
        current.append(chunk)
        current_cost += cost
    if current:
        batches.append(tuple(current))
    return tuple(batches)


class _WadSegment:
    """Read-only view over one chunk, capped to incremental IO blocks."""

    def __init__(
        self,
        handle: BinaryIO,
        wad_path: Path,
        chunk: WadChunk,
        max_read_size: int,
        *,
        on_read: Callable[[bytes], None] | None = None,
    ) -> None:
        self._handle = handle
        self._wad_path = wad_path
        self._chunk = chunk
        self._remaining = chunk.compressed_size
        self._max_read_size = max_read_size
        self._on_read = on_read
        handle.seek(chunk.offset)

    @property
    def remaining(self) -> int:
        return self._remaining

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self._remaining == 0:
            return b""
        if size is None or size < 0:
            size = self._max_read_size
        requested = min(size, self._remaining, self._max_read_size)
        data = self._handle.read(requested)
        if not data:
            raise CorruptWad(
                self._wad_path,
                f"unexpected end of file while reading chunk "
                f"{self._chunk.path_hash:016x}",
            )
        self._remaining -= len(data)
        if self._on_read is not None:
            self._on_read(data)
        return data

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        length = len(data)
        buffer[:length] = data
        return length


def _read_and_decode_chunk(
    handle: BinaryIO,
    wad_path: Path,
    chunk: WadChunk,
    limits: WadReadLimits,
) -> bytes:
    if chunk.compression_type == 0:
        return _read_raw_chunk(handle, wad_path, chunk, limits)
    if chunk.compression_type == 1:
        return _read_gzip_chunk(handle, wad_path, chunk, limits)
    if chunk.compression_type == 3:
        return _read_zstd_chunk(handle, wad_path, chunk, limits)
    # Every call site preflights all requested chunks before opening the file.
    raise AssertionError("unsupported chunk reached decoder")


def _read_raw_chunk(
    handle: BinaryIO,
    wad_path: Path,
    chunk: WadChunk,
    limits: WadReadLimits,
) -> bytes:
    if chunk.compressed_size != chunk.decompressed_size:
        raise WadSizeMismatch(
            wad_path,
            chunk,
            actual=chunk.compressed_size,
            expected=chunk.decompressed_size,
            detail="raw stored size",
        )
    segment = _WadSegment(
        handle,
        wad_path,
        chunk,
        limits.stream_buffer_size,
    )
    output = bytearray()
    while segment.remaining:
        output.extend(segment.read())
    return bytes(output)


def _read_gzip_chunk(
    handle: BinaryIO,
    wad_path: Path,
    chunk: WadChunk,
    limits: WadReadLimits,
) -> bytes:
    segment = _WadSegment(
        handle,
        wad_path,
        chunk,
        limits.stream_buffer_size,
    )
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    output = bytearray()
    try:
        while segment.remaining:
            pending = segment.read()
            while pending:
                remaining_budget = chunk.decompressed_size + 1 - len(output)
                if remaining_budget <= 0:
                    raise WadSizeMismatch(
                        wad_path,
                        chunk,
                        actual=len(output),
                        expected=chunk.decompressed_size,
                    )
                output.extend(decoder.decompress(pending, remaining_budget))
                if len(output) > chunk.decompressed_size:
                    raise WadSizeMismatch(
                        wad_path,
                        chunk,
                        actual=len(output),
                        expected=chunk.decompressed_size,
                    )
                pending = decoder.unconsumed_tail
                if decoder.eof:
                    if decoder.unused_data or pending or segment.remaining:
                        raise WadDecompressionFailed(
                            wad_path,
                            chunk,
                            "additional gzip member or trailing compressed data",
                        )
                    break
            if decoder.eof:
                break
    except WadError:
        raise
    except zlib.error as exc:
        raise WadDecompressionFailed(wad_path, chunk, str(exc)) from exc

    if not decoder.eof:
        raise WadDecompressionFailed(
            wad_path,
            chunk,
            "truncated gzip stream",
        )
    if len(output) != chunk.decompressed_size:
        raise WadSizeMismatch(
            wad_path,
            chunk,
            actual=len(output),
            expected=chunk.decompressed_size,
        )
    return bytes(output)


class _ZstdFrameBoundary:
    """Incrementally validate exactly one ordinary Zstandard frame."""

    _DICTIONARY_ID_SIZES = (0, 1, 2, 4)

    def __init__(
        self,
        wad_path: Path,
        chunk: WadChunk,
        limits: WadReadLimits,
    ) -> None:
        self._wad_path = wad_path
        self._chunk = chunk
        self._limits = limits
        self._buffer = bytearray()
        self._state = "magic"
        self._skip = 0
        self._last_block = False
        self._checksum_flag = False
        self._header_field_size = 0
        self._content_size_field_size = 0
        self._has_window_descriptor = False
        self._done = False

    def feed(self, data: bytes) -> None:
        if self._done:
            if data:
                self._trailing()
            return
        self._buffer.extend(data)
        while True:
            if self._state == "magic":
                raw = self._take(4)
                if raw is None:
                    return
                if raw != b"\x28\xb5\x2f\xfd":
                    raise WadDecompressionFailed(
                        self._wad_path,
                        self._chunk,
                        "invalid or unsupported Zstandard frame magic",
                    )
                self._state = "descriptor"
                continue

            if self._state == "descriptor":
                raw = self._take(1)
                if raw is None:
                    return
                descriptor = raw[0]
                if descriptor & 0x18:
                    raise WadDecompressionFailed(
                        self._wad_path,
                        self._chunk,
                        "reserved Zstandard frame descriptor bits are set",
                    )
                content_flag = descriptor >> 6
                single_segment = bool(descriptor & 0x20)
                self._has_window_descriptor = not single_segment
                self._checksum_flag = bool(descriptor & 0x04)
                dictionary_size = self._DICTIONARY_ID_SIZES[descriptor & 0x03]
                if content_flag == 0:
                    content_size = 1 if single_segment else 0
                elif content_flag == 1:
                    content_size = 2
                elif content_flag == 2:
                    content_size = 4
                else:
                    content_size = 8
                self._content_size_field_size = content_size
                self._header_field_size = (
                    (0 if single_segment else 1)
                    + dictionary_size
                    + content_size
                )
                self._state = "header_fields"
                continue

            if self._state == "header_fields":
                raw = self._take(self._header_field_size)
                if raw is None:
                    return
                if self._has_window_descriptor:
                    window_descriptor = raw[0]
                    window_log = 10 + (window_descriptor >> 3)
                    window_base = 1 << window_log
                    window_size = window_base + (
                        (window_base >> 3) * (window_descriptor & 0x07)
                    )
                    if window_size > self._limits.max_zstd_window_size:
                        raise WadReadLimitExceeded(
                            self._wad_path,
                            f"Zstandard frame for chunk "
                            f"{self._chunk.path_hash:016x} requests a "
                            f"{window_size}-byte window; limit is "
                            f"{self._limits.max_zstd_window_size}",
                            chunk=self._chunk,
                        )
                if self._content_size_field_size:
                    encoded = raw[-self._content_size_field_size :]
                    declared = int.from_bytes(encoded, "little")
                    if self._content_size_field_size == 2:
                        declared += 256
                    if declared > self._limits.max_required_bin_size:
                        raise WadReadLimitExceeded(
                            self._wad_path,
                            f"Zstandard frame for chunk "
                            f"{self._chunk.path_hash:016x} declares "
                            f"{declared} output bytes; limit is "
                            f"{self._limits.max_required_bin_size}",
                            chunk=self._chunk,
                        )
                    if declared != self._chunk.decompressed_size:
                        raise WadSizeMismatch(
                            self._wad_path,
                            self._chunk,
                            actual=declared,
                            expected=self._chunk.decompressed_size,
                            detail="Zstandard frame content size",
                        )
                self._state = "block_header"
                continue

            if self._state == "block_header":
                raw = self._take(3)
                if raw is None:
                    return
                header = int.from_bytes(raw, "little")
                self._last_block = bool(header & 1)
                block_type = (header >> 1) & 0x03
                block_size = header >> 3
                if block_type == 3:
                    raise WadDecompressionFailed(
                        self._wad_path,
                        self._chunk,
                        "reserved Zstandard block type",
                    )
                self._skip = 1 if block_type == 1 else block_size
                self._state = "block_payload"
                continue

            if self._state == "block_payload":
                if self._skip:
                    consumed = min(self._skip, len(self._buffer))
                    if consumed == 0:
                        return
                    del self._buffer[:consumed]
                    self._skip -= consumed
                    if self._skip:
                        return
                if self._last_block:
                    self._state = "checksum" if self._checksum_flag else "done"
                else:
                    self._state = "block_header"
                continue

            if self._state == "checksum":
                raw = self._take(4)
                if raw is None:
                    return
                self._state = "done"
                continue

            if self._state == "done":
                self._done = True
                if self._buffer:
                    self._trailing()
                return

            raise AssertionError(f"unknown Zstandard parser state: {self._state}")

    def finish(self, remaining_compressed_bytes: int) -> None:
        if remaining_compressed_bytes:
            self._trailing()
        if not self._done:
            raise WadDecompressionFailed(
                self._wad_path,
                self._chunk,
                "truncated Zstandard frame",
            )

    def _take(self, size: int) -> bytes | None:
        if len(self._buffer) < size:
            return None
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result

    def _trailing(self) -> None:
        raise WadDecompressionFailed(
            self._wad_path,
            self._chunk,
            "additional Zstandard frame or trailing compressed data",
        )


def _read_zstd_chunk(
    handle: BinaryIO,
    wad_path: Path,
    chunk: WadChunk,
    limits: WadReadLimits,
) -> bytes:
    boundary = _ZstdFrameBoundary(wad_path, chunk, limits)
    segment = _WadSegment(
        handle,
        wad_path,
        chunk,
        limits.stream_buffer_size,
        on_read=boundary.feed,
    )
    output = bytearray()
    reader: Any | None = None
    try:
        reader = zstd.ZstdDecompressor().stream_reader(
            segment,
            read_size=limits.stream_buffer_size,
            read_across_frames=False,
            closefd=False,
        )
        while True:
            remaining_budget = chunk.decompressed_size + 1 - len(output)
            if remaining_budget <= 0:
                raise WadSizeMismatch(
                    wad_path,
                    chunk,
                    actual=len(output),
                    expected=chunk.decompressed_size,
                )
            part = reader.read(min(limits.stream_buffer_size, remaining_budget))
            if not part:
                break
            output.extend(part)
            if len(output) > chunk.decompressed_size:
                raise WadSizeMismatch(
                    wad_path,
                    chunk,
                    actual=len(output),
                    expected=chunk.decompressed_size,
                )
    except WadError:
        raise
    except zstd.ZstdError as exc:
        raise WadDecompressionFailed(wad_path, chunk, str(exc)) from exc
    finally:
        if reader is not None:
            try:
                reader.close()
            except Exception:
                pass

    boundary.finish(segment.remaining)
    if len(output) != chunk.decompressed_size:
        raise WadSizeMismatch(
            wad_path,
            chunk,
            actual=len(output),
            expected=chunk.decompressed_size,
        )
    return bytes(output)


def _emit(
    observer: WadReadObserver,
    event: str,
    **fields: object,
) -> None:
    try:
        observer(event, **fields)
    except Exception:
        # Metrics must never change read correctness or fallback behavior.
        return
