"""Replay Quint traces of spec/bmcu_merger.qnt against the firmware policies.

    python ci/quint_replay.py [--traces N] [--steps N] [--seed S] [--itf-dir DIR]

Generates N random traces with `quint run --mbt` (or reads existing ITF files
from --itf-dir), converts each step into the line format documented in
ci/quint_replay_adapter.cpp, builds that adapter with zig c++ and feeds it the
lot. Exit status is the adapter's: 0 no divergence, 1 a divergence (printed),
2 a harness error.

The spec is the oracle for owner, TAIL, the downstream mask and the
DOWNSTREAM_OCCUPIED report; the adapter drives ams_merger_policy.h and
downstream_strand_policy.h the way the firmware's call sites do.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "spec" / "bmcu_merger.qnt"
ADAPTER = ROOT / "ci" / "quint_replay_adapter.cpp"

# Phases whose AMS-side motion is on_use / before_on_use / stop_on_use: the
# 0xFF idle handler returns early for these (bambu_bus_ams.cpp).
IN_USE_PHASES = {"PURGE", "PRINT", "PAUSE", "RUNOUT", "TAIL"}


def _int(v):
    if isinstance(v, dict) and "#bigint" in v:
        return int(v["#bigint"])
    return int(v)


def _map(v):
    return {_int(k): val["tag"] if isinstance(val, dict) and "tag" in val else val
            for k, val in v["#map"]}


def _set(v):
    return {_int(x) for x in v["#set"]}


def _picked_channel(state) -> int:
    pick = state.get("mbt::nondetPicks", {}).get("x")
    if pick and pick.get("tag") == "Some":
        return _int(pick["value"])
    return -1


def itf_to_lines(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    states = data["states"]
    name = path.name
    lines = [f"reset {name}"]
    for i in range(1, len(states)):
        prev, cur = states[i - 1], states[i]
        action = cur["mbt::actionTaken"]
        ch = _picked_channel(cur)
        prev_owner = _int(prev["owner"])
        prev_phase = _map(prev["phase"])
        owner_in_use = int(prev_owner >= 0 and prev_phase[prev_owner] in IN_USE_PHASES)
        mask = sum(1 << c for c in _set(cur["down"]))
        occupied = int(action == "sendOut" and ch in _set(cur["reported"]))
        lines.append(" ".join(str(x) for x in (
            name, i, action, ch, owner_in_use, _int(cur["owner"]),
            int(bool(cur["inTail"])), mask, occupied)))
    return lines


def generate(itf_dir: Path, traces: int, steps: int, seed: str | None) -> None:
    quint = shutil.which("quint")
    if quint is None:
        raise SystemExit("quint is not installed (npm i -g @informalsystems/quint)")
    # The typescript backend needs nothing downloaded at run time.
    command = [
        quint, "run", str(SPEC), "--backend=typescript", "--mbt",
        f"--max-samples={traces}", f"--n-traces={traces}", f"--max-steps={steps}",
        "--invariant=safety", f"--out-itf={itf_dir}/trace_{{seq}}.itf.json",
    ]
    if seed:
        command.append(f"--seed={seed}")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout[-4000:] + result.stderr[-2000:])
        raise SystemExit("quint run failed (safety violated or spec error)")


def build_adapter(out: Path) -> None:
    zig = shutil.which("zig")
    if zig is None:
        raise SystemExit("zig is not installed")
    env = os.environ.copy()
    env["ZIG_GLOBAL_CACHE_DIR"] = str(ROOT / ".pio" / "zig-global")
    env["ZIG_LOCAL_CACHE_DIR"] = str(ROOT / ".pio" / "zig-local")
    subprocess.run([zig, "c++", "-x", "c++", "-std=c++11", "-Isrc", str(ADAPTER),
                    "-o", str(out)], cwd=ROOT, env=env, check=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--traces", type=int, default=200)
    parser.add_argument("--steps", type=int, default=40)
    parser.add_argument("--seed")
    parser.add_argument("--itf-dir", type=Path)
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        itf_dir = args.itf_dir
        if itf_dir is None:
            itf_dir = tmp_path / "itf"
            itf_dir.mkdir()
            generate(itf_dir, args.traces, args.steps, args.seed)
        files = sorted(itf_dir.glob("*.itf.json"))
        if not files:
            raise SystemExit(f"no ITF traces in {itf_dir}")

        script = []
        for f in files:
            script.extend(itf_to_lines(f))

        exe = tmp_path / "quint_replay_adapter.exe"
        build_adapter(exe)
        result = subprocess.run([str(exe)], input="\n".join(script) + "\n",
                                capture_output=True, text=True)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode


if __name__ == "__main__":
    sys.exit(main())
