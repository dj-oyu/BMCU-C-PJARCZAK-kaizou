"""Persistent boot-session IDs that remain unique across soft resets."""

try:
    import ustruct as struct
except ImportError:
    import struct

MAGIC = b"BMS1"
RECORD_SIZE = 16
MASK64 = 0xFFFFFFFFFFFFFFFF
CHECK_XOR = 0xB4C05EED


def _decode(raw):
    if len(raw) != RECORD_SIZE or raw[:4] != MAGIC:
        return None
    value, check = struct.unpack_from(">QI", raw, 4)
    expected = ((value >> 32) ^ value ^ CHECK_XOR) & 0xFFFFFFFF
    return value if value and check == expected else None


def _read(path):
    try:
        with open(path, "rb") as source:
            return _decode(source.read())
    except OSError:
        return None


def next_boot_id(random_bytes, paths=("bmcu_boot.a", "bmcu_boot.b")):
    """Return and persist the next nonzero 64-bit boot session ID."""
    values = tuple(value for value in (_read(paths[0]), _read(paths[1]))
                   if value is not None)
    if values:
        value = (max(values) + 1) & MASK64
    else:
        value = int.from_bytes(random_bytes, "big") & MASK64
    if not value:
        value = 1
    check = ((value >> 32) ^ value ^ CHECK_XOR) & 0xFFFFFFFF
    record = bytearray(RECORD_SIZE)
    record[:4] = MAGIC
    struct.pack_into(">QI", record, 4, value, check)
    target = paths[value & 1]
    with open(target, "wb") as sink:
        sink.write(record)
        if hasattr(sink, "flush"):
            sink.flush()
    return value
