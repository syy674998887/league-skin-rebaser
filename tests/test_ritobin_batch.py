from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rebaser.ritobin_batch import (
    RitobinBatchError,
    RitobinBatchItem,
    partition_batch_items,
    run_bounded_recursive_batches,
)


class RitobinBatchTests(unittest.TestCase):
    def test_partitions_by_file_and_byte_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            items = []
            for index, size in enumerate((3, 3, 3, 8)):
                source = root / f"{index}.bin"
                source.write_bytes(b"x" * size)
                items.append(
                    RitobinBatchItem(
                        source,
                        root / "out" / f"{index}.json",
                        f"champion/skin-{index}/unit/target.bin",
                    )
                )
            batches = partition_batch_items(
                items,
                max_files=2,
                max_bytes=10,
            )

        self.assertEqual([len(batch) for batch in batches], [2, 1, 1])

    def test_large_allowed_input_is_isolated_from_adjacent_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            items = []
            for index, size in enumerate((3, 8, 2)):
                source = root / f"{index}.bin"
                source.write_bytes(b"x" * size)
                items.append(
                    RitobinBatchItem(
                        source,
                        root / "out" / f"{index}.json",
                        f"champion/skin-{index}/unit/target.bin",
                    )
                )
            batches = partition_batch_items(
                items,
                max_files=10,
                max_bytes=10,
                isolate_above_bytes=6,
            )

        self.assertEqual([len(batch) for batch in batches], [1, 1, 1])
        self.assertEqual(
            [[item.source.name for item in batch] for batch in batches],
            [["0.bin"], ["1.bin"], ["2.bin"]],
        )

    def test_isolated_file_threshold_must_fit_inside_batch_limit(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "isolated-file threshold",
        ):
            partition_batch_items(
                (),
                max_files=1,
                max_bytes=10,
                isolate_above_bytes=11,
            )

    def test_exact_recursive_outputs_are_published(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "source.bin"
            source.write_bytes(b"input")
            destination = root / "final" / "target.json"
            item = RitobinBatchItem(
                source,
                destination,
                "champion-1/skin-1/annie/target.bin",
            )

            def runner(
                input_root: Path,
                output_root: Path,
                _in_format: str,
                _out_format: str,
            ) -> None:
                self.assertTrue(
                    (
                        input_root
                        / "champion-1"
                        / "skin-1"
                        / "annie"
                        / "target.bin"
                    ).is_file()
                )
                output = (
                    output_root
                    / "champion-1"
                    / "skin-1"
                    / "annie"
                    / "target.json"
                )
                output.parent.mkdir(parents=True)
                output.write_bytes(b"converted")

            report = run_bounded_recursive_batches(
                [item],
                in_format="bin",
                out_format="json",
                workspace=root,
                max_files=10,
                max_bytes=100,
                run_batch=runner,
                diagnose=lambda *_: None,
            )
            converted = destination.read_bytes()

        self.assertEqual(report.batch_count, 1)
        self.assertEqual(report.file_count, 1)
        self.assertEqual(report.input_bytes, 5)
        self.assertEqual(report.batch_file_counts, (1,))
        self.assertEqual(report.batch_input_bytes, (5,))
        self.assertEqual(report.max_batch_files, 1)
        self.assertEqual(report.max_batch_input_bytes, 5)
        self.assertEqual(converted, b"converted")

    def test_exit_zero_partial_output_is_rejected_without_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            items = []
            for index in (1, 2):
                source = root / f"{index}.json"
                source.write_bytes(b"{}")
                destination = root / "final" / f"{index}.bin"
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"old")
                items.append(
                    RitobinBatchItem(
                        source,
                        destination,
                        f"champion/skin-{index}/unit/modified.json",
                    )
                )

            def partial(
                _input_root: Path,
                output_root: Path,
                _in_format: str,
                _out_format: str,
            ) -> None:
                output = (
                    output_root
                    / "champion"
                    / "skin-1"
                    / "unit"
                    / "modified.bin"
                )
                output.parent.mkdir(parents=True)
                output.write_bytes(b"partial")

            diagnosed: list[str] = []

            def diagnose(
                item: RitobinBatchItem,
                _in_format: str,
                _out_format: str,
            ) -> str | None:
                diagnosed.append(item.relative_path)
                return "bad json" if "skin-2" in item.relative_path else None

            with self.assertRaisesRegex(
                RitobinBatchError,
                "missing outputs.*individual failures.*bad json",
            ):
                run_bounded_recursive_batches(
                    items,
                    in_format="json",
                    out_format="bin",
                    workspace=root,
                    max_files=10,
                    max_bytes=100,
                    run_batch=partial,
                    diagnose=diagnose,
                )

            self.assertEqual(
                [item.destination.read_bytes() for item in items],
                [b"old", b"old"],
            )
            self.assertEqual(len(diagnosed), 2)

    def test_rejects_unsafe_and_oversized_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "source.bin"
            source.write_bytes(b"12345")
            unsafe = RitobinBatchItem(
                source,
                root / "out.json",
                "../escape.bin",
            )
            with self.assertRaisesRegex(RitobinBatchError, "unsafe"):
                run_bounded_recursive_batches(
                    [unsafe],
                    in_format="bin",
                    out_format="json",
                    workspace=root,
                    max_files=1,
                    max_bytes=10,
                    run_batch=lambda *_: None,
                    diagnose=lambda *_: None,
                )

            oversized = RitobinBatchItem(
                source,
                root / "out.json",
                "safe/input.bin",
            )
            with self.assertRaisesRegex(RitobinBatchError, "limit is 4"):
                run_bounded_recursive_batches(
                    [oversized],
                    in_format="bin",
                    out_format="json",
                    workspace=root,
                    max_files=1,
                    max_bytes=4,
                    run_batch=lambda *_: None,
                    diagnose=lambda *_: None,
                )


if __name__ == "__main__":
    unittest.main()
