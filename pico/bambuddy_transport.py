"""Host-testable Bambuddy envelope and bounded delivery queue.

This module deliberately has no socket or ``machine`` dependency. The network
adapter can therefore be replaced without changing identity, replay, or ACK
semantics, and the same code can be exercised by CPython CI.
"""

try:
    import uos as os
except ImportError:
    import os


SCHEMA = "bmcu.management.v2"
REGISTRY_VERSION = "alpha.3"
DEFAULT_QUEUE_LIMIT = 128
DEFAULT_QUEUE_AGE_MS = 30000


def _ticks_diff(now, then):
    try:
        import utime as time
    except ImportError:
        import time
    return time.ticks_diff(now, then) if hasattr(time, "ticks_diff") else now - then


def _json_safe(value):
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def new_boot_session(random_bytes=None):
    """Return an unpredictable identifier without requiring a wall clock."""
    source = random_bytes or os.urandom
    return source(16).hex()


class EnvelopeBuilder:
    """Adds stable bridge identity and per-link transport ordering."""

    def __init__(self, device_id, mode="production_monitor", boot_session=None,
                 registry_version=REGISTRY_VERSION):
        if not device_id:
            raise ValueError("device_id is required")
        if mode not in ("production_monitor", "bench_stub"):
            raise ValueError("invalid mode")
        self.device_id = device_id
        self.mode = mode
        self.pico_boot_session = boot_session or new_boot_session()
        self.registry_version = registry_version
        self._transport_sequence = {}
        self._bmcu_boot_session = {}
        self._protocol = {}
        self._link_state = {}

    def link_sessions(self):
        link_ids = set(self._transport_sequence)
        link_ids.update(self._bmcu_boot_session)
        link_ids.update(self._link_state)
        return [{
            "link_id": link_id,
            "bmcu_boot_session": self._bmcu_boot_session.get(link_id, 0),
            "state": self._link_state.get(link_id, "unknown"),
        } for link_id in sorted(link_ids)]

    def _next_sequence(self, link_id):
        value = self._transport_sequence.get(link_id, 0)
        self._transport_sequence[link_id] = value + 1
        return value

    def build(self, message, received_at_us, queue_depth=0):
        message = _json_safe(message)
        link_id = message.get("link_id", "bridge")
        message_type = message.get("type", "unknown")
        if message_type == "hello":
            self._bmcu_boot_session[link_id] = message.get(
                "bmcu_boot_session", self._bmcu_boot_session.get(link_id, 0))
            self._protocol[link_id] = message.get("protocol", self._protocol.get(link_id))
            self._link_state[link_id] = "resyncing"
        elif message_type == "link_state":
            self._link_state[link_id] = message.get("state", "unknown")
        elif message_type in ("status", "event", "full_status_record"):
            self._link_state[link_id] = "online"

        bmcu_sequence = message.get("sequence")
        data = dict(message)
        for key in ("link_id", "sequence", "kind", "type", "bmcu_boot_session"):
            data.pop(key, None)

        link = {
            "id": link_id,
            "state": self._link_state.get(link_id, "unknown"),
            "pico_boot_session": self.pico_boot_session,
            "bmcu_boot_session": self._bmcu_boot_session.get(link_id, 0),
            "transport_sequence": self._next_sequence(link_id),
            "queue_depth": queue_depth,
        }
        if bmcu_sequence is not None:
            link["bmcu_sequence"] = bmcu_sequence

        return {
            "schema": SCHEMA,
            "registry_version": self.registry_version,
            "device_id": self.device_id,
            "mode": self.mode,
            "received_at_us": received_at_us,
            "link": link,
            "frame": {
                "kind": message_type,
                "kind_id": message.get("kind"),
                "protocol": self._protocol.get(link_id),
            },
            "data": data,
        }


class TelemetryQueue:
    """Bounded oldest-first queue whose records survive until persisted ACK."""

    CRITICAL_KINDS = ("event", "hello", "link_state", "transport_drop",
                      "protocol_error", "snapshot_error")

    def __init__(self, limit=DEFAULT_QUEUE_LIMIT, max_age_ms=DEFAULT_QUEUE_AGE_MS):
        if limit < 2:
            raise ValueError("queue limit must be at least two")
        self.limit = limit
        self.max_age_ms = max_age_ms
        self.records = []
        self.dropped_count = 0
        self.quarantined = []

    def __len__(self):
        return len(self.records)

    def _drop_index(self, index):
        self.records.pop(index)
        self.dropped_count += 1

    def _expire(self, now_ms):
        index = 0
        while index < len(self.records):
            if _ticks_diff(now_ms, self.records[index]["queued_ms"]) > self.max_age_ms:
                self._drop_index(index)
            else:
                index += 1

    def enqueue(self, envelope, now_ms, critical=None):
        before = self.dropped_count
        self._expire(now_ms)
        if critical is None:
            critical = envelope["frame"]["kind"] in self.CRITICAL_KINDS
        if len(self.records) >= self.limit:
            drop = None
            for index, record in enumerate(self.records):
                if (not record["critical"] and
                        record["envelope"]["frame"]["kind"] == "status"):
                    drop = index
                    break
            self._drop_index(0 if drop is None else drop)
        self.records.append({"envelope": envelope, "queued_ms": now_ms,
                             "critical": bool(critical)})
        return self.dropped_count - before

    def batch(self, limit=16, now_ms=None):
        if now_ms is not None:
            self._expire(now_ms)
        return [record["envelope"] for record in self.records[:limit]]

    @staticmethod
    def _identity(envelope):
        link = envelope["link"]
        return (link["id"], link["pico_boot_session"], link["transport_sequence"])

    def apply_ack(self, persisted=None, rejected=None):
        persisted = persisted or []
        rejected = rejected or []
        watermarks = {}
        for item in persisted:
            key = (item.get("link_id"), item.get("pico_boot_session"))
            sequence = item.get("transport_sequence")
            if None not in key and isinstance(sequence, int):
                watermarks[key] = max(sequence, watermarks.get(key, -1))

        rejected_keys = set()
        for item in rejected:
            if item.get("retryable", False):
                continue
            sequence = item.get("transport_sequence")
            if not isinstance(sequence, int):
                continue
            link_id = item.get("link_id")
            session = item.get("pico_boot_session")
            if link_id is not None and session is not None:
                rejected_keys.add((link_id, session, sequence))

        kept = []
        persisted_count = 0
        rejected_count = 0
        for record in self.records:
            envelope = record["envelope"]
            link = envelope["link"]
            identity = self._identity(envelope)
            watermark = watermarks.get((link["id"], link["pico_boot_session"]))
            if watermark is not None and link["transport_sequence"] <= watermark:
                persisted_count += 1
            elif identity in rejected_keys:
                rejected_count += 1
                self.dropped_count += 1
                self.quarantined.append({
                    "identity": identity,
                    "envelope": envelope,
                })
                if len(self.quarantined) > 16:
                    self.quarantined.pop(0)
            else:
                kept.append(record)
        self.records = kept
        return {"persisted": persisted_count, "rejected": rejected_count}


class BambuddyOutbox:
    """Envelope builder plus loss-signalling FIFO used by network adapters."""

    def __init__(self, device_id, mode="production_monitor", boot_session=None,
                 queue_limit=DEFAULT_QUEUE_LIMIT, queue_age_ms=DEFAULT_QUEUE_AGE_MS):
        self.builder = EnvelopeBuilder(device_id, mode, boot_session)
        self.queue = TelemetryQueue(queue_limit, queue_age_ms)

    @property
    def pico_boot_session(self):
        return self.builder.pico_boot_session

    def publish(self, message, now_ms, received_at_us):
        envelope = self.builder.build(message, received_at_us, len(self.queue))
        dropped = self.queue.enqueue(envelope, now_ms)
        if dropped and envelope["frame"]["kind"] != "transport_drop":
            notice = None
            for record in self.queue.records:
                candidate = record["envelope"]
                if (candidate["frame"]["kind"] == "transport_drop" and
                        candidate["link"]["id"] == envelope["link"]["id"]):
                    notice = candidate
                    break
            if notice is None:
                notice = self.builder.build({
                    "type": "transport_drop",
                    "link_id": envelope["link"]["id"],
                    "reason": "queue_overflow",
                    "dropped_count": self.queue.dropped_count,
                }, received_at_us, len(self.queue))
                self.queue.enqueue(notice, now_ms, critical=True)
            notice["received_at_us"] = received_at_us
            notice["data"]["dropped_count"] = self.queue.dropped_count
        return envelope

    def apply_ack(self, ack):
        return self.queue.apply_ack(ack.get("persisted"), ack.get("rejected"))
