"""Shared crash-safe writer and cross-process lock for candidate registries."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class RegistryWriteError(RuntimeError):
    """A registry write could not be completed atomically."""


class RegistryLockTimeout(RegistryWriteError):
    """Another cooperating updater held the registry lock too long."""


def canonical_json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def registry_lock_path(registry_path: Path) -> Path:
    destination = registry_path.resolve()
    return destination.parent / f".{destination.name}.lock"


@contextmanager
def exclusive_registry_lock(
    registry_path: Path,
    *,
    timeout_seconds: float = 10.0,
    poll_seconds: float = 0.05,
) -> Iterator[Path]:
    """Acquire an OS-backed lock which is released automatically on crash."""

    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("lock timeout and poll interval must be positive")
    lock_path = registry_lock_path(registry_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                _try_lock(handle)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise RegistryLockTimeout(
                        f"timed out acquiring registry lock {lock_path}"
                    ) from exc
                time.sleep(poll_seconds)
        try:
            yield lock_path
        finally:
            _unlock(handle)
    finally:
        handle.close()


def prepare_atomic_json(
    destination: Path,
    payload: Any,
) -> tuple[Path, str]:
    """Write, fsync, re-read, and validate a same-directory temp file."""

    resolved = destination.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    expected = canonical_json_bytes(payload)
    temp_file = tempfile.NamedTemporaryFile(
        mode="w+b",
        prefix=f".{resolved.name}.",
        suffix=".tmp",
        dir=resolved.parent,
        delete=False,
    )
    temp_path = Path(temp_file.name)
    try:
        with temp_file:
            written = temp_file.write(expected)
            if written != len(expected):
                raise RegistryWriteError(
                    f"short write for registry temp file {temp_path}"
                )
            temp_file.flush()
            os.fsync(temp_file.fileno())
        actual = temp_path.read_bytes()
        if actual != expected:
            raise RegistryWriteError(
                f"registry temp file changed after fsync: {temp_path}"
            )
        try:
            parsed = json.loads(actual.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegistryWriteError(
                f"registry temp file is not valid UTF-8 JSON: {temp_path}"
            ) from exc
        if parsed != payload:
            raise RegistryWriteError(
                f"registry temp file does not round-trip: {temp_path}"
            )
        return temp_path, hashlib.sha256(actual).hexdigest()
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def commit_atomic_json(
    temp_path: Path,
    destination: Path,
    expected_sha256: str,
) -> None:
    """Verify the prepared temp, replace the target, and verify the result."""

    resolved = destination.resolve()
    temp_bytes = temp_path.read_bytes()
    actual_digest = hashlib.sha256(temp_bytes).hexdigest()
    if actual_digest != expected_sha256:
        raise RegistryWriteError(
            f"registry temp digest changed before replace: {temp_path}"
        )
    os.replace(temp_path, resolved)
    _fsync_directory(resolved.parent)
    committed = resolved.read_bytes()
    if hashlib.sha256(committed).hexdigest() != expected_sha256:
        raise RegistryWriteError(
            f"registry digest mismatch after replace: {resolved}"
        )


def _try_lock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
