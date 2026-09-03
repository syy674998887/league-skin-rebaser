"""Small deterministic WAD v3 fixture builder used by unit tests."""

from __future__ import annotations

import gzip
import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path

import xxhash

try:
    import zstandard as zstd
except ImportError:
    zstd = None


SYNTHETIC_TYPE4_TARGET_PATH = "data/test/synthetic.bin"
SYNTHETIC_TYPE4_SUBCHUNK_TOC_PATH = (
    "data/final/champions/synthetic.wad.subchunktoc"
)
SYNTHETIC_TYPE4_WAD_SHA256 = (
    "3b6eab4328c44e18ee42aa3824921074f67e313dcf5cc2058c63b2e46381aa0b"
)
SYNTHETIC_TYPE4_OUTPUT_SHA256 = (
    "8f5a3465d6ca20b6bccde1b686563f4b89c450a9741512fa891aff2ca2438b62"
)

_TYPE4_FIRST_PAYLOAD = b"PROP" + b"A" * 50
_TYPE4_SECOND_PAYLOAD = b"B" * 50
_TYPE4_FIRST_ZSTD_FRAME = bytes.fromhex(
    "28b52ffd20365d00002850524f5041010046400b"
)
_TYPE4_SECOND_ZSTD_FRAME = bytes.fromhex(
    "28b52ffd2032450000104242010045000b"
)
_TYPE4_HEADER_CHECKSUM = 0xB7B987AF383E6360


@dataclass(frozen=True)
class SyntheticChunk:
    path_hash: int
    payload: bytes
    compression_type: int
    subchunk_count: int = 0
    subchunk_index: int = 0
    duplicated: int = 0
    checksum: int = 0
    stored_payload: bytes | None = None
    declared_compressed_size: int | None = None
    declared_decompressed_size: int | None = None
    offset: int | None = None


@dataclass(frozen=True)
class SyntheticType4Fixture:
    """Paths and expected bytes for the runtime-built capability fixture."""

    wad_path: Path
    hash_dictionary_path: Path
    target_path: str
    subchunk_toc_path: str
    expected_payload: bytes


def _compressed_payload(chunk: SyntheticChunk) -> bytes:
    if chunk.stored_payload is not None:
        return chunk.stored_payload
    if chunk.compression_type == 0:
        return chunk.payload
    if chunk.compression_type == 1:
        return gzip.compress(chunk.payload, mtime=0)
    if chunk.compression_type == 3:
        if zstd is None:
            raise RuntimeError("zstandard is required for synthetic type 3 chunks")
        return zstd.ZstdCompressor().compress(chunk.payload)
    return chunk.payload


def _metadata(chunk: SyntheticChunk, version_minor: int) -> bytes:
    if not 0 <= chunk.subchunk_count <= 0x0F:
        raise ValueError("subchunk_count must fit the high nibble")
    if not 0 <= chunk.subchunk_index <= 0xFFFFFF:
        raise ValueError("subchunk_index must fit 24 bits")
    if version_minor >= 4:
        high = (chunk.subchunk_index >> 16) & 0xFF
        middle = (chunk.subchunk_index >> 8) & 0xFF
        low = chunk.subchunk_index & 0xFF
        return bytes((high, low, middle))
    if chunk.subchunk_index > 0xFFFF:
        raise ValueError("v3.0-v3.3 subchunk_index must fit 16 bits")
    return bytes((chunk.duplicated & 0xFF,)) + struct.pack(
        "<H",
        chunk.subchunk_index,
    )


def write_synthetic_wad(
    path: Path,
    chunks: list[SyntheticChunk],
    *,
    version_minor: int,
) -> None:
    compressed = [_compressed_payload(chunk) for chunk in chunks]
    header_size = 2 + 1 + 1 + 256 + 8 + 4
    data_offset = header_size + len(chunks) * 32
    entries: list[bytes] = []
    cursor = data_offset

    for chunk, raw in zip(chunks, compressed):
        flags = chunk.compression_type | (chunk.subchunk_count << 4)
        offset = cursor if chunk.offset is None else chunk.offset
        compressed_size = (
            len(raw)
            if chunk.declared_compressed_size is None
            else chunk.declared_compressed_size
        )
        decompressed_size = (
            len(chunk.payload)
            if chunk.declared_decompressed_size is None
            else chunk.declared_decompressed_size
        )
        entries.append(
            struct.pack(
                "<QIII",
                chunk.path_hash,
                offset,
                compressed_size,
                decompressed_size,
            )
            + bytes((flags,))
            + _metadata(chunk, version_minor)
            + struct.pack("<Q", chunk.checksum)
        )
        cursor += len(raw)

    body = (
        b"RW"
        + bytes((3, version_minor))
        + bytes(256)
        + bytes(8)
        + struct.pack("<I", len(chunks))
        + b"".join(entries)
        + b"".join(compressed)
    )
    path.write_bytes(body)


def write_synthetic_type4_fixture(root: Path) -> SyntheticType4Fixture:
    """Build the exact synthetic v3.4/type4/count2 fixture at runtime.

    The two embedded Zstandard frames encode only generated ``PROP``/A/B
    bytes.  No Riot payload or prebuilt binary fixture is stored in the
    repository.
    """

    root.mkdir(parents=True, exist_ok=True)
    wad_path = root / "Synthetic.wad.client"
    hash_dictionary_path = root / "hashes.txt"
    expected_payload = _TYPE4_FIRST_PAYLOAD + _TYPE4_SECOND_PAYLOAD
    stored_payload = _TYPE4_FIRST_ZSTD_FRAME + _TYPE4_SECOND_ZSTD_FRAME
    subchunk_toc = b"".join(
        (
            struct.pack(
                "<IIQ",
                len(_TYPE4_FIRST_ZSTD_FRAME),
                len(_TYPE4_FIRST_PAYLOAD),
                xxhash.xxh3_64(_TYPE4_FIRST_ZSTD_FRAME).intdigest(),
            ),
            struct.pack(
                "<IIQ",
                len(_TYPE4_SECOND_ZSTD_FRAME),
                len(_TYPE4_SECOND_PAYLOAD),
                xxhash.xxh3_64(_TYPE4_SECOND_ZSTD_FRAME).intdigest(),
            ),
        )
    )

    target_hash = xxhash.xxh64(
        SYNTHETIC_TYPE4_TARGET_PATH.encode("utf-8")
    ).intdigest()
    subchunk_toc_hash = xxhash.xxh64(
        SYNTHETIC_TYPE4_SUBCHUNK_TOC_PATH.encode("utf-8")
    ).intdigest()
    if target_hash >= subchunk_toc_hash:
        raise AssertionError("fixture table order assumption changed")

    header_size = 272
    entry_size = 32
    target_offset = header_size + entry_size * 2
    toc_offset = target_offset + len(stored_payload)
    target_entry = (
        struct.pack(
            "<QIII",
            target_hash,
            target_offset,
            len(stored_payload),
            len(expected_payload),
        )
        + bytes((4 | (2 << 4), 0, 0, 0))
        + struct.pack(
            "<Q",
            xxhash.xxh3_64(stored_payload).intdigest(),
        )
    )
    toc_entry = (
        struct.pack(
            "<QIII",
            subchunk_toc_hash,
            toc_offset,
            len(subchunk_toc),
            len(subchunk_toc),
        )
        + bytes(4)
        + struct.pack(
            "<Q",
            xxhash.xxh3_64(subchunk_toc).intdigest(),
        )
    )
    wad_bytes = (
        b"RW"
        + bytes((3, 4))
        + bytes(256)
        + struct.pack("<Q", _TYPE4_HEADER_CHECKSUM)
        + struct.pack("<I", 2)
        + target_entry
        + toc_entry
        + stored_payload
        + subchunk_toc
    )
    actual_wad_sha256 = hashlib.sha256(wad_bytes).hexdigest()
    if actual_wad_sha256 != SYNTHETIC_TYPE4_WAD_SHA256:
        raise AssertionError(
            "synthetic type4 fixture construction drifted: "
            f"{actual_wad_sha256}"
        )
    if (
        hashlib.sha256(expected_payload).hexdigest()
        != SYNTHETIC_TYPE4_OUTPUT_SHA256
    ):
        raise AssertionError("synthetic type4 expected output drifted")

    wad_path.write_bytes(wad_bytes)
    hash_dictionary_path.write_text(
        (
            f"{target_hash:016x} {SYNTHETIC_TYPE4_TARGET_PATH}\n"
            f"{subchunk_toc_hash:016x} "
            f"{SYNTHETIC_TYPE4_SUBCHUNK_TOC_PATH}\n"
        ),
        encoding="utf-8",
        newline="\n",
    )
    return SyntheticType4Fixture(
        wad_path=wad_path,
        hash_dictionary_path=hash_dictionary_path,
        target_path=SYNTHETIC_TYPE4_TARGET_PATH,
        subchunk_toc_path=SYNTHETIC_TYPE4_SUBCHUNK_TOC_PATH,
        expected_payload=expected_payload,
    )
