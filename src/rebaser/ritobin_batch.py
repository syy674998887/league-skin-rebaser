"""Bounded, exact-output recursive Ritobin batch orchestration."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable


class RitobinBatchError(RuntimeError):
    """A recursive conversion batch failed or produced an incomplete tree."""


@dataclass(frozen=True)
class RitobinBatchItem:
    source: Path
    destination: Path
    relative_path: str


@dataclass(frozen=True)
class RitobinBatchReport:
    batch_count: int
    file_count: int
    input_bytes: int
    batch_file_counts: tuple[int, ...]
    batch_input_bytes: tuple[int, ...]

    @property
    def max_batch_files(self) -> int:
        return max(self.batch_file_counts, default=0)

    @property
    def max_batch_input_bytes(self) -> int:
        return max(self.batch_input_bytes, default=0)


BatchRunner = Callable[[Path, Path, str, str], None]
DiagnosticRunner = Callable[[RitobinBatchItem, str, str], str | None]


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or value.startswith("/")
        or path.is_absolute()
        or ".." in path.parts
        or any(part in {"", "."} for part in path.parts)
    ):
        raise RitobinBatchError(f"unsafe batch relative path: {value!r}")
    return path


def validate_batch_items(
    items: Iterable[RitobinBatchItem],
    *,
    in_format: str,
    out_format: str,
) -> tuple[RitobinBatchItem, ...]:
    validated = tuple(items)
    if not validated:
        return ()
    relative_keys: set[str] = set()
    destination_keys: set[str] = set()
    for item in validated:
        relative = _safe_relative_path(item.relative_path)
        if relative.suffix.casefold() != f".{in_format}".casefold():
            raise RitobinBatchError(
                f"batch input {item.relative_path!r} does not end in .{in_format}"
            )
        relative_key = relative.as_posix().casefold()
        if relative_key in relative_keys:
            raise RitobinBatchError(
                f"duplicate batch relative path: {item.relative_path}"
            )
        relative_keys.add(relative_key)
        destination_key = str(item.destination.resolve()).casefold()
        if destination_key in destination_keys:
            raise RitobinBatchError(
                f"duplicate batch destination: {item.destination}"
            )
        destination_keys.add(destination_key)
        if not item.source.is_file() or item.source.is_symlink():
            raise RitobinBatchError(
                f"batch source is not a regular file: {item.source}"
            )
    return validated


def partition_batch_items(
    items: Iterable[RitobinBatchItem],
    *,
    max_files: int,
    max_bytes: int,
    isolate_above_bytes: int | None = None,
) -> tuple[tuple[RitobinBatchItem, ...], ...]:
    if max_files <= 0 or max_bytes <= 0:
        raise ValueError("batch limits must be positive")
    if isolate_above_bytes is not None and not (
        0 < isolate_above_bytes <= max_bytes
    ):
        raise ValueError(
            "isolated-file threshold must be positive and no greater "
            "than the batch byte limit"
        )
    batches: list[tuple[RitobinBatchItem, ...]] = []
    current: list[RitobinBatchItem] = []
    current_bytes = 0
    for item in items:
        size = item.source.stat().st_size
        if size > max_bytes:
            raise RitobinBatchError(
                f"batch source {item.source} is {size} bytes; "
                f"limit is {max_bytes}"
            )
        if isolate_above_bytes is not None and size > isolate_above_bytes:
            if current:
                batches.append(tuple(current))
                current = []
                current_bytes = 0
            batches.append((item,))
            continue
        if current and (
            len(current) >= max_files
            or current_bytes + size > max_bytes
        ):
            batches.append(tuple(current))
            current = []
            current_bytes = 0
        current.append(item)
        current_bytes += size
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def _output_relative(item: RitobinBatchItem, out_format: str) -> PurePosixPath:
    return _safe_relative_path(item.relative_path).with_suffix(f".{out_format}")


def _actual_output_files(output_root: Path) -> dict[str, Path]:
    actual: dict[str, Path] = {}
    for path in output_root.rglob("*"):
        if path.is_symlink():
            raise RitobinBatchError(f"batch output contains a symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(output_root).as_posix()
        key = relative.casefold()
        if key in actual:
            raise RitobinBatchError(
                f"batch output contains a case-insensitive collision: {relative}"
            )
        actual[key] = path
    return actual


def _diagnostic_text(
    batch: tuple[RitobinBatchItem, ...],
    *,
    in_format: str,
    out_format: str,
    diagnose: DiagnosticRunner,
) -> str:
    failures: list[str] = []
    for item in batch:
        failure = diagnose(item, in_format, out_format)
        if failure is not None:
            failures.append(f"{item.relative_path}: {failure}")
    if not failures:
        return "all files succeeded individually; recursive contract failed"
    return "individual failures: " + "; ".join(failures)


def run_bounded_recursive_batches(
    items: Iterable[RitobinBatchItem],
    *,
    in_format: str,
    out_format: str,
    workspace: Path,
    max_files: int,
    max_bytes: int,
    run_batch: BatchRunner,
    diagnose: DiagnosticRunner,
    isolate_above_bytes: int | None = None,
) -> RitobinBatchReport:
    validated = validate_batch_items(
        items,
        in_format=in_format,
        out_format=out_format,
    )
    batches = partition_batch_items(
        validated,
        max_files=max_files,
        max_bytes=max_bytes,
        isolate_above_bytes=isolate_above_bytes,
    )
    batch_file_counts = tuple(len(batch) for batch in batches)
    batch_input_bytes = tuple(
        sum(item.source.stat().st_size for item in batch)
        for batch in batches
    )
    total_bytes = sum(batch_input_bytes)
    for batch_number, batch in enumerate(batches, start=1):
        with tempfile.TemporaryDirectory(
            prefix=f".ritobin-batch-{in_format}-{batch_number}-",
            dir=workspace,
        ) as temp_name:
            temp_root = Path(temp_name)
            input_root = temp_root / "input"
            output_root = temp_root / "output"
            input_root.mkdir()
            output_root.mkdir()
            for item in batch:
                staged = input_root / Path(*PurePosixPath(item.relative_path).parts)
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item.source, staged)

            run_error: Exception | None = None
            try:
                run_batch(
                    input_root,
                    output_root,
                    in_format,
                    out_format,
                )
            except Exception as exc:
                run_error = exc

            expected = {
                _output_relative(item, out_format).as_posix().casefold(): item
                for item in batch
            }
            try:
                actual = _actual_output_files(output_root)
            except RitobinBatchError as exc:
                actual = {}
                if run_error is None:
                    run_error = exc
            missing = sorted(set(expected) - set(actual))
            extras = sorted(set(actual) - set(expected))
            if run_error is not None or missing or extras:
                diagnostic = _diagnostic_text(
                    batch,
                    in_format=in_format,
                    out_format=out_format,
                    diagnose=diagnose,
                )
                details: list[str] = []
                if run_error is not None:
                    details.append(f"runner error: {run_error}")
                if missing:
                    details.append(f"missing outputs: {missing}")
                if extras:
                    details.append(f"unexpected outputs: {extras}")
                details.append(diagnostic)
                raise RitobinBatchError(
                    f"recursive {in_format}->{out_format} batch "
                    f"{batch_number}/{len(batches)} rejected: "
                    + "; ".join(details)
                ) from run_error

            # Publish intermediate conversion files only after the whole batch
            # has the exact expected output set.
            for relative_key, item in expected.items():
                source = actual[relative_key]
                item.destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = item.destination.with_name(
                    f".{item.destination.name}.{os.getpid()}.tmp"
                )
                try:
                    shutil.copyfile(source, temporary)
                    os.replace(temporary, item.destination)
                finally:
                    temporary.unlink(missing_ok=True)

    return RitobinBatchReport(
        batch_count=len(batches),
        file_count=len(validated),
        input_bytes=total_bytes,
        batch_file_counts=batch_file_counts,
        batch_input_bytes=batch_input_bytes,
    )
