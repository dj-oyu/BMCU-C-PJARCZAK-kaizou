"""Build the self-describing schema the Pico serves at /api/schema.json.

The device's diagnostic endpoints are binary because heap and CPU on the target
are scarce. The cost of that choice falls on whoever has to decode them: three
layers stack before a STATUS field is reached, and the endianness flips between
them, so every ad-hoc reader re-derives the same offsets and any one of them can
get the sum wrong.

Serving the schema removes that cost without adding a second encoder to the
device or changing a single existing byte. The output is generated at build
time and streamed off littlefs like the web page, so it costs no heap and no
CPU at request time.

Unlike the page it is stored uncompressed. This is the entry point, and the
notes explaining that everything else is gzipped live inside it; urllib does not
transparently inflate, so a gzipped schema fails its first naive read before it
can say why. Twenty kilobytes of flash is a cheap way to avoid that.

Sources: docs/bmcu_wire_layout.json for offsets, plus the two enum registries.
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAYOUT = ROOT / "docs" / "bmcu_wire_layout.json"
BINARY_REGISTRY = ROOT / "docs" / "bmcu_binary_registry.json"
LINK_REGISTRY = ROOT / "docs" / "bmcu_link_enum_registry.json"
STAGED = ROOT / "pico" / "www" / "schema.json"

# Enum groups worth resolving names for. Anything referenced by a layout field's
# "enum" key must resolve, which the generator checks.
BINARY_GROUPS = (
    "message_types", "flags", "value_types", "link_states", "log_severity",
    "drop_reasons", "diagnostic_tags", "limits",
)


def invert(mapping):
    """Registry groups are name -> value; readers want value -> name."""
    return {str(value): name.lower()
            for name, value in mapping.items() if isinstance(value, int)}


def build():
    layout = json.loads(LAYOUT.read_text(encoding="utf-8"))
    binary_registry = json.loads(BINARY_REGISTRY.read_text(encoding="utf-8"))
    link_registry = json.loads(LINK_REGISTRY.read_text(encoding="utf-8"))

    enums = {group: invert(binary_registry[group])
             for group in BINARY_GROUPS if group in binary_registry}
    # The link registry already stores value -> name.
    for group, values in link_registry.get("enums", {}).items():
        enums.setdefault(group, {str(k): str(v) for k, v in values.items()})
    enums.update(layout.get("local_enums", {}))

    missing = sorted({
        field["enum"]
        for structure in layout["structures"].values()
        for field in structure["fields"]
        if "enum" in field and field["enum"] not in enums
    })
    if missing:
        raise SystemExit("layout references unknown enum groups: %s"
                         % ", ".join(missing))

    return {
        "schema": "bmcu-monitor-api",
        "revision": layout["revision"],
        "purpose": (
            "Self-description for the BMCU monitor's local HTTP API. Fetch this "
            "once, then every other endpoint can be decoded without reading the "
            "device source."
        ),
        "endpoints": layout["endpoints"],
        "structures": layout["structures"],
        "enums": enums,
        "reading_notes": layout["reading_notes"],
        "thresholds": layout["thresholds"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="fail instead of writing when the artifact drifts")
    arguments = parser.parse_args()

    # Sorted keys keep the committed artifact reproducible.
    rendered = json.dumps(build(), indent=1, ensure_ascii=False,
                          sort_keys=True).encode("utf-8")

    if arguments.check:
        try:
            current = STAGED.read_bytes()
        except OSError:
            current = b""
        if current != rendered:
            raise SystemExit("%s is stale; run tools/generate_api_schema.py"
                             % STAGED.relative_to(ROOT))
        return

    STAGED.parent.mkdir(parents=True, exist_ok=True)
    STAGED.write_bytes(rendered)
    print("%s: %d bytes json" % (STAGED.relative_to(ROOT), len(rendered)))


if __name__ == "__main__":
    main()
