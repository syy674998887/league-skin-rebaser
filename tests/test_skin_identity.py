import unittest
from unittest.mock import Mock

from rebaser.app import verified_peer_dependency
from rebaser.skin_bin import RESOURCE_RESOLVER
from rebaser.skin_bin import SkinBinError
from rebaser.skin_identity import SkinIdentity, bin_key, peer_skin_path, resolver_remaps
from tests.helpers.synthetic_prop import prop


def identity(skin_key, name, link, *resolver_keys):
    fields = [{"key": "content", "value": "keep"}]
    if name is not None:
        fields.append({"key": "ChampionSkinName", "type": "string", "value": name})
    if link is not None:
        fields.append({"key": "mResourceResolver", "type": "link", "value": link})
    entries = [{"key": skin_key, "value": {"name": "SkinCharacterDataProperties", "items": fields}}]
    entries.extend({"key": k, "value": {"name": "ResourceResolver", "items": []}} for k in resolver_keys)
    return SkinIdentity.read({"entries": {"value": {"items": entries}}}), entries


class SkinIdentityTests(unittest.TestCase):
    def test_absent_name_and_link_follow_the_source_records(self):
        base, _ = identity(1, None, 71, 71)
        target, entries = identity(2, "DJ", 72, 72)
        target.rebase(base.snapshot().values(), {})
        self.assertEqual(entries[0]["key"], 1)
        self.assertEqual([field["key"] for field in entries[0]["value"]["items"]], ["content", "mResourceResolver"])
        base, _ = identity(1, "Base", None)
        target, entries = identity(2, None, 72)
        target.rebase(base.snapshot().values(), {})
        self.assertEqual(entries[0]["value"]["items"][-1], {"key": "ChampionSkinName", "type": "string", "value": "Base"})
        with self.assertRaisesRegex(SkinBinError, "neither a name nor"):
            identity(3, None, None)

    def test_selects_the_linked_resolver_and_updates_references_from_peer_units(self):
        base, _ = identity(1, "Base", "base/map", 300, bin_key("base/map"))
        target, entries = identity(2, "Target", "target/map", 301, bin_key("target/map"))
        peer_base, _ = identity(3, "PeerBase", "base/map")
        peer, _ = identity(4, "PeerTarget", "TARGET/MAP")
        units = [(peer, peer_base.snapshot().values()), (target, base.snapshot().values())]
        remaps = resolver_remaps(units)
        for current, snapshot in units:
            current.rebase(snapshot, remaps)
        self.assertEqual(entries[1]["key"], 301)
        self.assertEqual(entries[2]["key"], bin_key("base/map"))
        self.assertEqual(target.link, entries[2]["key"])
        self.assertEqual(peer.link, target.link)
        self.assertIsNone(peer.resolver)

    def test_preserves_external_or_unowned_keys_and_rejects_ambiguous_owners(self):
        base, _ = identity(1, "Base", 500)
        local, _ = identity(2, "Other", 600, 600)
        external, entries = identity(3, "Shared", 700, 800)
        units = [(local, base.snapshot().values()), (external, base.snapshot().values())]
        remaps = resolver_remaps(units)
        for current, snapshot in units:
            current.rebase(snapshot, remaps)
        self.assertEqual(local.link, 600)
        self.assertEqual(external.link, 700)
        self.assertEqual(entries[1]["key"], 800)
        with self.assertRaisesRegex(SkinBinError, "same key"):
            identity(1, "Base", "same", "SAME", bin_key("same"))
        a, _ = identity(1, "A", 1, 1)
        b, _ = identity(2, "B", 2, 2)
        with self.assertRaisesRegex(SkinBinError, "ambiguous"):
            resolver_remaps([(a, a.snapshot().values()), (b, a.snapshot().values())])

    def test_cross_skin_provider_matches_named_and_hashed_links(self):
        name = "Characters/Swain/Skins/Skin1/Resources"
        for link in (name, bin_key(name)):
            self.assertEqual(peer_skin_path("swain", link), "data/characters/swain/skins/skin1.bin")
            self.assertIsNone(peer_skin_path("swaindemonform", link))

    def test_new_dependency_requires_the_referenced_key_in_the_requested_source_bin(self):
        link = "Characters/Swain/Skins/Skin1/Resources"
        path = "data/characters/swain/skins/skin1.bin"
        item, session = Mock(), Mock()
        data = {"linked": {"value": {"items": ["DATA/Characters/Swain/Swain.bin"]}}}
        session.prepared.read_path.return_value = prop([(RESOURCE_RESOLVER, 37, 0, b"")])
        with self.assertRaisesRegex(SkinBinError, "does not provide resolver"):
            verified_peer_dependency(item, data, link, "swain", session)
        session.prepared.read_path.return_value = prop([(RESOURCE_RESOLVER, bin_key(link), 0, b"")])
        self.assertEqual(verified_peer_dependency(item, data, link, "swain", session), path)
        data["linked"]["value"]["items"].append(path.upper())
        self.assertIsNone(verified_peer_dependency(item, data, link, "swain", session))
