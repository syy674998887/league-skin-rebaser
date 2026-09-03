from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import golden_oracle
import script
from helpers.synthetic_wad import SyntheticChunk, write_synthetic_wad


def metric_total(
    recorder: script.OperationRecorder,
    name: str,
    **required_labels: object,
) -> int:
    expected = {key: str(value) for key, value in required_labels.items()}
    total = 0
    for item in recorder.records():
        if item["name"] != name:
            continue
        labels = item["labels"]
        if all(labels.get(key) == value for key, value in expected.items()):
            total += int(item["value"])
    return total


class OperationRecorderTests(unittest.TestCase):
    def test_scope_labels_are_aggregated_and_context_is_restored(self) -> None:
        recorder = script.OperationRecorder()

        with script.use_operations(recorder):
            with script.measurement_scope(champion="Annie"):
                script.count_operation("wad.copy.attempts", purpose="catalog")
                script.count_operation("wad.copy.attempts", purpose="catalog")
            script.count_operation("outside", 3)
        script.count_operation("ignored")

        self.assertEqual(
            metric_total(
                recorder,
                "wad.copy.attempts",
                champion="Annie",
                purpose="catalog",
            ),
            2,
        )
        self.assertEqual(metric_total(recorder, "outside"), 3)
        self.assertEqual(metric_total(recorder, "ignored"), 0)

    def test_negative_operation_value_is_rejected(self) -> None:
        recorder = script.OperationRecorder()
        with self.assertRaisesRegex(ValueError, "negative"):
            recorder.add("bytes", -1)

    def test_timing_samples_include_failure_scope(self) -> None:
        ticks = iter((10, 25))
        recorder = script.TimingRecorder(clock=lambda: next(ticks))

        with self.assertRaises(RuntimeError):
            with script.use_timings(recorder):
                with script.measurement_scope(
                    champion="Annie",
                    skin_number=1,
                    unit="annietibbers",
                ):
                    with script.timed_phase("unit"):
                        raise RuntimeError("boom")

        sample = recorder.samples[0]
        self.assertEqual(sample.error, "RuntimeError")
        self.assertEqual(
            dict(sample.scope),
            {
                "champion": "Annie",
                "skin_number": "1",
                "unit": "annietibbers",
            },
        )

    def test_external_process_failure_is_counted_and_timed_as_failure(self) -> None:
        operations = script.OperationRecorder()
        timings = script.TimingRecorder()
        failed = subprocess.CompletedProcess(
            args=["tool"],
            returncode=7,
            stdout="out",
            stderr="err",
        )

        with (
            script.use_operations(operations),
            script.use_timings(timings),
            patch.object(script.subprocess, "run", return_value=failed),
        ):
            with self.assertRaises(script.ExternalProcessFailed):
                script.run_external_process(
                    ["tool"],
                    tool="synthetic",
                    timing_phase="tool.synthetic",
                )

        self.assertEqual(
            metric_total(operations, "process.attempts", tool="synthetic"),
            1,
        )
        self.assertEqual(
            metric_total(operations, "process.failures", tool="synthetic"),
            1,
        )
        self.assertEqual(
            metric_total(operations, "process.successes", tool="synthetic"),
            0,
        )
        self.assertEqual(timings.samples[0].error, "ExternalProcessFailed")


class SyntheticWadBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        script._WAD_INDEX_CACHE.clear()

    def test_multi_entry_nonzero_metadata_preserves_payload_alignment(self) -> None:
        cases = (
            (0, 0x1234, (0, 1)),
            (3, 0x2345, (0, 1)),
            (4, 0x123456, (0, 3)),
        )
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            for minor, subchunk_index, compression_types in cases:
                with self.subTest(minor=minor):
                    if 3 in compression_types and script.zstd is None:
                        self.skipTest("zstandard is not installed")
                    chunks = [
                        SyntheticChunk(
                            path_hash=0x1000000000000000 + minor,
                            payload=f"first-v3.{minor}".encode(),
                            compression_type=compression_types[0],
                            duplicated=1,
                            subchunk_index=subchunk_index,
                            checksum=0xABCDEF,
                        ),
                        SyntheticChunk(
                            path_hash=0x2000000000000000 + minor,
                            payload=f"second-v3.{minor}".encode(),
                            compression_type=compression_types[1],
                            duplicated=1,
                            subchunk_index=subchunk_index,
                            checksum=0x123456,
                        ),
                    ]
                    wad_path = root / f"v3-{minor}.wad"
                    write_synthetic_wad(
                        wad_path,
                        chunks,
                        version_minor=minor,
                    )

                    index = script.parse_wad_index(wad_path)
                    self.assertEqual(set(index), {chunk.path_hash for chunk in chunks})
                    for chunk in chunks:
                        parsed = index[chunk.path_hash]
                        self.assertEqual(parsed.subchunk_index, subchunk_index)
                        with self.assertRaises(
                            script.UnsupportedWadFeature,
                        ):
                            script.read_wad_chunk(wad_path, chunk.path_hash)

    def test_physical_reads_and_bytes_are_distinct_from_index_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            wad_path = Path(temp_name) / "metrics.wad"
            payload = b"measured-payload"
            path_hash = 0x1020304050607080
            write_synthetic_wad(
                wad_path,
                [SyntheticChunk(path_hash, payload, 0)],
                version_minor=4,
            )
            operations = script.OperationRecorder()

            with script.use_operations(operations):
                self.assertEqual(script.read_wad_chunk(wad_path, path_hash), payload)
                self.assertIsNone(script.read_wad_chunk(wad_path, path_hash + 1))
                self.assertEqual(script.read_wad_chunk(wad_path, path_hash), payload)

            self.assertEqual(metric_total(operations, "wad.index.requests"), 3)
            self.assertEqual(metric_total(operations, "wad.index.builds"), 1)
            self.assertEqual(metric_total(operations, "wad.index.cache_hits"), 2)
            self.assertEqual(metric_total(operations, "wad.chunk.probes"), 3)
            self.assertEqual(metric_total(operations, "wad.chunk.missing"), 1)
            self.assertEqual(metric_total(operations, "wad.chunk.physical_reads"), 2)
            self.assertEqual(
                metric_total(operations, "wad.chunk.compressed_bytes"),
                len(payload) * 2,
            )
            self.assertEqual(
                metric_total(operations, "wad.chunk.decompressed_bytes"),
                len(payload) * 2,
            )


class GoldenOracleTests(unittest.TestCase):
    def context(self) -> golden_oracle.GoldenContext:
        return golden_oracle.GoldenContext(
            champion="Annie",
            skin_number=1,
            unit="annietibbers",
            stage="legacy-oracle",
        )

    def test_clear_and_hash_named_outputs_are_both_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            clear = root / "data" / "characters" / "annie" / "skins" / "skin0.bin"
            clear.parent.mkdir(parents=True)
            clear.write_bytes(b"clear")
            path_hash = 0x0123456789ABCDEF
            hashed = root / "unknown" / f"{path_hash:016x}.bin"
            hashed.parent.mkdir()
            hashed.write_bytes(b"hashed")
            index = golden_oracle.LegacyExtractIndex(root)

            self.assertEqual(
                index.read(
                    "data/characters/annie/skins/skin0.bin",
                    0xAAAAAAAAAAAAAAAA,
                    self.context(),
                ),
                b"clear",
            )
            self.assertEqual(
                index.read(
                    "data/characters/annietibbers/skins/skin0.bin",
                    path_hash,
                    self.context(),
                ),
                b"hashed",
            )

    def test_missing_ambiguous_and_conflicting_errors_include_full_context(self) -> None:
        cases = ("missing", "ambiguous", "conflicting")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                path_hash = 0x1111222233334444
                relative = "data/characters/annie/skins/skin1.bin"
                if case == "ambiguous":
                    for folder in ("a", "b"):
                        path = root / folder / f"{path_hash:016x}.bin"
                        path.parent.mkdir()
                        path.write_bytes(folder.encode())
                elif case == "conflicting":
                    clear = root.joinpath(*relative.split("/"))
                    clear.parent.mkdir(parents=True)
                    clear.write_bytes(b"clear")
                    hashed = root / "hashes" / f"{path_hash:016x}.bin"
                    hashed.parent.mkdir()
                    hashed.write_bytes(b"different")
                index = golden_oracle.LegacyExtractIndex(root)

                with self.assertRaises(golden_oracle.GoldenOracleError) as caught:
                    index.read(relative, path_hash, self.context())

                message = str(caught.exception)
                self.assertIn("champion=Annie", message)
                self.assertIn("skin=1", message)
                self.assertIn("unit=annietibbers", message)
                self.assertIn("stage=legacy-oracle", message)

    def test_legacy_paths_reject_absolute_drive_and_parent_forms(self) -> None:
        unsafe_paths = (
            "/absolute/path.bin",
            r"\rooted\path.bin",
            "C:/absolute/path.bin",
            r"C:drive-relative.bin",
            r"\\server\share\path.bin",
            "../parent.bin",
            "data/../parent.bin",
        )
        with tempfile.TemporaryDirectory() as temp_name:
            index = golden_oracle.LegacyExtractIndex(Path(temp_name))
            for path in unsafe_paths:
                with self.subTest(path=path), self.assertRaisesRegex(
                    golden_oracle.GoldenOracleError,
                    "unsafe legacy path",
                ):
                    index.read(
                        path,
                        0x1111222233334444,
                        self.context(),
                    )

    def test_pair_golden_compares_direct_and_legacy_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            base_hash = 0x0101010101010101
            target_hash = 0x0202020202020202
            (root / f"{base_hash:016x}.bin").write_bytes(b"base")
            (root / f"{target_hash:016x}.bin").write_bytes(b"target")
            index = golden_oracle.LegacyExtractIndex(root)

            pair = golden_oracle.build_pair_golden(
                index=index,
                context=self.context(),
                base_path="data/base.bin",
                base_hash=base_hash,
                base_direct=b"base",
                target_path="data/target.bin",
                target_hash=target_hash,
                target_direct=b"target",
            )

            self.assertEqual(
                pair.base_sha256,
                golden_oracle.sha256_hex(b"base"),
            )
            self.assertEqual(
                pair.target_sha256,
                golden_oracle.sha256_hex(b"target"),
            )


class MetricsReportTests(unittest.TestCase):
    def test_metrics_report_is_written_atomically_and_round_trips(self) -> None:
        timings = script.TimingRecorder(clock=lambda: 10)
        operations = script.OperationRecorder()
        operations.add("skins.selected", champion="Annie")
        operations.set_fact("selection", [{"champion": "Annie", "skinNumber": 1}])
        report = script.build_metrics_report(
            timings,
            operations,
            status="success",
            error=None,
        )

        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "nested" / "metrics.json"
            script.write_json_atomically(path, report)
            loaded = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(loaded["schemaVersion"], script.METRICS_SCHEMA_VERSION)
        self.assertEqual(loaded["status"], "success")
        self.assertEqual(loaded["facts"]["selection"][0]["champion"], "Annie")

    def test_noninteractive_cli_options_are_explicit(self) -> None:
        args = script.parse_args(
            [
                "--champion",
                "Annie",
                "--format",
                "zip",
                "--metrics-json",
                "metrics.json",
            ]
        )
        self.assertEqual(args.champion, "Annie")
        self.assertEqual(args.metrics_json, Path("metrics.json"))


if __name__ == "__main__":
    unittest.main()
