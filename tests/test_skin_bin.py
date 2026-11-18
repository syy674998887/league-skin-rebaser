import struct
import unittest

from tests.helpers.synthetic_prop import prop
from rebaser.skin_bin import RESOURCE_RESOLVER, SKIN_PROPERTIES, SkinBin, SkinBinError


class SkinBinTests(unittest.TestCase):
    def test_only_the_active_resolver_key_changes_and_original_maps_remain_exact(self):
        one = (RESOURCE_RESOLVER, 11, 1, b"first resource map")
        other = (RESOURCE_RESOLVER, 13, 1, b"second resource map")
        skin = (SKIN_PROPERTIES, 5, 0, b"")
        original = SkinBin.parse(prop([other, skin, one], links=[b"Original.bin"]))
        edited = SkinBin.parse(prop([
            (RESOURCE_RESOLVER, 19, 0, b""), (SKIN_PROPERTIES, 7, 0, b""),
            (RESOURCE_RESOLVER, 13, 0, b""),
        ], links=[b"Original.bin"]))
        output = original.restore_target(edited, extra_dependency="data/characters/peer/skins/skin1.bin")
        self.assertEqual(output, prop([
            other, (SKIN_PROPERTIES, 7, 0, b""), (RESOURCE_RESOLVER, 19, 1, one[3]),
        ], links=[b"Original.bin", b"data/characters/peer/skins/skin1.bin"]))
        plain = SkinBin.parse(prop([skin]))
        self.assertEqual(plain.restore_target(SkinBin.parse(plain.ritobin_view())), prop([skin]))

    def test_round_trip_preserves_wide_materials_and_entry_order(self):
        material = (0xFF9D3409, 30, 1, struct.pack("<IBQ", 0x8D39BDE6, 17, 0x123456789ABCDEF0))
        target = [(RESOURCE_RESOLVER, 10, 0, b""), material, (SKIN_PROPERTIES, 20, 0, b"")]
        rebased = [(SKIN_PROPERTIES, 21, 0, b""), (RESOURCE_RESOLVER, 11, 0, b"")]
        for version in (1, 2, 3):
            with self.subTest(version=version):
                opts = {"version": version, "links": (b"DATA/Test.bin",)}
                original = SkinBin.parse(prop(target, **opts))
                self.assertEqual(original.ritobin_view(), prop([target[0], target[2]], **opts))
                self.assertEqual(original.restore_target(SkinBin.parse(prop(rebased, **opts))),
                                 prop([rebased[1], material, rebased[0]], **opts))

    def test_rejects_invalid_envelopes_and_key_collisions(self):
        entries = [(SKIN_PROPERTIES, 1, 0, b""), (RESOURCE_RESOLVER, 2, 0, b"")]
        good = prop(entries)
        for malformed in (good[:-1], good + b"x", prop(entries, version=5),
                          prop([entries[0], entries[0]]), b"PROP" + b"\x02\0\0\0" + b"\xff" * 4):
            with self.subTest(data=malformed), self.assertRaises(SkinBinError):
                SkinBin.parse(malformed)
        source = SkinBin.parse(prop([*entries, (42, 3, 0, b"")]))
        for invalid in (prop(entries, links=(b"changed",)), prop(entries[:1]),
                        prop([entries[0], (RESOURCE_RESOLVER, 3, 0, b"")]),
                        prop([*entries, (42, 4, 0, b"")])):
            with self.subTest(converted=invalid), self.assertRaises(SkinBinError):
                source.restore_target(SkinBin.parse(invalid))
        with self.assertRaises(SkinBinError):
            SkinBin.parse(prop([entries[0], (SKIN_PROPERTIES, 7, 0, b"")])).ritobin_view()
