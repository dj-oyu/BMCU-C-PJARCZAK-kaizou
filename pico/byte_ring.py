"""Fixed-slot byte ring using caller-owned storage."""


class RingFull(Exception):
    pass


class RecordTooLarge(ValueError):
    pass


class ByteRing:
    """A bounded FIFO with replaceable and protected record classes.

    Metadata arrays are allocated once during construction. Payload bytes live
    entirely in the storage supplied by the caller.
    """

    def __init__(self, storage, slot_size):
        if slot_size <= 0 or len(storage) < slot_size:
            raise ValueError("invalid ring storage")
        self.storage = storage
        self.slot_size = slot_size
        self.capacity = len(storage) // slot_size
        self.lengths = [0] * self.capacity
        self.sequences = [0] * self.capacity
        self.protected = [False] * self.capacity
        self.replace_keys = [-1] * self.capacity
        self.head = 0
        self.count = 0
        self.dropped_first = 0
        self.dropped_last = 0
        self.dropped_count = 0

    def __len__(self):
        return self.count

    def _index(self, logical):
        return (self.head + logical) % self.capacity

    def _copy_into(self, index, data):
        if len(data) > self.slot_size:
            raise RecordTooLarge(len(data))
        start = index * self.slot_size
        memoryview(self.storage)[start:start + len(data)] = data
        self.lengths[index] = len(data)

    def _record_drop(self, sequence):
        if self.dropped_count == 0:
            self.dropped_first = sequence
        self.dropped_last = sequence
        self.dropped_count += 1

    def append(self, sequence, data, protected=False, replace_key=-1):
        if len(data) > self.slot_size:
            raise RecordTooLarge(len(data))
        if replace_key >= 0:
            for logical in range(self.count):
                index = self._index(logical)
                if self.replace_keys[index] == replace_key:
                    self._copy_into(index, data)
                    self.sequences[index] = sequence
                    self.protected[index] = protected
                    return True
        if self.count == self.capacity:
            raise RingFull()
        index = self._index(self.count)
        self._copy_into(index, data)
        self.sequences[index] = sequence
        self.protected[index] = protected
        self.replace_keys[index] = replace_key
        self.count += 1
        return False

    def append_evict_acked(self, sequence, data, acknowledged_watermark,
                           protected=False, replace_key=-1):
        """Append, evicting only an ACKed, non-protected FIFO head.

        Protected or unacknowledged data is never silently removed.
        """
        try:
            return self.append(sequence, data, protected, replace_key)
        except RingFull:
            head = self.head
            if (self.protected[head] or
                    self.sequences[head] > acknowledged_watermark):
                raise
            self.lengths[head] = 0
            self.replace_keys[head] = -1
            self.head = (head + 1) % self.capacity
            self.count -= 1
            self.append(sequence, data, protected, replace_key)
            return False

    def peek(self):
        if not self.count:
            return None
        index = self.head
        start = index * self.slot_size
        return (self.sequences[index],
                memoryview(self.storage)[start:start + self.lengths[index]],
                self.protected[index], self.replace_keys[index])

    def release_through(self, watermark):
        released = 0
        while self.count and self.sequences[self.head] <= watermark:
            self.lengths[self.head] = 0
            self.protected[self.head] = False
            self.replace_keys[self.head] = -1
            self.head = (self.head + 1) % self.capacity
            self.count -= 1
            released += 1
        return released

    def release_one(self):
        if not self.count:
            return False
        self.lengths[self.head] = 0
        self.protected[self.head] = False
        self.replace_keys[self.head] = -1
        self.head = (self.head + 1) % self.capacity
        self.count -= 1
        return True

    def iter_records(self):
        for logical in range(self.count):
            index = self._index(logical)
            start = index * self.slot_size
            yield (self.sequences[index],
                   memoryview(self.storage)[start:start + self.lengths[index]],
                   self.protected[index], self.replace_keys[index])

    def take_dropped_range(self):
        result = (self.dropped_first, self.dropped_last, self.dropped_count)
        self.dropped_first = 0
        self.dropped_last = 0
        self.dropped_count = 0
        return result
