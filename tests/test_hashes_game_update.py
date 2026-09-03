from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import URLError

import xxhash

from rebaser import hashes_game_update as updater


ANNIE = "data/characters/annie/skins/skin0.bin"
LOCKE = "data/characters/locke/skins/skin0.bin"
ZOE = "data/characters/zoe/skins/skin0.bin"
KNOWN_EXCEPTION = (
    b"10e25123126b83b0 "
    b"assets/perks/styles/style5/futuresmarket/futures.ps4.market.dds\n"
)
SOURCE_URL = "https://example.test/hashes.game.txt"


def dictionary_line(path: str) -> bytes:
    path_bytes = path.encode("utf-8")
    path_hash = xxhash.xxh64(path_bytes, seed=0).intdigest()
    return f"{path_hash:016x} {path}\n".encode("utf-8")


def test_policy(*, min_rows: int = 2) -> updater.HashValidationPolicy:
    return updater.HashValidationPolicy(
        min_bytes=1,
        max_bytes=1024 * 1024,
        min_rows=min_rows,
        max_line_bytes=1024,
        required_paths=(ANNIE, LOCKE),
    )


class FakeResponse(io.BytesIO):
    def __init__(
        self,
        body: bytes,
        *,
        url: str,
        etag: str,
        last_modified: str,
    ) -> None:
        super().__init__(body)
        self.status = 200
        self.headers = {
            "ETag": etag,
            "Last-Modified": last_modified,
            "Content-Length": str(len(body)),
        }
        self._url = url

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class RecordingOpener:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.etag = '"v1"'
        self.last_modified = "Thu, 30 Jul 2026 20:46:30 GMT"
        self.calls: list[tuple[str, float | None]] = []

    def __call__(
        self,
        request: object,
        *,
        timeout: float | None = None,
    ) -> FakeResponse:
        method = request.get_method()  # type: ignore[attr-defined]
        self.calls.append((method, timeout))
        body = b"" if method == "HEAD" else self.body
        response = FakeResponse(
            body,
            url=SOURCE_URL,
            etag=self.etag,
            last_modified=self.last_modified,
        )
        if method == "HEAD":
            response.headers["Content-Length"] = str(len(self.body))
        return response


class HashUpdateTests(unittest.TestCase):
    def test_content_length_alone_is_not_a_remote_validator(self) -> None:
        remote = updater.RemoteValidators(
            etag=None,
            last_modified=None,
            content_length=123,
        )
        self.assertFalse(
            updater._remote_matches_state(
                remote,
                {"contentLength": 123},
            )
        )

    def test_content_length_supplements_etag_validation(self) -> None:
        source = {"etag": '"v1"', "contentLength": 123}
        self.assertTrue(
            updater._remote_matches_state(
                updater.RemoteValidators('"v1"', None, 123),
                source,
            )
        )
        self.assertFalse(
            updater._remote_matches_state(
                updater.RemoteValidators('"v1"', None, 124),
                source,
            )
        )

    def test_auto_downloads_once_then_uses_fast_remote_validation(self) -> None:
        body = dictionary_line(ANNIE) + dictionary_line(LOCKE)
        opener = RecordingOpener(body)
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            destination = root / "tools" / "hashes.game.txt"
            state_path = root / "cache" / "hashes-game-update.json"

            first = updater.ensure_latest_hashes_game(
                destination,
                state_path,
                mode="auto",
                source_url=SOURCE_URL,
                policy=test_policy(),
                opener=opener,
                retries=1,
            )

            self.assertEqual(first.action, "updated")
            self.assertEqual(destination.read_bytes(), body)
            self.assertEqual([method for method, _ in opener.calls], ["HEAD", "GET"])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["source"]["etag"], '"v1"')
            self.assertEqual(state["local"]["rows"], 2)
            self.assertEqual(state["local"]["sha256"], first.sha256)

            second = updater.ensure_latest_hashes_game(
                destination,
                state_path,
                mode="auto",
                source_url=SOURCE_URL,
                policy=test_policy(),
                opener=opener,
                retries=1,
            )

            self.assertEqual(second.action, "current")
            self.assertEqual(
                [method for method, _ in opener.calls],
                ["HEAD", "GET", "HEAD"],
            )
            self.assertEqual(second.sha256, first.sha256)

    def test_remote_validator_change_replaces_dictionary(self) -> None:
        first_body = dictionary_line(ANNIE) + dictionary_line(LOCKE)
        opener = RecordingOpener(first_body)
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            destination = root / "hashes.game.txt"
            state_path = root / "state.json"
            updater.ensure_latest_hashes_game(
                destination,
                state_path,
                mode="auto",
                source_url=SOURCE_URL,
                policy=test_policy(),
                opener=opener,
                retries=1,
            )

            second_body = first_body + dictionary_line(ZOE)
            opener.body = second_body
            opener.etag = '"v2"'
            opener.last_modified = "Thu, 30 Jul 2026 21:00:00 GMT"
            result = updater.ensure_latest_hashes_game(
                destination,
                state_path,
                mode="auto",
                source_url=SOURCE_URL,
                policy=test_policy(),
                opener=opener,
                retries=1,
            )

            self.assertEqual(result.action, "updated")
            self.assertEqual(result.row_count, 3)
            self.assertEqual(destination.read_bytes(), second_body)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["source"]["etag"], '"v2"')

    def test_invalid_download_never_replaces_existing_dictionary(self) -> None:
        invalid = b"0000000000000000 " + ANNIE.encode("utf-8") + b"\n"
        invalid += dictionary_line(LOCKE)
        opener = RecordingOpener(invalid)
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            destination = root / "hashes.game.txt"
            state_path = root / "state.json"
            destination.write_bytes(b"known-good-local")

            with self.assertRaisesRegex(
                updater.HashValidationError,
                "hash mismatch",
            ):
                updater.ensure_latest_hashes_game(
                    destination,
                    state_path,
                    mode="auto",
                    source_url=SOURCE_URL,
                    policy=test_policy(),
                    opener=opener,
                    retries=1,
                )

            self.assertEqual(destination.read_bytes(), b"known-good-local")
            self.assertFalse(state_path.exists())
            self.assertEqual(
                list(root.glob(".hashes.game.txt.*.download")),
                [],
            )

    def test_exact_known_upstream_hash_exception_is_accepted(self) -> None:
        body = dictionary_line(ANNIE) + dictionary_line(LOCKE) + KNOWN_EXCEPTION
        opener = RecordingOpener(body)
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            result = updater.ensure_latest_hashes_game(
                root / "hashes.game.txt",
                root / "state.json",
                mode="auto",
                source_url=SOURCE_URL,
                policy=test_policy(min_rows=3),
                opener=opener,
                retries=1,
            )

            self.assertEqual(result.action, "updated")
            self.assertEqual(result.row_count, 3)

    def test_network_failure_is_fail_closed(self) -> None:
        calls = 0

        def fail_open(_request: object, *, timeout: float) -> object:
            nonlocal calls
            calls += 1
            raise URLError("offline")

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            destination = root / "hashes.game.txt"
            destination.write_bytes(b"stale")
            with self.assertRaisesRegex(
                updater.HashNetworkError,
                "could not verify newest",
            ):
                updater.ensure_latest_hashes_game(
                    destination,
                    root / "state.json",
                    mode="auto",
                    source_url=SOURCE_URL,
                    policy=test_policy(),
                    opener=fail_open,
                    retries=1,
                )

            self.assertEqual(calls, 1)
            self.assertEqual(destination.read_bytes(), b"stale")

    def test_never_mode_performs_no_network_or_validation(self) -> None:
        def unexpected_open(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("never mode must not open the network")

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            destination = root / "missing-hashes.game.txt"
            result = updater.ensure_latest_hashes_game(
                destination,
                root / "state.json",
                mode="never",
                source_url=SOURCE_URL,
                policy=test_policy(),
                opener=unexpected_open,
            )

            self.assertEqual(result.action, "skipped")
            self.assertIsNone(result.size)


if __name__ == "__main__":
    unittest.main()
