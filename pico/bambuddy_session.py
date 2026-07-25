"""Transport-agnostic Bambuddy ACK adaptation helpers.

This module deliberately has no socket or ``machine`` dependency so every
network adapter (WebSocket, HTTPS NDJSON, ...) shares one implementation of the
ACK adaptation rules instead of forking them. Delivery semantics themselves
live in :mod:`bambuddy_transport`; nothing here mutates a queue.
"""


def enrich_rejected(message, inflight):
    """Map batch-index rejections onto their per-link dedup identities."""
    if inflight is None:
        return message
    enriched = dict(message)
    rejected = []
    for item in message.get("rejected", []) or []:
        candidate = dict(item)
        index = candidate.get("index")
        if isinstance(index, int) and 0 <= index < len(inflight):
            link = inflight[index]["link"]
            sent_sequence = link["transport_sequence"]
            parsed_sequence = candidate.get("transport_sequence")
            if parsed_sequence is None or parsed_sequence == sent_sequence:
                candidate["link_id"] = link["id"]
                candidate["pico_boot_session"] = link["pico_boot_session"]
                candidate["transport_sequence"] = sent_sequence
        rejected.append(candidate)
    enriched["rejected"] = rejected
    return enriched


def accepted_only_watermarks(message, inflight):
    """Adapt Bambuddy accepted-only ACKs into durable per-link watermarks."""
    if inflight is None or message.get("persisted") or message.get("rejected"):
        return message
    accepted = message.get("accepted")
    if not isinstance(accepted, int) or accepted < len(inflight):
        return message
    watermarks = {}
    for envelope in inflight:
        link = envelope["link"]
        key = (link["id"], link["pico_boot_session"])
        sequence = link["transport_sequence"]
        watermarks[key] = max(sequence, watermarks.get(key, -1))
    adapted = dict(message)
    adapted["persisted"] = [{
        "link_id": key[0],
        "pico_boot_session": key[1],
        "transport_sequence": sequence,
    } for key, sequence in watermarks.items()]
    return adapted


def hello_persisted_by_ack(message, hello_link, hello_pending):
    """Return True when this ACK durably covers the pending transport HELLO."""
    if hello_link is None:
        return False
    if (hello_pending and message.get("accepted", 0) > 0 and
            not message.get("rejected")):
        return True
    for item in message.get("persisted", []) or []:
        if (item.get("link_id") == hello_link["id"] and
                item.get("pico_boot_session") ==
                hello_link["pico_boot_session"] and
                item.get("transport_sequence", -1) >=
                hello_link["transport_sequence"]):
            return True
    return False
