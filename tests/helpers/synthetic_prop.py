import struct


def prop(entries, *, version=3, links=()):
    """Build a PROP envelope from (class, key, field count, field bytes)."""
    data = bytearray(b"PROP" + struct.pack("<I", version))
    if version >= 2:
        data.extend(struct.pack("<I", len(links)))
        for link in links:
            data.extend(struct.pack("<H", len(link)) + link)
    data.extend(struct.pack("<I", len(entries)))
    for kind, _, _, _ in entries:
        data.extend(struct.pack("<I", kind))
    for _, key, count, fields in entries:
        data.extend(struct.pack("<IIH", len(fields) + 6, key, count))
        data.extend(fields)
    return bytes(data)
