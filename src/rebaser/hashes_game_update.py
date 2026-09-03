"""Fail-closed updater for the local CommunityDragon game-path dictionary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import xxhash

from .registry_write import (
    commit_atomic_json,
    exclusive_registry_lock,
    prepare_atomic_json,
)


DEFAULT_SOURCE_URL = (
    "https://raw.communitydragon.org/data/hashes/lol/hashes.game.txt"
)
USER_AGENT = "league-skin-rebaser/0.1 (hash dictionary updater)"
STATE_SCHEMA_VERSION = 1
UPDATE_MODES = ("auto", "force", "never")
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_RETRIES = 3
DEFAULT_LOCK_TIMEOUT_SECONDS = 600.0
_RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
_HASH_TEXT_RE = re.compile(r"^[0-9a-f]{16}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LOWER_HEX_BYTES = frozenset(b"0123456789abcdef")
_KNOWN_UPSTREAM_HASH_MISMATCHES = (
    (
        "10e25123126b83b0",
        "assets/perks/styles/style5/futuresmarket/futures.ps4.market.dds",
    ),
)
_NETWORK_ERRORS = (
    URLError,
    TimeoutError,
    ConnectionError,
    http.client.HTTPException,
)


class HashUpdateError(RuntimeError):
    """The newest dictionary could not be proven and installed safely."""


class HashNetworkError(HashUpdateError):
    """The configured CommunityDragon source could not be verified."""


class HashValidationError(HashUpdateError):
    """Downloaded dictionary bytes do not satisfy the source contract."""


def _normalize_game_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("/").lower()


@dataclass(frozen=True)
class HashValidationPolicy:
    """Size, shape, and sentinel requirements for one complete dictionary."""

    min_bytes: int = 100 * 1024 * 1024
    max_bytes: int = 512 * 1024 * 1024
    min_rows: int = 1_000_000
    max_line_bytes: int = 16 * 1024
    required_paths: tuple[str, ...] = (
        "data/characters/annie/skins/skin0.bin",
        "data/characters/locke/skins/skin0.bin",
    )
    allowed_hash_mismatches: tuple[tuple[str, str], ...] = (
        _KNOWN_UPSTREAM_HASH_MISMATCHES
    )

    def __post_init__(self) -> None:
        if (
            isinstance(self.min_bytes, bool)
            or not isinstance(self.min_bytes, int)
            or self.min_bytes < 1
        ):
            raise ValueError("min_bytes must be a positive integer")
        if (
            isinstance(self.max_bytes, bool)
            or not isinstance(self.max_bytes, int)
            or self.max_bytes < self.min_bytes
        ):
            raise ValueError("max_bytes must be an integer >= min_bytes")
        if (
            isinstance(self.min_rows, bool)
            or not isinstance(self.min_rows, int)
            or self.min_rows < 1
        ):
            raise ValueError("min_rows must be a positive integer")
        if (
            isinstance(self.max_line_bytes, bool)
            or not isinstance(self.max_line_bytes, int)
            or self.max_line_bytes < 18
        ):
            raise ValueError("max_line_bytes must be an integer >= 18")
        if not self.required_paths:
            raise ValueError("required_paths must not be empty")
        for path in self.required_paths:
            if not isinstance(path, str) or _normalize_game_path(path) != path:
                raise ValueError(
                    f"required path is not canonical WAD spelling: {path!r}"
                )
        for declared, path in self.allowed_hash_mismatches:
            if (
                not isinstance(declared, str)
                or _HASH_TEXT_RE.fullmatch(declared) is None
                or not isinstance(path, str)
                or _normalize_game_path(path) != path
            ):
                raise ValueError(
                    "allowed hash mismatches must contain canonical "
                    "'<16hex>', '<path>' pairs"
                )


DEFAULT_VALIDATION_POLICY = HashValidationPolicy()


@dataclass(frozen=True)
class RemoteValidators:
    """HTTP validators captured from one response."""

    etag: str | None
    last_modified: str | None
    content_length: int | None

    def merged_with(self, fallback: RemoteValidators | None) -> RemoteValidators:
        if fallback is None:
            return self
        return RemoteValidators(
            etag=self.etag or fallback.etag,
            last_modified=self.last_modified or fallback.last_modified,
            content_length=(
                self.content_length
                if self.content_length is not None
                else fallback.content_length
            ),
        )


@dataclass(frozen=True)
class HashFileValidation:
    """Identity and semantic evidence captured while streaming a dictionary."""

    size: int
    row_count: int
    sha256: str


@dataclass(frozen=True)
class HashUpdateResult:
    """Result returned to the CLI and metrics report."""

    action: str
    path: Path
    source_url: str
    validators: RemoteValidators | None
    size: int | None
    row_count: int | None
    sha256: str | None

    def fact(self) -> dict[str, object]:
        validators = self.validators
        return {
            "action": self.action,
            "path": str(self.path),
            "sourceUrl": self.source_url,
            "etag": None if validators is None else validators.etag,
            "lastModified": (
                None if validators is None else validators.last_modified
            ),
            "contentLength": (
                None if validators is None else validators.content_length
            ),
            "size": self.size,
            "rows": self.row_count,
            "sha256": self.sha256,
        }


UrlOpener = Callable[..., Any]


def ensure_latest_hashes_game(
    destination: Path | str,
    state_path: Path | str,
    *,
    mode: str,
    source_url: str = DEFAULT_SOURCE_URL,
    policy: HashValidationPolicy = DEFAULT_VALIDATION_POLICY,
    opener: UrlOpener = urlopen,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> HashUpdateResult:
    """Apply an explicit update policy to the local/remote dictionary.

    ``never`` performs no network or validation. ``auto`` revalidates the
    remote HTTP metadata and skips the large GET only when the remote validators
    and local stat identity both match the last fully validated update.
    ``force`` downloads unconditionally.
    """

    if mode not in UPDATE_MODES:
        raise ValueError(f"unsupported hash update mode: {mode!r}")
    if not isinstance(source_url, str) or not source_url.startswith("https://"):
        raise ValueError("hash source URL must use HTTPS")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 1:
        raise ValueError("retries must be a positive integer")

    resolved_destination = Path(destination).resolve()
    resolved_state = Path(state_path).resolve()
    if resolved_destination == resolved_state:
        raise ValueError("dictionary and updater state paths must differ")

    if mode == "never":
        stat_result = (
            resolved_destination.stat()
            if resolved_destination.is_file()
            else None
        )
        return HashUpdateResult(
            action="skipped",
            path=resolved_destination,
            source_url=source_url,
            validators=None,
            size=None if stat_result is None else stat_result.st_size,
            row_count=None,
            sha256=None,
        )

    _assert_safe_destination(resolved_destination, "dictionary")
    _assert_safe_destination(resolved_state, "updater state")
    resolved_destination.parent.mkdir(parents=True, exist_ok=True)
    resolved_state.parent.mkdir(parents=True, exist_ok=True)

    with exclusive_registry_lock(
        resolved_destination,
        timeout_seconds=lock_timeout_seconds,
    ):
        head_validators = _fetch_remote_validators(
            source_url,
            opener=opener,
            timeout_seconds=timeout_seconds,
            retries=retries,
        )
        state = _load_state(resolved_state)
        if (
            mode == "auto"
            and head_validators is not None
            and _state_proves_current(
                state,
                resolved_destination,
                source_url,
                head_validators,
            )
        ):
            assert state is not None
            local = state["local"]
            return HashUpdateResult(
                action="current",
                path=resolved_destination,
                source_url=source_url,
                validators=head_validators,
                size=int(local["size"]),
                row_count=int(local["rows"]),
                sha256=str(local["sha256"]),
            )

        temp_path, validation, get_validators = _download_with_retries(
            source_url,
            resolved_destination,
            policy,
            opener=opener,
            timeout_seconds=timeout_seconds,
            retries=retries,
        )
        validators = get_validators.merged_with(head_validators)
        try:
            os.replace(temp_path, resolved_destination)
            _fsync_directory(resolved_destination.parent)
        finally:
            temp_path.unlink(missing_ok=True)

        # The fsynced, fully validated temp file was installed by the
        # same-directory atomic replace; rereading the large file here would
        # duplicate its SHA-256 pass before HashSkinIndex consumes it.
        stat_result = resolved_destination.stat()
        state_payload = _build_state(
            source_url,
            validators,
            validation,
            stat_result.st_mtime_ns,
        )
        state_temp, state_digest = prepare_atomic_json(
            resolved_state,
            state_payload,
        )
        commit_atomic_json(
            state_temp,
            resolved_state,
            state_digest,
        )
        return HashUpdateResult(
            action="updated",
            path=resolved_destination,
            source_url=source_url,
            validators=validators,
            size=validation.size,
            row_count=validation.row_count,
            sha256=validation.sha256,
        )


def _fetch_remote_validators(
    source_url: str,
    *,
    opener: UrlOpener,
    timeout_seconds: float,
    retries: int,
) -> RemoteValidators | None:
    """Fetch fast metadata; return None only when HEAD is unsupported."""

    request = _build_request(source_url, method="HEAD")
    for attempt in range(retries):
        try:
            with opener(request, timeout=timeout_seconds) as response:
                _require_success_status(response, source_url)
                return _response_validators(response)
        except HTTPError as exc:
            if exc.code in {405, 501}:
                return None
            if exc.code not in _RETRYABLE_HTTP_CODES or attempt + 1 >= retries:
                raise HashNetworkError(
                    f"hash metadata request failed with HTTP {exc.code}: "
                    f"{source_url}"
                ) from exc
        except _NETWORK_ERRORS as exc:
            if attempt + 1 >= retries:
                raise HashNetworkError(
                    f"could not verify newest hash metadata at {source_url}: "
                    f"{exc}"
                ) from exc
        _retry_delay(attempt)
    raise AssertionError("metadata retry loop exited unexpectedly")


def _download_with_retries(
    source_url: str,
    destination: Path,
    policy: HashValidationPolicy,
    *,
    opener: UrlOpener,
    timeout_seconds: float,
    retries: int,
) -> tuple[Path, HashFileValidation, RemoteValidators]:
    for attempt in range(retries):
        try:
            return _download_once(
                source_url,
                destination,
                policy,
                opener=opener,
                timeout_seconds=timeout_seconds,
            )
        except HashValidationError:
            raise
        except HTTPError as exc:
            if exc.code not in _RETRYABLE_HTTP_CODES or attempt + 1 >= retries:
                raise HashNetworkError(
                    f"hash download failed with HTTP {exc.code}: {source_url}"
                ) from exc
        except _NETWORK_ERRORS as exc:
            if attempt + 1 >= retries:
                raise HashNetworkError(
                    f"hash download failed after {retries} attempt(s): {exc}"
                ) from exc
        _retry_delay(attempt)
    raise AssertionError("download retry loop exited unexpectedly")


def _download_once(
    source_url: str,
    destination: Path,
    policy: HashValidationPolicy,
    *,
    opener: UrlOpener,
    timeout_seconds: float,
) -> tuple[Path, HashFileValidation, RemoteValidators]:
    temp_file = tempfile.NamedTemporaryFile(
        mode="w+b",
        prefix=f".{destination.name}.",
        suffix=".download",
        dir=destination.parent,
        delete=False,
    )
    temp_path = Path(temp_file.name)
    try:
        request = _build_request(source_url, method="GET")
        with temp_file:
            with opener(request, timeout=timeout_seconds) as response:
                _require_success_status(response, source_url)
                validators = _response_validators(response)
                validation = _stream_and_validate(
                    response,
                    temp_file,
                    policy,
                )
            temp_file.flush()
            os.fsync(temp_file.fileno())
        if (
            validators.content_length is not None
            and validators.content_length != validation.size
        ):
            raise HashValidationError(
                "downloaded dictionary size differs from HTTP Content-Length: "
                f"{validation.size} != {validators.content_length}"
            )
        if temp_path.stat().st_size != validation.size:
            raise HashValidationError(
                "dictionary temp file size changed after fsync"
            )
        return temp_path, validation, validators
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _stream_and_validate(
    response: Any,
    destination: Any,
    policy: HashValidationPolicy,
) -> HashFileValidation:
    digest = hashlib.sha256()
    total = 0
    rows = 0
    required = set(policy.required_paths)
    found_required: set[str] = set()
    allowed_mismatches = set(policy.allowed_hash_mismatches)

    while True:
        raw_line = response.readline(policy.max_line_bytes + 1)
        if not raw_line:
            break
        if (
            len(raw_line) > policy.max_line_bytes
            or (
                len(raw_line) == policy.max_line_bytes + 1
                and not raw_line.endswith(b"\n")
            )
        ):
            raise HashValidationError(
                f"dictionary row {rows + 1} exceeds the line-size limit"
            )
        total += len(raw_line)
        if total > policy.max_bytes:
            raise HashValidationError(
                f"dictionary exceeds the {policy.max_bytes}-byte limit"
            )
        written = destination.write(raw_line)
        if written != len(raw_line):
            raise HashValidationError("short write while staging dictionary")
        digest.update(raw_line)

        logical = raw_line[:-1] if raw_line.endswith(b"\n") else raw_line
        if logical.endswith(b"\r"):
            logical = logical[:-1]
        rows += 1
        path = _validate_dictionary_row(
            logical,
            rows,
            allowed_mismatches,
        )
        if path in required:
            found_required.add(path)

    if total < policy.min_bytes:
        raise HashValidationError(
            f"dictionary is too small: {total} < {policy.min_bytes} bytes"
        )
    if rows < policy.min_rows:
        raise HashValidationError(
            f"dictionary has too few rows: {rows} < {policy.min_rows}"
        )
    missing = sorted(required - found_required)
    if missing:
        raise HashValidationError(
            f"dictionary is missing required current paths: {missing}"
        )
    return HashFileValidation(
        size=total,
        row_count=rows,
        sha256=digest.hexdigest(),
    )


def _validate_dictionary_row(
    raw: bytes,
    row_number: int,
    allowed_hash_mismatches: set[tuple[str, str]],
) -> str:
    if len(raw) < 18 or raw[16:17] != b" ":
        raise HashValidationError(
            f"dictionary row {row_number} is not '<16hex> <path>'"
        )
    declared_raw = raw[:16]
    if any(byte not in _LOWER_HEX_BYTES for byte in declared_raw):
        raise HashValidationError(
            f"dictionary row {row_number} has an invalid lowercase hash"
        )
    path_raw = raw[17:]
    if not path_raw or b"\x00" in path_raw:
        raise HashValidationError(
            f"dictionary row {row_number} has an empty or NUL path"
        )
    try:
        path = path_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HashValidationError(
            f"dictionary row {row_number} path is not UTF-8"
        ) from exc
    if _normalize_game_path(path) != path:
        raise HashValidationError(
            f"dictionary row {row_number} path is not canonical: {path!r}"
        )
    declared = int(declared_raw, 16)
    computed = xxhash.xxh64(path_raw, seed=0).intdigest()
    declared_text = declared_raw.decode("ascii")
    if (
        declared != computed
        and (declared_text, path) not in allowed_hash_mismatches
    ):
        raise HashValidationError(
            f"dictionary row {row_number} hash mismatch for {path!r}: "
            f"{declared:016x} != {computed:016x}"
        )
    return path


def _build_request(source_url: str, *, method: str) -> Request:
    return Request(
        source_url,
        method=method,
        headers={
            "Accept": "text/plain",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": USER_AGENT,
        },
    )


def _require_success_status(response: Any, source_url: str) -> None:
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(response, "code", 200)
    if not isinstance(status, int) or not 200 <= status < 300:
        raise HashNetworkError(
            f"unexpected HTTP status {status!r} from {source_url}"
        )
    final_url = getattr(response, "geturl", lambda: source_url)()
    if not isinstance(final_url, str) or not final_url.startswith("https://"):
        raise HashNetworkError(
            f"hash source redirected outside HTTPS: {final_url!r}"
        )


def _response_validators(response: Any) -> RemoteValidators:
    headers = getattr(response, "headers", {})
    etag = _clean_header(headers, "ETag")
    last_modified = _clean_header(headers, "Last-Modified")
    content_length_text = _clean_header(headers, "Content-Length")
    content_length: int | None = None
    if content_length_text is not None:
        try:
            parsed = int(content_length_text)
        except ValueError as exc:
            raise HashNetworkError(
                f"invalid HTTP Content-Length: {content_length_text!r}"
            ) from exc
        if parsed < 0:
            raise HashNetworkError(
                f"invalid negative HTTP Content-Length: {parsed}"
            )
        content_length = parsed
    return RemoteValidators(
        etag=etag,
        last_modified=last_modified,
        content_length=content_length,
    )


def _clean_header(headers: Mapping[str, Any], name: str) -> str | None:
    value = headers.get(name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _load_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("schemaVersion") != STATE_SCHEMA_VERSION:
        return None
    source = parsed.get("source")
    local = parsed.get("local")
    if not isinstance(source, dict) or not isinstance(local, dict):
        return None
    return parsed


def _state_proves_current(
    state: dict[str, Any] | None,
    destination: Path,
    source_url: str,
    remote: RemoteValidators,
) -> bool:
    if state is None or not destination.is_file() or destination.is_symlink():
        return False
    source = state.get("source")
    local = state.get("local")
    if not isinstance(source, dict) or not isinstance(local, dict):
        return False
    if source.get("url") != source_url:
        return False
    if not _remote_matches_state(remote, source):
        return False

    size = local.get("size")
    modified_ns = local.get("modifiedNs")
    rows = local.get("rows")
    sha256 = local.get("sha256")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size < 1
        or isinstance(modified_ns, bool)
        or not isinstance(modified_ns, int)
        or modified_ns < 0
        or isinstance(rows, bool)
        or not isinstance(rows, int)
        or rows < 1
        or not isinstance(sha256, str)
        or _SHA256_RE.fullmatch(sha256) is None
    ):
        return False
    try:
        stat_result = destination.stat()
    except OSError:
        return False
    return (
        stat_result.st_size == size
        and stat_result.st_mtime_ns == modified_ns
    )


def _remote_matches_state(
    remote: RemoteValidators,
    source_state: Mapping[str, Any],
) -> bool:
    validator_matches: list[bool] = []
    if remote.etag is not None:
        validator_matches.append(source_state.get("etag") == remote.etag)
    if remote.last_modified is not None:
        validator_matches.append(
            source_state.get("lastModified") == remote.last_modified
        )
    if not validator_matches or not all(validator_matches):
        return False
    return (
        remote.content_length is None
        or source_state.get("contentLength") == remote.content_length
    )


def _build_state(
    source_url: str,
    validators: RemoteValidators,
    validation: HashFileValidation,
    modified_ns: int,
) -> dict[str, object]:
    return {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "source": {
            "url": source_url,
            "etag": validators.etag,
            "lastModified": validators.last_modified,
            "contentLength": validators.content_length,
        },
        "local": {
            "size": validation.size,
            "modifiedNs": modified_ns,
            "rows": validation.row_count,
            "sha256": validation.sha256,
        },
    }


def _assert_safe_destination(path: Path, label: str) -> None:
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise HashUpdateError(
            f"{label} path must be a regular file when it exists: {path}"
        )
    parent = path.parent
    if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
        raise HashUpdateError(
            f"{label} parent must be a regular directory: {parent}"
        )


def _retry_delay(attempt: int) -> None:
    time.sleep(0.25 * (2**attempt))


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
