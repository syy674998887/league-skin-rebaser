from __future__ import annotations

import gzip
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import zstandard as zstd

from rebaser import wad_access
from helpers.synthetic_wad import SyntheticChunk, write_synthetic_wad


ANNIE_SKIN0 = "data/characters/annie/skins/skin0.bin"
ANNIE_SKIN1 = "data/characters/annie/skins/skin1.bin"
TIBBERS_SKIN0 = "data/characters/annietibbers/skins/skin0.bin"


def write_paths(
    path: Path,
    entries: list[tuple[str, bytes, int]],
    *,
    version_minor: int = 4,
) -> None:
    write_synthetic_wad(
        path,
        [
            SyntheticChunk(
                path_hash=wad_access.wad_path_hash(chunk_path),
                payload=payload,
                compression_type=compression_type,
            )
            for chunk_path, payload, compression_type in entries
        ],
        version_minor=version_minor,
    )


class RecordingObserver:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def __call__(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))

    def named(self, name: str) -> list[dict[str, object]]:
        return [fields for event, fields in self.events if event == name]


class WadPathHashTests(unittest.TestCase):
    def test_normalization_and_fixed_xxh64_vector(self) -> None:
        variants = (
            ANNIE_SKIN0,
            "/DATA/CHARACTERS/ANNIE/SKINS/SKIN0.BIN",
            r"\Data\Characters\Annie\Skins\Skin0.bin",
        )

        self.assertEqual(
            wad_access.normalize_wad_path(variants[2]),
            ANNIE_SKIN0,
        )
        for variant in variants:
            self.assertEqual(
                wad_access.wad_path_hash(variant),
                0x599C1DD4B0FE6EF4,
            )
        self.assertEqual(
            struct.unpack("<Q", struct.pack("<Q", 0x599C1DD4B0FE6EF4))[0],
            wad_access.wad_path_hash(ANNIE_SKIN0),
        )

    def test_invalid_path_inputs_are_rejected(self) -> None:
        with self.assertRaises(TypeError):
            wad_access.normalize_wad_path(123)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            wad_access.normalize_wad_path("data/\x00/file")


class WadIndexTests(unittest.TestCase):
    def test_v3_layout_metadata_is_parsed_without_entry_drift(self) -> None:
        cases = (
            (0, 0xBEEF, 7, True, None, wad_access.WadChecksumKind.CHECKSUM_OLD_UNTRUSTED),
            (1, 0x1234, 2, True, 0x1020304050607080, wad_access.WadChecksumKind.XXH3_64),
            (3, 0xCAFE, 8, True, 0x1122334455667788, wad_access.WadChecksumKind.XXH3_64),
            (4, 0xA1B2C3, 9, False, 0x8877665544332211, wad_access.WadChecksumKind.XXH3_64),
        )
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            for minor, subchunk_index, subchunk_count, duplicated, checksum, kind in cases:
                with self.subTest(minor=minor):
                    first_hash = 0x1000000000000000 + minor
                    second_hash = 0x2000000000000000 + minor
                    wad_path = root / f"v3-{minor}.wad"
                    raw_checksum = (
                        0x0102030405060708 if checksum is None else checksum
                    )
                    write_synthetic_wad(
                        wad_path,
                        [
                            SyntheticChunk(first_hash, b"first", 0),
                            SyntheticChunk(
                                second_hash,
                                b"second",
                                0,
                                subchunk_count=subchunk_count,
                                subchunk_index=subchunk_index,
                                duplicated=1,
                                checksum=raw_checksum,
                            ),
                        ],
                        version_minor=minor,
                    )

                    index = wad_access.parse_wad_index(wad_path)
                    self.assertEqual(index.version, wad_access.WadVersion(3, minor))
                    self.assertEqual(len(index.chunks_by_hash), 2)
                    chunk = index.chunks_by_hash[second_hash]
                    self.assertEqual(chunk.subchunk_count, subchunk_count)
                    self.assertEqual(chunk.subchunk_index, subchunk_index)
                    self.assertEqual(chunk.duplicated, duplicated)
                    self.assertEqual(chunk.checksum, checksum)
                    self.assertEqual(chunk.checksum_kind, kind)
                    self.assertEqual(len(chunk.raw_entry_tail), 12)
                    self.assertEqual(chunk.entry_index, 1)

    def test_v3_0_checksum_old_changes_digest_but_is_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            digests: list[str] = []
            for checksum in (1, 2):
                wad_path = root / f"old-{checksum}.wad"
                write_synthetic_wad(
                    wad_path,
                    [SyntheticChunk(1, b"data", 0, checksum=checksum)],
                    version_minor=0,
                )
                index = wad_access.parse_wad_index(wad_path)
                chunk = index.chunks_by_hash[1]
                self.assertIsNone(chunk.checksum)
                self.assertFalse(chunk.has_reliable_checksum)
                digests.append(index.toc_digest)
            self.assertNotEqual(*digests)

    def test_future_minor_and_other_major_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            for major, minor in ((3, 5), (2, 4), (4, 0)):
                with self.subTest(version=f"{major}.{minor}"):
                    wad_path = root / f"{major}-{minor}.wad"
                    write_synthetic_wad(
                        wad_path,
                        [SyntheticChunk(1, b"data", 0)],
                        version_minor=minor,
                    )
                    data = bytearray(wad_path.read_bytes())
                    data[2] = major
                    wad_path.write_bytes(data)
                    with self.assertRaises(wad_access.UnsupportedWadVersion):
                        wad_access.parse_wad_index(wad_path)

    def test_truncated_header_and_toc_are_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            short_header = root / "short-header.wad"
            short_header.write_bytes(b"RW\x03\x04")
            with self.assertRaises(wad_access.CorruptWad):
                wad_access.parse_wad_index(short_header)

            short_toc = root / "short-toc.wad"
            write_synthetic_wad(
                short_toc,
                [SyntheticChunk(1, b"data", 0)],
                version_minor=4,
            )
            short_toc.write_bytes(short_toc.read_bytes()[:280])
            with self.assertRaises(wad_access.CorruptWad):
                wad_access.parse_wad_index(short_toc)

            short_data = root / "short-data.wad"
            write_synthetic_wad(
                short_data,
                [
                    SyntheticChunk(
                        1,
                        b"x",
                        0,
                        declared_compressed_size=2,
                    )
                ],
                version_minor=4,
            )
            with self.assertRaises(wad_access.CorruptWad):
                wad_access.parse_wad_index(short_data)

    def test_duplicate_hash_and_invalid_offsets_are_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            duplicate = root / "duplicate.wad"
            write_synthetic_wad(
                duplicate,
                [
                    SyntheticChunk(1, b"first", 0),
                    SyntheticChunk(1, b"second", 0),
                ],
                version_minor=4,
            )
            with self.assertRaisesRegex(wad_access.CorruptWad, "duplicate"):
                wad_access.parse_wad_index(duplicate)

            overlaps_toc = root / "overlap.wad"
            write_synthetic_wad(
                overlaps_toc,
                [SyntheticChunk(2, b"data", 0, offset=272)],
                version_minor=4,
            )
            with self.assertRaisesRegex(wad_access.CorruptWad, "overlaps"):
                wad_access.parse_wad_index(overlaps_toc)

            outside = root / "outside.wad"
            write_synthetic_wad(
                outside,
                [SyntheticChunk(3, b"data", 0, offset=100_000)],
                version_minor=4,
            )
            with self.assertRaisesRegex(wad_access.CorruptWad, "exceeds"):
                wad_access.parse_wad_index(outside)

            shared_span = root / "shared-span.wad"
            write_synthetic_wad(
                shared_span,
                [
                    SyntheticChunk(4, b"same", 0, offset=336),
                    SyntheticChunk(5, b"same", 0, offset=336),
                ],
                version_minor=4,
            )
            shared_index = wad_access.parse_wad_index(shared_span)
            self.assertEqual(shared_index.chunks_by_hash[4].offset, 336)
            self.assertEqual(shared_index.chunks_by_hash[5].offset, 336)

            partial_overlap = root / "partial-overlap.wad"
            write_synthetic_wad(
                partial_overlap,
                [
                    SyntheticChunk(6, b"abcdef", 0, offset=336),
                    SyntheticChunk(7, b"xyz", 0, offset=339),
                ],
                version_minor=4,
            )
            with self.assertRaisesRegex(wad_access.CorruptWad, "partially overlaps"):
                wad_access.parse_wad_index(partial_overlap)

    def test_chunk_count_limit_is_typed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "two.wad"
            write_synthetic_wad(
                wad_path,
                [
                    SyntheticChunk(1, b"a", 0),
                    SyntheticChunk(2, b"b", 0),
                ],
                version_minor=4,
            )
            limits = wad_access.WadReadLimits(max_toc_entries=1)
            with self.assertRaises(wad_access.WadReadLimitExceeded):
                wad_access.parse_wad_index(wad_path, limits=limits)


class PreparedChampionWadTests(unittest.TestCase):
    def test_raw_gzip_and_zstd_are_read_by_normalized_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "formats.wad"
            write_paths(
                wad_path,
                [
                    (ANNIE_SKIN0, b"PROPraw", 0),
                    (ANNIE_SKIN1, b"PROPgzip" * 100, 1),
                    (TIBBERS_SKIN0, b"PROPzstd" * 100, 3),
                ],
            )
            prepared = wad_access.PreparedChampionWad(wad_path)

            result = prepared.read_many(
                (
                    r"\DATA\CHARACTERS\ANNIE\SKINS\SKIN0.BIN",
                    ANNIE_SKIN1,
                    TIBBERS_SKIN0,
                ),
                validate_bin=True,
            )

            self.assertEqual(result[ANNIE_SKIN0], b"PROPraw")
            self.assertEqual(result[ANNIE_SKIN1], b"PROPgzip" * 100)
            self.assertEqual(result[TIBBERS_SKIN0], b"PROPzstd" * 100)

    def test_contains_inspect_missing_and_single_path_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "inspect.wad"
            write_paths(wad_path, [(ANNIE_SKIN0, b"PROPdata", 0)])
            prepared = wad_access.PreparedChampionWad(wad_path)

            self.assertTrue(prepared.contains_path(ANNIE_SKIN0.upper()))
            self.assertFalse(prepared.contains_path(ANNIE_SKIN1))
            inspected = prepared.inspect_many((ANNIE_SKIN0, ANNIE_SKIN1))
            self.assertIsInstance(inspected[ANNIE_SKIN0], wad_access.WadChunk)
            self.assertIsNone(inspected[ANNIE_SKIN1])
            self.assertEqual(prepared.read_path(ANNIE_SKIN0), b"PROPdata")
            with self.assertRaises(wad_access.WadPathNotFound) as raised:
                prepared.read_many((ANNIE_SKIN0, ANNIE_SKIN1))
            self.assertEqual(raised.exception.paths, (ANNIE_SKIN1,))

    def test_hash_api_inspects_reads_deduplicates_and_reports_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "hash-api.wad"
            write_paths(
                wad_path,
                [
                    (ANNIE_SKIN0, b"PROPbase", 0),
                    (ANNIE_SKIN1, b"PROPtarget", 0),
                ],
            )
            observer = RecordingObserver()
            prepared = wad_access.PreparedChampionWad(
                wad_path,
                observer=observer,
            )
            base_hash = wad_access.wad_path_hash(ANNIE_SKIN0)
            target_hash = wad_access.wad_path_hash(ANNIE_SKIN1)
            missing_hash = wad_access.wad_path_hash(TIBBERS_SKIN0)

            self.assertTrue(prepared.contains_hash(base_hash))
            self.assertFalse(prepared.contains_hash(missing_hash))
            inspected = prepared.inspect_hashes((target_hash, missing_hash))
            self.assertIsInstance(inspected[target_hash], wad_access.WadChunk)
            self.assertIsNone(inspected[missing_hash])

            payloads = prepared.read_hashes(
                (target_hash, base_hash, target_hash),
                validate_bin=True,
            )
            self.assertEqual(
                payloads,
                {
                    target_hash: b"PROPtarget",
                    base_hash: b"PROPbase",
                },
            )
            self.assertEqual(
                prepared.read_hash(base_hash, validate_bin=True),
                b"PROPbase",
            )
            self.assertEqual(len(observer.named("wad.read.chunk")), 2)

            with self.assertRaises(wad_access.WadHashNotFound) as raised:
                prepared.read_hashes((base_hash, missing_hash))
            self.assertEqual(raised.exception.path_hashes, (missing_hash,))

    def test_hash_api_rejects_non_uint64_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "hash-input.wad"
            write_paths(wad_path, [(ANNIE_SKIN0, b"PROPdata", 0)])
            prepared = wad_access.PreparedChampionWad(wad_path)

            for invalid in (-1, 1 << 64, True, "1"):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(ValueError):
                        prepared.inspect_hashes((invalid,))  # type: ignore[arg-type]

    def test_contains_and_inspect_refresh_after_source_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "inspect-replaced.wad"
            write_paths(wad_path, [(ANNIE_SKIN0, b"old", 0)])
            observer = RecordingObserver()
            prepared = wad_access.PreparedChampionWad(
                wad_path,
                observer=observer,
            )
            self.assertEqual(prepared.read_path(ANNIE_SKIN0), b"old")
            self.assertEqual(prepared.decoded_cache_size, 1)

            write_paths(
                wad_path,
                [(ANNIE_SKIN1, b"new-payload-is-a-different-size", 0)],
            )

            self.assertFalse(prepared.contains_path(ANNIE_SKIN0))
            self.assertTrue(prepared.contains_path(ANNIE_SKIN1))
            inspected = prepared.inspect_many((ANNIE_SKIN0, ANNIE_SKIN1))
            self.assertIsNone(inspected[ANNIE_SKIN0])
            self.assertIsInstance(inspected[ANNIE_SKIN1], wad_access.WadChunk)
            self.assertEqual(prepared.decoded_cache_size, 0)
            self.assertEqual(len(observer.named("wad.index.complete")), 2)

    def test_duplicate_paths_are_read_once_and_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "dedupe.wad"
            write_paths(wad_path, [(ANNIE_SKIN0, b"PROPdata", 0)])
            observer = RecordingObserver()
            prepared = wad_access.PreparedChampionWad(
                wad_path,
                observer=observer,
            )

            result = prepared.read_many(
                (ANNIE_SKIN0, ANNIE_SKIN0.upper(), "\\" + ANNIE_SKIN0)
            )
            self.assertEqual(result, {ANNIE_SKIN0: b"PROPdata"})
            self.assertEqual(len(observer.named("wad.read.chunk")), 1)
            self.assertEqual(prepared.decoded_cache_size, 1)

            prepared.read_many((ANNIE_SKIN0,))
            self.assertEqual(len(observer.named("wad.read.chunk")), 1)
            completed = observer.named("wad.read.complete")[-1]
            self.assertEqual(completed["physical_chunks"], 0)
            self.assertEqual(completed["cache_hits"], 1)
            self.assertEqual(len(observer.named("wad.index.complete")), 1)

    def test_chunks_are_read_in_offset_order_and_split_into_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "batches.wad"
            write_paths(
                wad_path,
                [
                    (ANNIE_SKIN0, b"a" * 10, 0),
                    (ANNIE_SKIN1, b"b" * 10, 0),
                ],
            )
            observer = RecordingObserver()
            limits = wad_access.WadReadLimits(
                max_required_bin_size=100,
                max_compressed_chunk_size=100,
                max_read_batch_bytes=40,
                stream_buffer_size=4,
                decompressor_buffer_size=16,
            )
            prepared = wad_access.PreparedChampionWad(
                wad_path,
                limits=limits,
                observer=observer,
            )

            result = prepared.read_many((ANNIE_SKIN1, ANNIE_SKIN0))

            self.assertEqual(tuple(result), (ANNIE_SKIN1, ANNIE_SKIN0))
            read_hashes = [
                fields["path_hash"]
                for fields in observer.named("wad.read.chunk")
            ]
            self.assertEqual(
                read_hashes,
                [
                    f"{wad_access.wad_path_hash(ANNIE_SKIN0):016x}",
                    f"{wad_access.wad_path_hash(ANNIE_SKIN1):016x}",
                ],
            )
            self.assertEqual(len(observer.named("wad.read.batch")), 2)

    def test_one_read_many_stage_opens_the_data_file_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "single-open.wad"
            write_paths(
                wad_path,
                [
                    (ANNIE_SKIN0, b"first", 0),
                    (ANNIE_SKIN1, b"second", 1),
                ],
            )
            prepared = wad_access.PreparedChampionWad(wad_path)
            real_open = Path.open
            read_opens = 0

            def counting_open(path: Path, *args: object, **kwargs: object):
                nonlocal read_opens
                mode = args[0] if args else kwargs.get("mode", "r")
                if path == wad_path and mode == "rb":
                    read_opens += 1
                return real_open(path, *args, **kwargs)

            with patch.object(Path, "open", counting_open):
                prepared.read_many((ANNIE_SKIN1, ANNIE_SKIN0))

            self.assertEqual(read_opens, 1)

    def test_unrelated_unsupported_chunk_does_not_block_supported_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "unrelated.wad"
            write_synthetic_wad(
                wad_path,
                [
                    SyntheticChunk(
                        wad_access.wad_path_hash(ANNIE_SKIN0),
                        b"PROPdata",
                        0,
                    ),
                    SyntheticChunk(
                        wad_access.wad_path_hash(ANNIE_SKIN1),
                        b"unused",
                        4,
                        subchunk_count=2,
                        subchunk_index=3,
                    ),
                ],
                version_minor=4,
            )
            prepared = wad_access.PreparedChampionWad(wad_path)

            self.assertEqual(
                prepared.read_many((ANNIE_SKIN0,)),
                {ANNIE_SKIN0: b"PROPdata"},
            )
            with self.assertRaises(wad_access.UnsupportedWadFeature):
                prepared.read_many((ANNIE_SKIN1,))

    def test_type_2_type_4_unknown_and_subchunk_metadata_are_typed(self) -> None:
        cases = (
            (2, 0, 0, "Satellite"),
            (4, 0, 0, "ZstdMulti"),
            (5, 0, 0, "unknown"),
            (0, 1, 0, "subchunk"),
            (3, 0, 2, "subchunk"),
        )
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            for index, (kind, count, sub_index, message) in enumerate(cases):
                with self.subTest(kind=kind, count=count, index=sub_index):
                    wad_path = root / f"unsupported-{index}.wad"
                    write_synthetic_wad(
                        wad_path,
                        [
                            SyntheticChunk(
                                wad_access.wad_path_hash(ANNIE_SKIN0),
                                b"payload",
                                kind,
                                subchunk_count=count,
                                subchunk_index=sub_index,
                            )
                        ],
                        version_minor=4,
                    )
                    prepared = wad_access.PreparedChampionWad(wad_path)
                    with self.assertRaisesRegex(
                        wad_access.UnsupportedWadFeature,
                        message,
                    ):
                        prepared.read_many((ANNIE_SKIN0,))

    def test_all_paths_are_preflighted_before_any_physical_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "preflight.wad"
            write_synthetic_wad(
                wad_path,
                [
                    SyntheticChunk(
                        wad_access.wad_path_hash(ANNIE_SKIN0),
                        b"PROPdata",
                        0,
                    ),
                    SyntheticChunk(
                        wad_access.wad_path_hash(ANNIE_SKIN1),
                        b"unsupported",
                        2,
                    ),
                ],
                version_minor=4,
            )
            observer = RecordingObserver()
            prepared = wad_access.PreparedChampionWad(
                wad_path,
                observer=observer,
            )

            with self.assertRaises(wad_access.UnsupportedWadFeature):
                prepared.read_many((ANNIE_SKIN0, ANNIE_SKIN1))
            self.assertEqual(observer.named("wad.read.chunk"), [])
            self.assertEqual(prepared.decoded_cache_size, 0)

    def test_declared_size_and_batch_limits_are_checked_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            cases = (
                (
                    root / "decompressed-limit.wad",
                    SyntheticChunk(
                        wad_access.wad_path_hash(ANNIE_SKIN0),
                        b"x",
                        0,
                        declared_decompressed_size=100,
                    ),
                    wad_access.WadReadLimits(max_required_bin_size=10),
                ),
                (
                    root / "compressed-limit.wad",
                    SyntheticChunk(
                        wad_access.wad_path_hash(ANNIE_SKIN0),
                        b"x" * 100,
                        0,
                    ),
                    wad_access.WadReadLimits(
                        max_compressed_chunk_size=10,
                        max_required_bin_size=200,
                    ),
                ),
                (
                    root / "batch-limit.wad",
                    SyntheticChunk(
                        wad_access.wad_path_hash(ANNIE_SKIN0),
                        b"x" * 20,
                        0,
                    ),
                    wad_access.WadReadLimits(
                        max_compressed_chunk_size=100,
                        max_required_bin_size=100,
                        max_read_batch_bytes=50,
                        decompressor_buffer_size=20,
                    ),
                ),
                (
                    root / "retained-limit.wad",
                    SyntheticChunk(
                        wad_access.wad_path_hash(ANNIE_SKIN0),
                        b"x" * 20,
                        0,
                    ),
                    wad_access.WadReadLimits(
                        max_compressed_chunk_size=100,
                        max_required_bin_size=100,
                        max_read_batch_bytes=100,
                        max_retained_output_bytes=10,
                        decompressor_buffer_size=20,
                    ),
                ),
            )
            for wad_path, chunk, limits in cases:
                with self.subTest(wad=wad_path.name):
                    write_synthetic_wad(
                        wad_path,
                        [chunk],
                        version_minor=4,
                    )
                    observer = RecordingObserver()
                    prepared = wad_access.PreparedChampionWad(
                        wad_path,
                        limits=limits,
                        observer=observer,
                    )
                    with self.assertRaises(wad_access.WadReadLimitExceeded):
                        prepared.read_many((ANNIE_SKIN0,))
                    self.assertEqual(observer.named("wad.read.chunk"), [])

    def test_raw_size_mismatch_is_typed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "raw-size.wad"
            write_synthetic_wad(
                wad_path,
                [
                    SyntheticChunk(
                        wad_access.wad_path_hash(ANNIE_SKIN0),
                        b"short",
                        0,
                        declared_decompressed_size=4,
                    )
                ],
                version_minor=4,
            )
            prepared = wad_access.PreparedChampionWad(wad_path)
            with self.assertRaises(wad_access.WadSizeMismatch):
                prepared.read_many((ANNIE_SKIN0,))

    def test_invalid_bin_payload_does_not_commit_partial_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "bin-signature.wad"
            write_paths(
                wad_path,
                [
                    (ANNIE_SKIN0, b"PROPvalid", 0),
                    (ANNIE_SKIN1, b"NOT-A-BIN", 0),
                ],
            )
            prepared = wad_access.PreparedChampionWad(wad_path)

            with self.assertRaises(wad_access.UnexpectedBinPayload):
                prepared.read_many(
                    (ANNIE_SKIN0, ANNIE_SKIN1),
                    validate_bin=True,
                )
            self.assertEqual(prepared.decoded_cache_size, 0)

    def test_observer_failures_do_not_change_reader_correctness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "observer.wad"
            write_paths(wad_path, [(ANNIE_SKIN0, b"data", 0)])

            def broken_observer(event: str, **fields: object) -> None:
                raise RuntimeError("metrics backend is unavailable")

            prepared = wad_access.PreparedChampionWad(
                wad_path,
                observer=broken_observer,
            )
            self.assertEqual(prepared.read_path(ANNIE_SKIN0), b"data")


class BoundedDecompressionTests(unittest.TestCase):
    def _prepared(
        self,
        root: Path,
        name: str,
        *,
        compression_type: int,
        payload: bytes,
        stored_payload: bytes | None = None,
        declared_size: int | None = None,
    ) -> wad_access.PreparedChampionWad:
        wad_path = root / name
        write_synthetic_wad(
            wad_path,
            [
                SyntheticChunk(
                    wad_access.wad_path_hash(ANNIE_SKIN0),
                    payload,
                    compression_type,
                    stored_payload=stored_payload,
                    declared_decompressed_size=declared_size,
                )
            ],
            version_minor=4,
        )
        return wad_access.PreparedChampionWad(
            wad_path,
            limits=wad_access.WadReadLimits(
                max_required_bin_size=2 * 1024 * 1024,
                max_compressed_chunk_size=2 * 1024 * 1024,
                max_read_batch_bytes=5 * 1024 * 1024,
                stream_buffer_size=7,
                decompressor_buffer_size=1024,
            ),
        )

    def test_invalid_gzip_and_zstd_are_typed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            for kind, raw in ((1, b"not-gzip"), (3, b"not-zstd")):
                with self.subTest(kind=kind):
                    prepared = self._prepared(
                        root,
                        f"invalid-{kind}.wad",
                        compression_type=kind,
                        payload=b"expected",
                        stored_payload=raw,
                    )
                    with self.assertRaises(wad_access.WadDecompressionFailed):
                        prepared.read_many((ANNIE_SKIN0,))

    def test_gzip_trailing_and_multiple_members_are_rejected(self) -> None:
        payload = b"gzip-payload"
        cases = (
            gzip.compress(payload, mtime=0) + b"trailing",
            gzip.compress(payload, mtime=0) + gzip.compress(b"", mtime=0),
        )
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            for index, stored in enumerate(cases):
                with self.subTest(index=index):
                    prepared = self._prepared(
                        root,
                        f"gzip-trailing-{index}.wad",
                        compression_type=1,
                        payload=payload,
                        stored_payload=stored,
                    )
                    with self.assertRaisesRegex(
                        wad_access.WadDecompressionFailed,
                        "additional gzip member|trailing",
                    ):
                        prepared.read_many((ANNIE_SKIN0,))

    def test_zstd_trailing_and_multiple_frames_are_rejected(self) -> None:
        payload = b"zstd-payload"
        compressor = zstd.ZstdCompressor()
        cases = (
            compressor.compress(payload) + b"trailing",
            compressor.compress(payload) + compressor.compress(b""),
        )
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            for index, stored in enumerate(cases):
                with self.subTest(index=index):
                    prepared = self._prepared(
                        root,
                        f"zstd-trailing-{index}.wad",
                        compression_type=3,
                        payload=payload,
                        stored_payload=stored,
                    )
                    with self.assertRaisesRegex(
                        wad_access.WadDecompressionFailed,
                        "additional Zstandard frame|trailing",
                    ):
                        prepared.read_many((ANNIE_SKIN0,))

    def test_gzip_bomb_stops_at_expected_size_plus_one(self) -> None:
        expanded = b"x" * (1024 * 1024)
        with tempfile.TemporaryDirectory() as temp_name:
            prepared = self._prepared(
                Path(temp_name),
                "gzip-bomb.wad",
                compression_type=1,
                payload=expanded,
                declared_size=16,
            )
            with self.assertRaises(wad_access.WadSizeMismatch) as raised:
                prepared.read_many((ANNIE_SKIN0,))
            self.assertEqual(raised.exception.actual, 17)

    def test_zstd_declared_frame_size_is_checked_before_decode(self) -> None:
        expanded = b"x" * (1024 * 1024)
        with tempfile.TemporaryDirectory() as temp_name:
            prepared = self._prepared(
                Path(temp_name),
                "zstd-frame-size.wad",
                compression_type=3,
                payload=expanded,
                declared_size=16,
            )
            with self.assertRaises(wad_access.WadSizeMismatch) as raised:
                prepared.read_many((ANNIE_SKIN0,))
            self.assertEqual(raised.exception.actual, len(expanded))

    def test_zstd_unknown_frame_size_bomb_stops_at_plus_one(self) -> None:
        expanded = b"x" * (1024 * 1024)
        stored = zstd.ZstdCompressor(write_content_size=False).compress(expanded)
        with tempfile.TemporaryDirectory() as temp_name:
            prepared = self._prepared(
                Path(temp_name),
                "zstd-unknown-size-bomb.wad",
                compression_type=3,
                payload=expanded,
                stored_payload=stored,
                declared_size=16,
            )
            with self.assertRaises(wad_access.WadSizeMismatch) as raised:
                prepared.read_many((ANNIE_SKIN0,))
            self.assertEqual(raised.exception.actual, 17)

    def test_zstd_window_is_bounded_before_decoder_allocation(self) -> None:
        # Standard frame magic, no content-size field, a 2 GiB window, and an
        # empty last raw block. The structural preflight rejects the window
        # before these bytes are handed to the native decoder.
        stored = b"\x28\xb5\x2f\xfd\x00\xa8\x01\x00\x00"
        with tempfile.TemporaryDirectory() as temp_name:
            prepared = self._prepared(
                Path(temp_name),
                "zstd-window.wad",
                compression_type=3,
                payload=b"",
                stored_payload=stored,
                declared_size=0,
            )
            with self.assertRaisesRegex(
                wad_access.WadReadLimitExceeded,
                "window",
            ):
                prepared.read_many((ANNIE_SKIN0,))

    def test_failed_later_chunk_does_not_commit_earlier_decode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "atomic-cache.wad"
            write_synthetic_wad(
                wad_path,
                [
                    SyntheticChunk(
                        wad_access.wad_path_hash(ANNIE_SKIN0),
                        b"good",
                        0,
                    ),
                    SyntheticChunk(
                        wad_access.wad_path_hash(ANNIE_SKIN1),
                        b"bad",
                        1,
                        stored_payload=b"not-gzip",
                    ),
                ],
                version_minor=4,
            )
            observer = RecordingObserver()
            prepared = wad_access.PreparedChampionWad(
                wad_path,
                observer=observer,
            )

            with self.assertRaises(wad_access.WadDecompressionFailed):
                prepared.read_many((ANNIE_SKIN0, ANNIE_SKIN1))
            self.assertEqual(prepared.decoded_cache_size, 0)
            first_attempt_reads = len(observer.named("wad.read.chunk"))
            self.assertEqual(
                len(observer.named("wad.read.chunk_attempt")),
                2,
            )
            failures = observer.named("wad.read.chunk_failure")
            self.assertEqual(len(failures), 1)
            self.assertEqual(
                failures[0]["error_type"],
                "WadDecompressionFailed",
            )

            self.assertEqual(prepared.read_path(ANNIE_SKIN0), b"good")
            self.assertEqual(
                len(observer.named("wad.read.chunk")),
                first_attempt_reads + 1,
            )
            self.assertEqual(
                len(observer.named("wad.read.chunk_attempt")),
                3,
            )


class WadIdentityTests(unittest.TestCase):
    def test_change_before_read_reindexes_missing_path_and_clears_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "changed-before-read.wad"
            write_paths(wad_path, [(ANNIE_SKIN0, b"old", 0)])
            prepared = wad_access.PreparedChampionWad(wad_path)
            self.assertEqual(prepared.read_path(ANNIE_SKIN0), b"old")

            write_paths(
                wad_path,
                [
                    (ANNIE_SKIN0, b"new-and-a-different-size", 0),
                    (ANNIE_SKIN1, b"newly-added", 0),
                ],
            )

            self.assertEqual(prepared.read_path(ANNIE_SKIN1), b"newly-added")
            self.assertEqual(
                prepared.read_path(ANNIE_SKIN0),
                b"new-and-a-different-size",
            )
            self.assertEqual(prepared.decoded_cache_size, 2)

    def test_one_source_change_rebuilds_and_retries_without_partial_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "changing-once.wad"
            write_paths(wad_path, [(ANNIE_SKIN0, b"first", 0)])
            touched = False

            def touch_once(event: str, **fields: object) -> None:
                nonlocal touched
                if event != "wad.read.chunk" or touched:
                    return
                touched = True
                stat_result = wad_path.stat()
                os.utime(
                    wad_path,
                    ns=(
                        stat_result.st_atime_ns,
                        stat_result.st_mtime_ns + 1_000_000,
                    ),
                )

            prepared = wad_access.PreparedChampionWad(
                wad_path,
                observer=touch_once,
            )

            self.assertEqual(prepared.read_path(ANNIE_SKIN0), b"first")
            self.assertTrue(touched)
            self.assertEqual(prepared.decoded_cache_size, 1)

    def test_two_source_changes_raise_after_one_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "changing-twice.wad"
            write_paths(wad_path, [(ANNIE_SKIN0, b"data", 0)])
            changes = 0

            def touch_every_read(event: str, **fields: object) -> None:
                nonlocal changes
                if event != "wad.read.chunk":
                    return
                changes += 1
                stat_result = wad_path.stat()
                os.utime(
                    wad_path,
                    ns=(
                        stat_result.st_atime_ns,
                        stat_result.st_mtime_ns + 1_000_000,
                    ),
                )

            prepared = wad_access.PreparedChampionWad(
                wad_path,
                observer=touch_every_read,
            )

            with self.assertRaises(wad_access.WadChangedDuringRead):
                prepared.read_many((ANNIE_SKIN0,))
            self.assertEqual(changes, 2)
            self.assertEqual(prepared.decoded_cache_size, 0)


if __name__ == "__main__":
    unittest.main()
