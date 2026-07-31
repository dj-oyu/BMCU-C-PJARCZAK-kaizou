"""BMJ1 append-only journal codec and bounded staged writer."""

try:
    import ustruct as struct
except ImportError:
    import struct
try:
    import ubinascii as binascii
except ImportError:
    import binascii

import bmcu_binary_constants as C
from byte_ring import ByteRing, RingFull

MAGIC = b"BMJ1"
VERSION = 1
SEGMENT_HEADER_SIZE = 32
RECORD_HEADER_SIZE = 24
RECORD_MIN_SIZE = 28
RECORD_MAX_SIZE = RECORD_HEADER_SIZE + C.MAX_PAYLOAD_SIZE + 4
CHECKPOINT_MAGIC = b"BMA1"


class JournalError(ValueError):
    pass


def _crc32(data):
    return binascii.crc32(data) & 0xFFFFFFFF


def write_segment_header(out, pico_boot_id, segment_sequence, created_at_us):
    if len(out) < SEGMENT_HEADER_SIZE:
        raise JournalError("segment header buffer too small")
    struct.pack_into(">4sB3sQIQ", out, 0, MAGIC, VERSION, b"\0\0\0",
                     pico_boot_id, segment_sequence, created_at_us)
    struct.pack_into(">I", out, 28, _crc32(memoryview(out)[:28]))
    return SEGMENT_HEADER_SIZE


def parse_segment_header(data):
    if len(data) < SEGMENT_HEADER_SIZE:
        raise JournalError("truncated segment header")
    magic, version, reserved, boot_id, sequence, created = struct.unpack_from(
        ">4sB3sQIQ", data, 0)
    expected = struct.unpack_from(">I", data, 28)[0]
    if magic != MAGIC or version != VERSION or reserved != b"\0\0\0":
        raise JournalError("invalid segment header")
    if _crc32(memoryview(data)[:28]) != expected:
        raise JournalError("segment header CRC mismatch")
    return boot_id, sequence, created


def write_record(out, record_type, flags, link_index, transport_sequence,
                 received_at_us, payload):
    total = RECORD_HEADER_SIZE + len(payload) + 4
    if total > RECORD_MAX_SIZE or total > len(out):
        raise JournalError("journal record too large")
    struct.pack_into(">HBBB3sQQ", out, 0, total, record_type, flags,
                     link_index, b"\0\0\0", transport_sequence, received_at_us)
    memoryview(out)[RECORD_HEADER_SIZE:
                    RECORD_HEADER_SIZE + len(payload)] = payload
    struct.pack_into(">I", out, total - 4,
                     _crc32(memoryview(out)[:total - 4]))
    return total


def parse_record(data, offset=0):
    if len(data) - offset < 2:
        return None
    total = struct.unpack_from(">H", data, offset)[0]
    if total < RECORD_MIN_SIZE or total > RECORD_MAX_SIZE:
        raise JournalError("invalid journal record length")
    if len(data) - offset < total:
        return None
    record_type, flags, link_index, reserved, sequence, received = \
        struct.unpack_from(">BBB3sQQ", data, offset + 2)
    if reserved != b"\0\0\0":
        raise JournalError("invalid journal reserved bytes")
    expected = struct.unpack_from(">I", data, offset + total - 4)[0]
    if _crc32(memoryview(data)[offset:offset + total - 4]) != expected:
        raise JournalError("journal record CRC mismatch")
    payload = memoryview(data)[offset + RECORD_HEADER_SIZE:
                               offset + total - 4]
    return total, record_type, flags, link_index, sequence, received, payload


class JournalStager:
    """Preallocated staging slots; disk I/O happens only in ``flush_one``."""

    def __init__(self, storage):
        self.ring = ByteRing(storage, RECORD_MAX_SIZE)
        self.scratch = bytearray(RECORD_MAX_SIZE)
        self.next_local_sequence = 1

    def stage(self, record_type, flags, link_index, transport_sequence,
              received_at_us, payload):
        # Keep one slot available for a critical BMCU event when ordinary
        # journal traffic temporarily outruns flash writes.
        if (self.ring.capacity > 1 and
                not flags & C.FLAG_CRITICAL and
                len(self.ring) >= self.ring.capacity - 1):
            raise RingFull("journal critical reserve")
        size = write_record(self.scratch, record_type, flags, link_index,
                            transport_sequence, received_at_us, payload)
        self.ring.append(self.next_local_sequence,
                         memoryview(self.scratch)[:size],
                         protected=True)
        self.next_local_sequence += 1

    def flush_one(self, file_object):
        current = self.ring.peek()
        if current is None:
            return 0
        _, record, _, _ = current
        written = file_object.write(record)
        if written is None:
            written = len(record)
        if written != len(record):
            raise OSError("partial journal write")
        self.ring.release_through(current[0])
        return written


def recover_records(file_object, scratch):
    """Yield valid records and stop at the first torn/corrupt tail."""
    header = file_object.read(SEGMENT_HEADER_SIZE)
    boot_id, segment_sequence, created_at_us = parse_segment_header(header)
    while True:
        prefix = file_object.read(2)
        if not prefix:
            break
        if len(prefix) != 2:
            break
        total = struct.unpack_from(">H", prefix, 0)[0]
        if total < RECORD_MIN_SIZE or total > RECORD_MAX_SIZE:
            break
        remaining = file_object.read(total - 2)
        if len(remaining) != total - 2:
            break
        scratch[:2] = prefix
        memoryview(scratch)[2:total] = remaining
        try:
            record = parse_record(memoryview(scratch)[:total])
        except JournalError:
            break
        yield boot_id, segment_sequence, created_at_us, record


class BMJ1Journal:
    """Small rotating-segment manager; staging remains independent of I/O."""

    def __init__(self, directory, pico_boot_id, created_at_us,
                 staging_slots=4, segment_size=65536):
        self.directory = directory.rstrip("/")
        self.pico_boot_id = pico_boot_id
        self.segment_size = segment_size
        self.segment_sequence = 1
        self.stager = JournalStager(
            bytearray(RECORD_MAX_SIZE * staging_slots))
        self.path = self._path(self.segment_sequence)
        self.watermarks = load_watermarks(self.directory)
        self.checkpoint_dirty = False
        self.bytes_written = 0
        self.failure_count = 0
        self.file = None
        self._open(created_at_us)

    def _path(self, sequence):
        return "%s/%08d.bmj" % (self.directory, sequence)

    def _open(self, created_at_us):
        try:
            import uos as os
        except ImportError:
            import os
        try:
            os.mkdir(self.directory)
        except OSError:
            pass
        try:
            existing = os.listdir(self.directory)
            sequences = []
            for name in existing:
                if name.endswith(".bmj"):
                    try:
                        sequences.append(int(name[:-4]))
                    except ValueError:
                        pass
            if sequences:
                self.segment_sequence = max(sequences) + 1
                self.path = self._path(self.segment_sequence)
        except OSError:
            pass
        self.file = open(self.path, "wb")
        header = bytearray(SEGMENT_HEADER_SIZE)
        write_segment_header(header, self.pico_boot_id,
                             self.segment_sequence, created_at_us)
        self.file.write(header)
        self.file.flush()
        self.bytes_written += SEGMENT_HEADER_SIZE

    def stage(self, *args):
        return self.stager.stage(*args)

    def flush_one(self, created_at_us=0):
        if self.file is None:
            return 0
        current = self.stager.ring.peek()
        if current is None:
            if self.checkpoint_dirty:
                save_watermarks(self.directory, self.watermarks)
                self.checkpoint_dirty = False
            return 0
        record_size = len(current[1])
        try:
            position = self.file.tell()
        except AttributeError:
            position = 0
        if position and position + record_size > self.segment_size:
            self.file.flush()
            self.file.close()
            self.segment_sequence += 1
            self.path = self._path(self.segment_sequence)
            self.file = open(self.path, "wb")
            header = bytearray(SEGMENT_HEADER_SIZE)
            write_segment_header(header, self.pico_boot_id,
                                 self.segment_sequence, created_at_us)
            self.file.write(header)
            self.bytes_written += SEGMENT_HEADER_SIZE
        written = self.stager.flush_one(self.file)
        self.file.flush()
        self.bytes_written += written
        return written

    def record_ack(self, pico_boot_id, watermark):
        previous = self.watermarks.get(pico_boot_id, 0)
        if watermark > previous:
            self.watermarks[pico_boot_id] = watermark
            self.checkpoint_dirty = True


def recover_directory(directory, callback):
    """Recover valid records from all segments in filename order."""
    try:
        import uos as os
    except ImportError:
        import os
    try:
        names = sorted(name for name in os.listdir(directory)
                       if name.endswith(".bmj"))
    except OSError:
        return 0
    scratch = bytearray(RECORD_MAX_SIZE)
    watermarks = load_watermarks(directory)
    count = 0
    for name in names:
        try:
            with open(directory.rstrip("/") + "/" + name, "rb") as source:
                for boot_id, _, _, record in recover_records(source, scratch):
                    _, record_type, flags, link, sequence, received, payload = \
                        record
                    if sequence <= watermarks.get(boot_id, 0):
                        continue
                    callback(boot_id, record_type, flags, link, sequence,
                             received, payload)
                    count += 1
        except (OSError, JournalError):
            continue
    return count


def scan_available_ranges(directory):
    ranges = {}
    watermarks = load_watermarks(directory)

    def found(boot_id, _type, _flags, _link, sequence, _received, _payload):
        if sequence <= watermarks.get(boot_id, 0):
            return
        current = ranges.get(boot_id)
        if current is None:
            ranges[boot_id] = [sequence, sequence]
        else:
            current[0] = min(current[0], sequence)
            current[1] = max(current[1], sequence)

    recover_directory(directory, found)
    # Dict insertion order follows the segment scan, so the retained tail is
    # the most recent history rather than arbitrary numeric boot IDs.
    return tuple((boot, value[0], value[1])
                 for boot, value in ranges.items())


class JournalReplayCursor:
    """Pages recent unacknowledged journal records into bounded RAM."""

    def __init__(self, directory):
        self.directory = directory.rstrip("/")
        self.watermarks = load_watermarks(directory)
        self.pending = None
        ranges = scan_available_ranges(directory)
        historical_limit = max(0, C.MAX_REPLAY_BOOT_RANGES - 1)
        self.available_ranges = ranges[-historical_limit:] \
            if historical_limit else ()
        self.allowed_boots = {
            boot_id for boot_id, _oldest, _newest in self.available_ranges
        }
        self.next_sequences = {
            boot_id: max(self.watermarks.get(boot_id, 0) + 1, oldest)
            for boot_id, oldest, _newest in self.available_ranges
        }
        self.drop_payload = bytearray(32)
        self._iterator = self._records()

    def _records(self):
        try:
            import uos as os
        except ImportError:
            import os
        try:
            names = sorted(name for name in os.listdir(self.directory)
                           if name.endswith(".bmj"))
        except OSError:
            return
        scratch = bytearray(RECORD_MAX_SIZE)
        for name in names:
            try:
                with open(self.directory + "/" + name, "rb") as source:
                    for boot_id, _, _, record in recover_records(
                            source, scratch):
                        _, kind, flags, link, sequence, received, payload = \
                            record
                        if boot_id not in self.allowed_boots:
                            continue
                        if sequence <= self.watermarks.get(boot_id, 0):
                            continue
                        yield (boot_id, kind, flags, link, sequence, received,
                               bytes(payload))
            except (OSError, JournalError):
                continue

    def page_into(self, outbox, limit=16):
        loaded = 0
        while loaded < limit:
            if self.pending is None:
                try:
                    self.pending = next(self._iterator)
                except StopIteration:
                    break
            boot_id, _kind, _flags, _link, sequence, received, _payload = \
                self.pending
            expected = self.next_sequences.get(boot_id, sequence)
            if sequence > expected:
                last = min(sequence - 1, expected + 0xFFFFFFFF - 1)
                struct.pack_into(
                    ">QQQIB3s", self.drop_payload, 0, received,
                    expected, last, last - expected + 1,
                    C.DROP_JOURNAL_CORRUPT, b"\0\0\0")
                if not outbox.restore(
                        boot_id, C.TRANSPORT_DROP, C.FLAG_CRITICAL,
                        C.GLOBAL_SCOPE, expected, received,
                        self.drop_payload):
                    break
                self.next_sequences[boot_id] = last + 1
                loaded += 1
                continue
            if sequence < expected:
                self.pending = None
                continue
            if not outbox.restore(*self.pending):
                break
            self.next_sequences[boot_id] = sequence + 1
            self.pending = None
            loaded += 1
        return loaded

def load_watermarks(directory):
    path = directory.rstrip("/") + "/ack.bma"
    try:
        with open(path, "rb") as source:
            raw = source.read()
    except OSError:
        return {}
    if len(raw) < 12 or raw[:4] != CHECKPOINT_MAGIC or raw[4] != 1:
        return {}
    count = raw[5]
    expected_size = 8 + count * 16 + 4
    if len(raw) != expected_size:
        return {}
    if _crc32(memoryview(raw)[:-4]) != struct.unpack_from(
            ">I", raw, len(raw) - 4)[0]:
        return {}
    result = {}
    pos = 8
    for _ in range(count):
        boot_id, watermark = struct.unpack_from(">QQ", raw, pos)
        result[boot_id] = watermark
        pos += 16
    return result


def save_watermarks(directory, watermarks):
    items = sorted(watermarks.items())[-C.MAX_REPLAY_BOOT_RANGES:]
    raw = bytearray(8 + len(items) * 16 + 4)
    struct.pack_into(">4sBBH", raw, 0, CHECKPOINT_MAGIC, 1, len(items), 0)
    pos = 8
    for boot_id, watermark in items:
        struct.pack_into(">QQ", raw, pos, boot_id, watermark)
        pos += 16
    struct.pack_into(">I", raw, pos, _crc32(memoryview(raw)[:pos]))
    path = directory.rstrip("/") + "/ack.bma"
    temporary = path + ".tmp"
    with open(temporary, "wb") as target:
        target.write(raw)
        target.flush()
    try:
        import uos as os
    except ImportError:
        import os
    try:
        os.rename(temporary, path)
    except OSError:
        try:
            os.remove(path)
        except OSError:
            pass
        os.rename(temporary, path)
