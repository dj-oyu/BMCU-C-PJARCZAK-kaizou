#!/usr/bin/env python3
"""Derive and format GitHub Release metadata for firmware matrix builds."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re

import firmware_matrix as fm


ROOT = Path(__file__).resolve().parents[1]


def short_firmware_version(version_path: Path = ROOT / "version") -> str:
    raw_version = version_path.read_text(encoding="utf-8").strip()
    parts = raw_version.split(".")
    if len(parts) < 2 or not all(part.isdigit() for part in parts):
        raise SystemExit(f"invalid firmware version: {raw_version!r}")
    try:
        return format(Decimal(".".join(parts[:2])).normalize(), "f")
    except InvalidOperation as error:
        raise SystemExit(f"invalid firmware version: {raw_version!r}") from error


def release_metadata(label: str | None = None, release_day: date | None = None) -> dict[str, str]:
    release_day = release_day or datetime.now(timezone.utc).date()
    if label and label.strip():
        release_label = fm.sanitize_release_label(label)
        match = re.fullmatch(
            r"V(?P<version>\d+(?:\.\d+)+)-kaizou-(?P<day>\d{8})",
            release_label,
        )
        if match:
            try:
                title_day = datetime.strptime(match.group("day"), "%Y%m%d").date()
            except ValueError as error:
                raise SystemExit(f"invalid release date in label: {release_label}") from error
            release_name = (
                f"BMCU V{match.group('version')} kaizou firmware matrix "
                f"({title_day.isoformat()})"
            )
        else:
            release_name = f"BMCU {release_label} firmware matrix ({release_day.isoformat()})"
    else:
        version = short_firmware_version()
        release_label = f"V{version}-kaizou-{release_day:%Y%m%d}"
        release_name = f"BMCU V{version} kaizou firmware matrix ({release_day.isoformat()})"
    return {"release_label": release_label, "release_name": release_name}


def write_github_outputs(path: Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise SystemExit(f"GitHub output {key!r} must be a single line")
            stream.write(f"{key}={value}\n")


def required_asset(assets: Path, name: str) -> Path:
    path = assets / name
    if not path.is_file():
        raise SystemExit(f"missing release asset: {path}")
    return path


def write_release_notes(assets: Path, label: str) -> tuple[Path, Path]:
    assets = assets.resolve()
    release_label = fm.sanitize_release_label(label)
    prefix = f"BMCU-{release_label}"
    all_zip = required_asset(assets, f"{prefix}-all.zip")
    standard_zip = required_asset(assets, f"{prefix}-standard-A1.zip")
    high_force_zip = required_asset(assets, f"{prefix}-high-force-P1S.zip")
    soft_load_zip = required_asset(assets, f"{prefix}-soft-load-A1.zip")
    manifest = required_asset(assets, f"{prefix}-manifest.json")
    notes = required_asset(assets, f"{prefix}-release-notes.md")
    checksums = required_asset(assets, f"{prefix}-SHA256SUMS.txt")

    entries = json.loads(manifest.read_text(encoding="utf-8"))
    config = fm.load_config()
    commit = os.environ.get("GITHUB_SHA", "local build")
    run_url = ""
    if all(os.environ.get(name) for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")):
        run_url = (
            f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
            f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        )
    run_line = f"- GitHub Actions run: {run_url}\n" if run_url else ""
    distances = ", ".join(f"`{value}`" for value in config["ams_retract_m"])
    zip_hashes = {
        "all": fm.file_metadata(all_zip)[0].upper(),
        "standard": fm.file_metadata(standard_zip)[0].upper(),
        "high_force": fm.file_metadata(high_force_zip)[0].upper(),
        "soft_load": fm.file_metadata(soft_load_zip)[0].upper(),
    }
    notes.write_text(
        f"Built from main commit `{commit}`.\n\n"
        "## Downloads\n\n"
        f"- `{all_zip.name}`: all {len(entries)} firmware binaries plus guides and manifests\n"
        f"- `{standard_zip.name}`: standard loading force; recommended starting point\n"
        f"- `{high_force_zip.name}`: higher loading force for P1S or long/bent PTFE paths\n"
        f"- `{soft_load_zip.name}`: reduced loading force for A1/A1 Mini setups that grind or click\n\n"
        "## Selecting a binary\n\n"
        "Inside a profile ZIP, choose in this order:\n\n"
        "1. `autoload_on` or `autoload_off`\n"
        "2. `filament_rgb_on` or `filament_rgb_off`\n"
        "3. `ams_a`, `ams_b`, `ams_c`, or `ams_d`\n"
        f"4. Retraction distance: {distances} m\n\n"
        "`solo` is provided separately with the fixed `0.095 m` setting.\n\n"
        "## Verification\n\n"
        "- Host tests: passed\n"
        f"- Full firmware matrix: {len(fm.matrix_shards(config))} jobs × "
        f"{len(fm.targets(config))} variants = {len(entries)} successful builds\n"
        "- `manifest.json`, `manifest.csv`, and `manifest.txt` contain size, "
        "SHA-256, and CRC32 for every binary\n"
        f"{run_line}\n"
        "## Package SHA-256\n\n"
        f"- all: `{zip_hashes['all']}`\n"
        f"- standard A1: `{zip_hashes['standard']}`\n"
        f"- high-force P1S: `{zip_hashes['high_force']}`\n"
        f"- soft-load A1: `{zip_hashes['soft_load']}`\n\n"
        "> Pre-release: the firmware matrix builds cleanly, but hardware validation "
        "is still required. Start with the standard profile unless your hardware "
        "requires another loading-force profile.\n",
        encoding="utf-8",
    )

    checksum_assets = sorted(
        path for path in assets.iterdir()
        if path.is_file() and path != checksums
    )
    with checksums.open("w", encoding="utf-8", newline="\n") as stream:
        for asset in checksum_assets:
            stream.write(f"{fm.file_metadata(asset)[0]}  {asset.name}\n")
    return notes, checksums


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    command = commands.add_parser("metadata")
    command.add_argument("--label")
    command.add_argument("--github-output", type=Path)

    command = commands.add_parser("notes")
    command.add_argument("--assets", type=Path, required=True)
    command.add_argument("--label", required=True)

    args = parser.parse_args()
    if args.command == "metadata":
        metadata = release_metadata(args.label)
        if args.github_output:
            write_github_outputs(args.github_output, metadata)
        else:
            print(json.dumps(metadata, separators=(",", ":")))
    elif args.command == "notes":
        write_release_notes(args.assets, args.label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
