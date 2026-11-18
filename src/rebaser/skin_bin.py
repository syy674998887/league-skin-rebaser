"""Preserve a skin PROP while editing its identity and resolver keys.

Entry types and entry lengths are part of the PROP envelope. A material or
particle entry can therefore be copied byte-for-byte without interpreting its
properties, including the wide material-name hashes introduced in 16.21.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


# Lowercase FNV-1a hashes of SkinCharacterDataProperties and ResourceResolver.
SKIN_PROPERTIES = 0x9B67E9F6
RESOURCE_RESOLVER = 0xEF3A0F33
_IDENTITY_TYPES = frozenset({SKIN_PROPERTIES, RESOURCE_RESOLVER})


class SkinBinError(ValueError):
    """An input cannot be safely split or reassembled as a skin PROP."""


@dataclass(frozen=True)
class SkinBin:
    header: bytes  # magic, version and the original dependency list
    entries: tuple[tuple[int, bytes], ...]  # class hash and length-prefixed body

    @classmethod
    def parse(cls, data: bytes) -> SkinBin:
        if len(data) < 12 or data[:4] != b"PROP":
            raise SkinBinError("missing or truncated PROP header")
        version = struct.unpack_from("<I", data, 4)[0]
        if version not in (1, 2, 3):
            raise SkinBinError(f"unsupported skin PROP version {version}")
        pos = 8
        if version >= 2:
            links = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            for _ in range(links):
                if pos + 2 > len(data):
                    raise SkinBinError("truncated PROP dependency length")
                length = struct.unpack_from("<H", data, pos)[0]
                pos += 2 + length
                if pos > len(data):
                    raise SkinBinError("truncated PROP dependency")
        header = data[:pos]
        if pos + 4 > len(data):
            raise SkinBinError("missing PROP entry count")
        count = struct.unpack_from("<I", data, pos)[0]
        types_start = pos + 4
        # Each entry needs a 4-byte class plus a length, key and field count.
        if count > (len(data) - types_start) // 14:
            raise SkinBinError("truncated PROP entry table")
        pos = types_start + 4 * count
        entries = []
        keys = set()
        for (kind,) in struct.iter_unpack("<I", data[types_start:pos]):
            if pos + 10 > len(data):
                raise SkinBinError("truncated PROP entry header")
            length, key = struct.unpack_from("<II", data, pos)
            end = pos + 4 + length
            if length < 6 or end > len(data):
                raise SkinBinError("invalid PROP entry length")
            if key in keys:
                raise SkinBinError(f"duplicate PROP entry key {key:08x}")
            keys.add(key)
            entries.append((kind, data[pos:end]))
            pos = end
        if pos != len(data):
            raise SkinBinError("unexpected bytes after PROP entries")
        return cls(header, tuple(entries))

    def _encode(self, entries: tuple[tuple[int, bytes], ...], *, header: bytes | None = None) -> bytes:
        types = b"".join(struct.pack("<I", kind) for kind, _ in entries)
        bodies = b"".join(body for _, body in entries)
        return b"".join((self.header if header is None else header, struct.pack("<I", len(entries)), types, bodies))

    def _skin_body(self) -> bytes:
        skins = [body for kind, body in self.entries if kind == SKIN_PROPERTIES]
        if len(skins) != 1:
            raise SkinBinError(f"expected one SkinCharacterDataProperties, found {len(skins)}")
        return skins[0]

    def resolver_keys(self) -> set[int]:
        return {
            struct.unpack_from("<I", body, 4)[0]
            for kind, body in self.entries if kind == RESOURCE_RESOLVER
        }

    def ritobin_view(self) -> bytes:
        self._skin_body()
        # Only a resolver's key can change. Keep its contents in the source BIN.
        entries = tuple(
            (kind, body if kind == SKIN_PROPERTIES else struct.pack("<I", 6) + body[4:8] + b"\0\0")
            for kind, body in self.entries if kind in _IDENTITY_TYPES
        )
        return self._encode(entries)

    def restore_target(self, converted: SkinBin, *, extra_dependency: str | None = None) -> bytes:
        self._skin_body()
        skin = converted._skin_body()
        if self.header != converted.header or any(kind not in _IDENTITY_TYPES for kind, _ in converted.entries):
            raise SkinBinError("converted PROP changed the original header or introduced entries")
        before, after = self.resolver_keys(), converted.resolver_keys()
        removed, added = before - after, after - before
        if len(before) != len(after) or len(removed) > 1:
            raise SkinBinError("converted PROP changed the set of resolvers beyond one rekey")
        entries = []
        for kind, body in self.entries:
            if kind == SKIN_PROPERTIES:
                body = skin
            elif kind == RESOURCE_RESOLVER and struct.unpack_from("<I", body, 4)[0] in removed:
                body = body[:4] + struct.pack("<I", next(iter(added))) + body[8:]
            entries.append((kind, body))
        if len({body[4:8] for _, body in entries}) != len(entries):
            raise SkinBinError("rebased identity collides with another target entry")
        header = self.header
        if extra_dependency is not None:
            if len(header) < 12:
                raise SkinBinError("this PROP version cannot link an external resolver provider")
            path = extra_dependency.encode("utf-8")
            count = struct.unpack_from("<I", header, 8)[0]
            header = b"".join((header[:8], struct.pack("<I", count + 1), header[12:], struct.pack("<H", len(path)), path))
        return self._encode(tuple(entries), header=header)
