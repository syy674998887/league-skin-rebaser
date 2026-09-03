"""Hash-aware Golden helpers for legacy WAD extraction comparisons."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


_HASHED_FILENAME_RE = re.compile(r"^([0-9a-f]{16})\..+$", re.IGNORECASE)


@dataclass(frozen=True)
class GoldenContext:
    champion: str
    skin_number: int
    unit: str
    stage: str

    def describe(self) -> str:
        return (
            f"champion={self.champion}, skin={self.skin_number}, "
            f"unit={self.unit}, stage={self.stage}"
        )


class GoldenOracleError(ValueError):
    def __init__(self, context: GoldenContext, message: str):
        super().__init__(f"{context.describe()}: {message}")
        self.context = context


@dataclass(frozen=True)
class GoldenPair:
    context: GoldenContext
    base_path: str
    target_path: str
    base_sha256: str
    target_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "context": asdict(self.context),
            "basePath": self.base_path,
            "targetPath": self.target_path,
            "baseSha256": self.base_sha256,
            "targetSha256": self.target_sha256,
        }


class LegacyExtractIndex:
    """Index clear and hash-named outputs from one wad-extract directory."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_dir():
            raise ValueError(f"legacy extraction root is not a directory: {root}")
        self.paths_by_hash: dict[int, list[Path]] = {}
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            resolved_path = path.resolve()
            try:
                resolved_path.relative_to(self.root)
            except ValueError as exc:
                raise ValueError(
                    f"legacy extraction contains a file outside its root: {path}"
                ) from exc
            match = _HASHED_FILENAME_RE.fullmatch(path.name)
            if match is None:
                continue
            self.paths_by_hash.setdefault(
                int(match.group(1), 16),
                [],
            ).append(resolved_path)
        for paths in self.paths_by_hash.values():
            paths.sort(key=lambda path: str(path).casefold())

    def read(
        self,
        relative_path: str,
        expected_hash: int,
        context: GoldenContext,
    ) -> bytes:
        if not isinstance(relative_path, str) or "\0" in relative_path:
            raise GoldenOracleError(context, f"unsafe legacy path: {relative_path!r}")
        windows_path = PureWindowsPath(relative_path)
        normalized = relative_path.replace("\\", "/")
        pure_path = PurePosixPath(normalized)
        if (
            not normalized
            or windows_path.drive
            or windows_path.root
            or pure_path.is_absolute()
            or ".." in pure_path.parts
        ):
            raise GoldenOracleError(context, f"unsafe legacy path: {relative_path!r}")

        clear_path = self.root.joinpath(*pure_path.parts).resolve()
        try:
            clear_path.relative_to(self.root)
        except ValueError as exc:
            raise GoldenOracleError(
                context,
                f"legacy path escapes extraction root: {relative_path!r}",
            ) from exc
        clear_data = clear_path.read_bytes() if clear_path.is_file() else None
        hashed_paths = self.paths_by_hash.get(expected_hash, [])

        if clear_data is not None and hashed_paths:
            conflicting = [
                path
                for path in hashed_paths
                if path.read_bytes() != clear_data
            ]
            if conflicting:
                raise GoldenOracleError(
                    context,
                    "clear and hash-named legacy outputs contain different bytes "
                    f"for {relative_path!r}",
                )
            return clear_data

        if clear_data is not None:
            return clear_data

        if not hashed_paths:
            raise GoldenOracleError(
                context,
                f"legacy output missing {relative_path!r} and "
                f"{expected_hash:016x}.*",
            )
        if len(hashed_paths) != 1:
            raise GoldenOracleError(
                context,
                f"legacy output has {len(hashed_paths)} matches for "
                f"{expected_hash:016x}.*",
            )
        return hashed_paths[0].read_bytes()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_pair_golden(
    *,
    index: LegacyExtractIndex,
    context: GoldenContext,
    base_path: str,
    base_hash: int,
    base_direct: bytes,
    target_path: str,
    target_hash: int,
    target_direct: bytes,
) -> GoldenPair:
    base_legacy = index.read(base_path, base_hash, context)
    target_legacy = index.read(target_path, target_hash, context)
    if base_direct != base_legacy:
        raise GoldenOracleError(context, f"direct/legacy base bytes differ for {base_path}")
    if target_direct != target_legacy:
        raise GoldenOracleError(
            context,
            f"direct/legacy target bytes differ for {target_path}",
        )
    return GoldenPair(
        context=context,
        base_path=base_path,
        target_path=target_path,
        base_sha256=sha256_hex(base_direct),
        target_sha256=sha256_hex(target_direct),
    )
