"""Resolve skin properties and the resolver they actually reference."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from .base_cache import BaseRebaseSnapshot
from .skin_bin import SkinBinError


def bin_key(value: int | str) -> int:
    """Ritobin may spell the same 32-bit key as a name or a number."""
    if type(value) is int and 0 <= value <= 0xFFFFFFFF:
        return value
    if not isinstance(value, str) or not value:
        raise SkinBinError(f"invalid BIN entry key {value!r}")
    result = 2166136261
    for byte in value.lower().encode("utf-8"):
        result = ((result ^ byte) * 16777619) & 0xFFFFFFFF
    return result


@dataclass
class SkinIdentity:
    skin: dict[str, Any]
    name_field: dict[str, Any] | None
    link_field: dict[str, Any] | None
    resolver: dict[str, Any] | None

    @classmethod
    def read(cls, data: dict[str, Any]) -> SkinIdentity:
        try:
            entries = data["entries"]["value"]["items"]
            skins = [e for e in entries if e["value"]["name"] == "SkinCharacterDataProperties"]
            if len(skins) != 1:
                raise SkinBinError(f"expected one SkinCharacterDataProperties, found {len(skins)}")
            skin = skins[0]
            bin_key(skin["key"])
            fields = {}
            for field in skin["value"]["items"]:
                key = field["key"]
                if key in ("ChampionSkinName", "mResourceResolver"):
                    if key in fields or field.get("value") is None:
                        raise SkinBinError(f"duplicate or invalid {key}")
                    fields[key] = field
            name = fields.get("ChampionSkinName")
            if name is not None and (not isinstance(name["value"], str) or not name["value"]):
                raise SkinBinError("invalid ChampionSkinName")
            link = fields.get("mResourceResolver")
            if name is None and link is None:
                raise SkinBinError("skin properties have neither a name nor a resource link")
            link_key = bin_key(link["value"]) if link is not None else None
            matches = [
                e for e in entries
                if e["value"]["name"] == "ResourceResolver" and bin_key(e["key"]) == link_key
            ]
            if len(matches) > 1:
                raise SkinBinError("multiple local ResourceResolver entries own the same key")
            return cls(skin, name, link, matches[0] if matches else None)
        except (KeyError, TypeError) as exc:
            raise SkinBinError(f"invalid skin identity record: {exc}") from exc

    @property
    def link(self) -> int | str | None:
        return self.link_field["value"] if self.link_field is not None else None

    def snapshot(self) -> BaseRebaseSnapshot:
        return BaseRebaseSnapshot.from_values(
            skin_entry_key=self.skin["key"],
            champion_skin_name=self.name_field["value"] if self.name_field is not None else None,
            resource_resolver=self.link,
            resolver_entry_key=self.resolver["key"] if self.resolver is not None else None,
        )

    def rebase(self, base: dict[str, Any], remaps: dict[int, int | str]) -> None:
        self.skin["key"] = base["skinEntryKey"]
        name = base["championSkinName"]
        fields = self.skin["value"]["items"]
        if self.name_field is not None:
            if name is None:
                fields.remove(self.name_field)
            else:
                self.name_field["value"] = name
        elif name is not None:
            fields.append({"key": "ChampionSkinName", "type": "string", "value": name})

        if self.link_field is not None:
            new_link = remaps.get(bin_key(self.link_field["value"]))
            if new_link is not None:
                self.link_field["value"] = new_link
                if self.resolver is not None:
                    self.resolver["key"] = new_link


def resolver_remaps(units: list[tuple[SkinIdentity, dict[str, Any]]]) -> dict[int, int | str]:
    """Map only keys owned by a target BIN in this one output WAD."""
    result: dict[int, int | str] = {}
    destinations: dict[int, int] = {}
    for target, base in units:
        if target.resolver is None:
            continue
        old_link = target.resolver["key"]
        old_key = bin_key(old_link)
        # An external base reference can belong to another unit or shared file.
        new_link = base["resolverEntryKey"] if base["resolverEntryKey"] is not None else old_link
        new_key = bin_key(new_link)
        if (old_key in result and bin_key(result[old_key]) != new_key) or (
            new_key in destinations and destinations[new_key] != old_key
        ):
            raise SkinBinError("ambiguous ResourceResolver mapping within the selected skin")
        result[old_key] = new_link
        destinations[new_key] = old_key
    return result


@lru_cache(maxsize=32)
def _peer_skin_keys(unit: str) -> dict[int, int]:
    # Local LCU skin numbers occupy the low three decimal digits of a skin ID.
    return {bin_key(f"characters/{unit}/skins/skin{n}/resources"): n for n in range(1, 1000)}


def peer_skin_path(unit: str, link: int | str) -> str | None:
    number = _peer_skin_keys(unit).get(bin_key(link))
    return f"data/characters/{unit}/skins/skin{number}.bin" if number is not None else None
