#!/usr/bin/env python3
"""Plan, build, and merge the supported BMCU firmware matrix."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile
import zlib


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "ci" / "firmware_matrix.json"
RELEASE_PROFILE_SLUGS = {
    "standard": "standard-A1",
    "high_force": "high-force-P1S",
    "soft_load": "soft-load-A1",
}
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def load_config() -> dict:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    profile_ids = [item["id"] for item in config["profiles"]]
    if len(profile_ids) != len(set(profile_ids)):
        raise SystemExit("duplicate profile id in firmware_matrix.json")
    if any(item["bmcu_p1s"] and item["bmcu_soft_load"] for item in config["profiles"]):
        raise SystemExit("BMCU_P1S and BMCU_SOFT_LOAD cannot both be enabled")
    if config["autoload_values"] != [0, 1] or config["filament_rgb_values"] != [0, 1]:
        raise SystemExit("boolean option tables must be [0, 1]")
    return config


def matrix_shards(config: dict) -> list[dict]:
    return [
        {"profile": profile["id"], "autoload": autoload, "rgb": rgb}
        for profile in config["profiles"]
        for autoload in config["autoload_values"]
        for rgb in config["filament_rgb_values"]
    ]


def targets(config: dict) -> list[dict]:
    result = [{
        "target": "solo",
        "target_id": "solo",
        "ams_num": config["solo"]["ams_num"],
        "retract_m": config["solo"]["retract_m"],
    }]
    for address in config["ams_addresses"]:
        for retract_m in config["ams_retract_m"]:
            result.append({
                "target": "ams",
                "target_id": f"ams_{address['id']}",
                "ams_num": address["ams_num"],
                "retract_m": retract_m,
            })
    return result


def profile_by_id(config: dict, profile_id: str) -> dict:
    for profile in config["profiles"]:
        if profile["id"] == profile_id:
            return profile
    raise SystemExit(f"unknown profile: {profile_id}")


def artifact_relative_path(profile_id: str, autoload: int, rgb: int, target: dict) -> Path:
    feature_dir = Path(
        profile_id,
        "autoload_on" if autoload else "autoload_off",
        "filament_rgb_on" if rgb else "filament_rgb_off",
    )
    if target["target"] == "solo":
        filename = f"solo_{target['retract_m']}f.bin"
    else:
        filename = f"{target['target_id']}_{target['retract_m']}f.bin"
    return Path("firmwares") / feature_dir / target["target_id"] / filename


def file_metadata(path: Path) -> tuple[str, str, int]:
    digest = hashlib.sha256()
    crc = 0
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            crc = zlib.crc32(block, crc)
            size += len(block)
    return digest.hexdigest(), f"{crc & 0xFFFFFFFF:08X}", size


def build_shard(config: dict, profile_id: str, autoload: int, rgb: int,
                output: Path, dry_run: bool = False) -> list[dict]:
    profile = profile_by_id(config, profile_id)
    variants = targets(config)
    if dry_run:
        for target in variants:
            print(artifact_relative_path(profile_id, autoload, rgb, target).as_posix())
        return []

    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    entries = []
    pio_environment = config["platformio_environment"]
    source_bin = ROOT / ".pio" / "build" / pio_environment / "firmware.bin"

    for index, target in enumerate(variants, 1):
        relative = artifact_relative_path(profile_id, autoload, rgb, target)
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        compiler_retract = f"{target['retract_m']}f"
        env = os.environ.copy()
        env.update({
            "BAMBU_BUS_AMS_NUM": str(target["ams_num"]),
            "AMS_RETRACT_LEN": compiler_retract,
            "BMCU_DM_TWO_MICROSWITCH": str(autoload),
            "BMCU_ONLINE_LED_FILAMENT_RGB": str(rgb),
            "DBMCU_P1S": str(profile["bmcu_p1s"]),
            "BMCU_SOFT_LOAD": str(profile["bmcu_soft_load"]),
        })
        print(
            f"[{index:02d}/{len(variants)}] {profile_id} autoload={autoload} rgb={rgb} "
            f"target={target['target_id']} retract={compiler_retract}",
            flush=True,
        )
        subprocess.run(
            ["pio", "run", "-e", pio_environment],
            cwd=ROOT,
            env=env,
            check=True,
        )
        if not source_bin.is_file():
            raise SystemExit(f"PlatformIO did not create {source_bin}")
        shutil.copy2(source_bin, destination)
        sha256, crc32, size = file_metadata(destination)
        entries.append({
            "profile": profile_id,
            "profile_label": profile["label"],
            "bmcu_p1s": profile["bmcu_p1s"],
            "bmcu_soft_load": profile["bmcu_soft_load"],
            "autoload": autoload,
            "filament_rgb": rgb,
            "target": target["target"],
            "target_id": target["target_id"],
            "ams_num": target["ams_num"],
            "retract_m": target["retract_m"],
            "path": relative.as_posix(),
            "size": size,
            "sha256": sha256,
            "crc32": crc32,
            "git_sha": os.environ.get("GITHUB_SHA", ""),
        })

    manifest_dir = output / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"manifest-{profile_id}-autoload{autoload}-rgb{rgb}.json"
    manifest_path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {manifest_path} ({len(entries)} binaries)")
    return entries


def merge_manifests(root: Path) -> list[dict]:
    root = root.resolve()
    manifest_paths = sorted((root / "manifests").glob("manifest-*.json"))
    if not manifest_paths:
        raise SystemExit(f"no shard manifests found below {root / 'manifests'}")
    entries = []
    seen_paths = set()
    for manifest_path in manifest_paths:
        for entry in json.loads(manifest_path.read_text(encoding="utf-8")):
            if entry["path"] in seen_paths:
                raise SystemExit(f"duplicate firmware path: {entry['path']}")
            seen_paths.add(entry["path"])
            binary = root / entry["path"]
            if not binary.is_file():
                raise SystemExit(f"missing firmware binary: {binary}")
            sha256, crc32, size = file_metadata(binary)
            if (sha256, crc32, size) != (entry["sha256"], entry["crc32"], entry["size"]):
                raise SystemExit(f"manifest mismatch: {binary}")
            entries.append(entry)

    entries.sort(key=lambda item: item["path"])
    (root / "manifest.json").write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    fieldnames = [
        "profile", "profile_label", "bmcu_p1s", "bmcu_soft_load", "autoload",
        "filament_rgb", "target", "target_id", "ams_num", "retract_m", "path",
        "size", "sha256", "crc32", "git_sha",
    ]
    with (root / "manifest.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(entries)
    with (root / "manifest.txt").open("w", encoding="utf-8") as stream:
        stream.write("# SHA256 CRC32 SIZE PATH\n")
        for entry in entries:
            stream.write(f"{entry['sha256']} {entry['crc32']} {entry['size']} {entry['path']}\n")
    print(f"merged {len(manifest_paths)} shards and {len(entries)} binaries below {root}")
    return entries


def sanitize_release_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    if not label:
        raise SystemExit("release label must contain a letter, number, dot, underscore, or hyphen")
    return label[:80]


def default_release_label() -> str:
    if os.environ.get("GITHUB_REF_TYPE") == "tag" and os.environ.get("GITHUB_REF_NAME"):
        return sanitize_release_label(os.environ["GITHUB_REF_NAME"])
    sha = os.environ.get("GITHUB_SHA", "")
    return f"main-{sha[:8]}" if sha else "local"


def write_deterministic_zip(destination: Path, root: Path, files: list[Path]) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(relative, ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def package_release(root: Path, output: Path, label: str | None = None,
                    expected_count: int | None = None) -> list[Path]:
    root = root.resolve()
    output = output.resolve()
    entries = merge_manifests(root)
    if expected_count is not None and len(entries) != expected_count:
        raise SystemExit(f"expected {expected_count} firmware binaries, found {len(entries)}")
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"release output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    release_label = sanitize_release_label(label) if label and label.strip() else default_release_label()
    prefix = f"BMCU-{release_label}"
    common_files = [
        path for path in (
            root / "manifest.json",
            root / "manifest.csv",
            root / "manifest.txt",
            root / "FIRMWARE_BUILD_MATRIX.md",
        ) if path.is_file()
    ]
    common_files.extend(path for path in (root / "guides").glob("*") if path.is_file())
    firmware_files = [root / entry["path"] for entry in entries]

    assets = []
    all_zip = output / f"{prefix}-all.zip"
    write_deterministic_zip(all_zip, root, firmware_files + common_files)
    assets.append(all_zip)
    for profile_id, profile_slug in RELEASE_PROFILE_SLUGS.items():
        profile_files = [
            root / entry["path"] for entry in entries if entry["profile"] == profile_id
        ]
        if not profile_files:
            raise SystemExit(f"no firmware binaries found for release profile: {profile_id}")
        destination = output / f"{prefix}-{profile_slug}.zip"
        write_deterministic_zip(destination, root, profile_files + common_files[3:])
        assets.append(destination)

    for suffix in ("json", "csv", "txt"):
        source = root / f"manifest.{suffix}"
        destination = output / f"{prefix}-manifest.{suffix}"
        shutil.copy2(source, destination)
        assets.append(destination)

    config = load_config()
    commit = os.environ.get("GITHUB_SHA", "local build")
    notes = output / f"{prefix}-release-notes.md"
    distances = ", ".join(config["ams_retract_m"])
    notes.write_text(
        f"# BMCU firmware {release_label}\n\n"
        f"- Source commit: `{commit}`\n"
        f"- Firmware binaries: {len(entries)}\n"
        f"- Load profiles: standard A1, high-force P1S, soft-load A1\n"
        f"- AMS retraction distances (m): {distances}\n\n"
        "Choose a load-profile ZIP, then select autoload, filament RGB, AMS address, "
        "and retraction distance from its folder hierarchy. The `all` ZIP contains "
        "every supported variant and the complete manifests.\n",
        encoding="utf-8",
    )
    assets.append(notes)

    checksums = output / f"{prefix}-SHA256SUMS.txt"
    with checksums.open("w", encoding="utf-8", newline="\n") as stream:
        for asset in sorted(assets, key=lambda item: item.name):
            sha256, _, _ = file_metadata(asset)
            stream.write(f"{sha256}  {asset.name}\n")
    assets.append(checksums)
    print(f"prepared {len(assets)} release assets in {output}")
    return assets


def build_all(config: dict, output: Path) -> None:
    for shard in matrix_shards(config):
        build_shard(config, shard["profile"], shard["autoload"], shard["rgb"], output)
    merge_manifests(output)


def int_bool(value: str) -> int:
    parsed = int(value)
    if parsed not in (0, 1):
        raise argparse.ArgumentTypeError("expected 0 or 1")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    commands.add_parser("matrix")

    command = commands.add_parser("list")
    command.add_argument("--profile", required=True)
    command.add_argument("--autoload", type=int_bool, required=True)
    command.add_argument("--rgb", type=int_bool, required=True)

    command = commands.add_parser("build")
    command.add_argument("--profile", required=True)
    command.add_argument("--autoload", type=int_bool, required=True)
    command.add_argument("--rgb", type=int_bool, required=True)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--dry-run", action="store_true")

    command = commands.add_parser("build-all")
    command.add_argument("--output", type=Path, required=True)

    command = commands.add_parser("merge")
    command.add_argument("--root", type=Path, required=True)

    command = commands.add_parser("package-release")
    command.add_argument("--root", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--label")

    args = parser.parse_args()
    config = load_config()
    shards = matrix_shards(config)
    variant_count = len(shards) * len(targets(config))

    if args.command == "validate":
        print(f"valid: {len(shards)} shards, {len(targets(config))} binaries/shard, {variant_count} total")
    elif args.command == "matrix":
        print(json.dumps({"include": shards}, separators=(",", ":")))
    elif args.command == "list":
        build_shard(config, args.profile, args.autoload, args.rgb, Path("."), dry_run=True)
    elif args.command == "build":
        build_shard(config, args.profile, args.autoload, args.rgb, args.output, args.dry_run)
    elif args.command == "build-all":
        build_all(config, args.output)
    elif args.command == "merge":
        merge_manifests(args.root)
    elif args.command == "package-release":
        package_release(args.root, args.output, args.label, expected_count=variant_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
