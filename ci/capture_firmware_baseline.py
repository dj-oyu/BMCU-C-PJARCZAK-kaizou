"""Write a reproducible JSON baseline for an already-built PlatformIO environment."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", default="fw")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--parameter", action="append", default=[], metavar="NAME=VALUE")
    args = parser.parse_args()
    build = ROOT / ".pio" / "build" / args.environment
    artifacts = {}
    for name in ("firmware.bin", "firmware.elf", "firmware.map"):
        path = build / name
        if path.is_file():
            artifacts[name] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
    if "firmware.bin" not in artifacts:
        parser.error("build artifact not found; run PlatformIO before capturing a baseline")
    parameters = {}
    for item in args.parameter:
        key, separator, value = item.partition("=")
        if not separator or not key:
            parser.error("--parameter must use NAME=VALUE")
        parameters[key] = value
    report = {
        "schema": 1,
        "git": {"commit": git("rev-parse", "HEAD"), "describe": git("describe", "--always", "--dirty")},
        "platformio_environment": args.environment,
        "build_parameters": parameters,
        "artifacts": artifacts,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
