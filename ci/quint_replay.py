"""Replay Quint traces against the firmware policies.

    python ci/quint_replay.py [--spec merger|zone|all] [--traces N] [--steps N]
                              [--seed S] [--itf-dir DIR]

For each spec, generates N random traces with `quint run --mbt` (or reads
existing ITF files from --itf-dir, which then needs a single --spec), converts
each step into the line format its adapter documents, builds the adapter with
zig c++ and feeds it the lot. Exit status: 0 no divergence, 1 a divergence
(printed), 2 a harness error.

    merger  spec/bmcu_merger.qnt  -> ci/quint_replay_adapter.cpp
            (owner and TAIL, through ams_merger_policy.h)
    zone    spec/strand_zone.qnt  -> ci/zone_replay_adapter.cpp
            (every field of every channel's estimate, through
            strand_zone_policy.h)
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

# Phases whose AMS-side motion is on_use / before_on_use / stop_on_use: the
# 0xFF idle handler returns early for these (bambu_bus_ams.cpp).
IN_USE_PHASES = {"PURGE", "PRINT", "PAUSE", "JAM", "RUNOUT", "TAIL"}

ZONE_CHANNELS = 3


def _int(v):
    if isinstance(v, dict) and "#bigint" in v:
        return int(v["#bigint"])
    return int(v)


def _map(v):
    return {_int(k): val for k, val in v["#map"]}


def _tag(v):
    return v["tag"] if isinstance(v, dict) and "tag" in v else v


def _picked_channel(state) -> int:
    pick = state.get("mbt::nondetPicks", {}).get("x")
    if pick and pick.get("tag") == "Some":
        return _int(pick["value"])
    return -1


def merger_lines(path: Path) -> list[str]:
    states = json.loads(path.read_text(encoding="utf-8"))["states"]
    name = path.name
    lines = [f"reset {name}"]
    for i in range(1, len(states)):
        prev, cur = states[i - 1], states[i]
        # lastAction refines the action name where one spec action stands for
        # two firmware paths (pullBack / jamPullBack, redetectFound /
        # redetectFoundShort).
        prev_owner = _int(prev["owner"])
        prev_phase = {k: _tag(v) for k, v in _map(prev["phase"]).items()}
        owner_in_use = int(prev_owner >= 0 and prev_phase[prev_owner] in IN_USE_PHASES)
        lines.append(" ".join(str(x) for x in (
            name, i, cur["lastAction"], _picked_channel(cur), owner_in_use,
            _int(cur["owner"]), int(bool(cur["inTail"])))))
    return lines


def zone_lines(path: Path) -> list[str]:
    states = json.loads(path.read_text(encoding="utf-8"))["states"]
    name = path.name
    lines = [f"reset {name}"]
    for i in range(1, len(states)):
        cur = states[i]
        ch = _int(cur["lastCh"])
        has, front = _map(cur["has"]), _map(cur["front"])
        # The switch reading after the step (strand_zone.qnt keyOf).
        key = int(ch >= 0 and bool(has[ch]) and _int(front[ch]) >= 0)
        fields = [name, i, cur["lastAction"], ch, _int(cur["lastDelta"]),
                  int(bool(cur["lastMeasured"])), int(bool(cur["lastDriving"])), key,
                  _int(cur["owner"])]
        est = _map(cur["est"])
        for c in range(ZONE_CHANNELS):
            e = est[c]
            fields += [_int(e["zone"]), _int(e["front"]), int(e["known"]),
                       int(e["tailKnown"]), int(e["remnant"]), int(e["exact"]),
                       int(e["reachedHead"]), _int(e["lastDir"]), int(e["key"])]
        lines.append(" ".join(str(x) for x in fields))
    return lines


SPECS = {
    "merger": (ROOT / "spec" / "bmcu_merger.qnt", ROOT / "ci" / "quint_replay_adapter.cpp",
               merger_lines),
    "zone": (ROOT / "spec" / "strand_zone.qnt", ROOT / "ci" / "zone_replay_adapter.cpp",
             zone_lines),
}


def generate(spec: Path, itf_dir: Path, traces: int, steps: int, seed: str | None) -> None:
    quint = shutil.which("quint")
    if quint is None:
        raise SystemExit("quint is not installed (npm i -g @informalsystems/quint)")
    # The typescript backend needs nothing downloaded at run time.
    command = [
        quint, "run", str(spec), "--backend=typescript", "--mbt",
        f"--max-samples={traces}", f"--n-traces={traces}", f"--max-steps={steps}",
        "--invariant=safety", f"--out-itf={itf_dir}/trace_{{seq}}.itf.json",
    ]
    if seed:
        command.append(f"--seed={seed}")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout[-4000:] + result.stderr[-2000:])
        raise SystemExit(f"quint run {spec.name} failed (safety violated or spec error)")


def build_adapter(source: Path, out: Path, defines: list[str] = ()) -> None:
    zig = shutil.which("zig")
    if zig is None:
        raise SystemExit("zig is not installed")
    env = os.environ.copy()
    env["ZIG_GLOBAL_CACHE_DIR"] = str(ROOT / ".pio" / "zig-global")
    env["ZIG_LOCAL_CACHE_DIR"] = str(ROOT / ".pio" / "zig-local")
    subprocess.run([zig, "c++", "-x", "c++", "-std=c++11", *defines, "-Isrc", str(source),
                    "-o", str(out)], cwd=ROOT, env=env, check=True)


def replay(kind: str, args, tmp_path: Path, defines: list[str] = ()) -> int:
    spec, adapter, to_lines = SPECS[kind]
    itf_dir = args.itf_dir
    if itf_dir is None:
        itf_dir = tmp_path / f"itf-{kind}"
        itf_dir.mkdir()
        generate(spec, itf_dir, args.traces, args.steps, args.seed)
    files = sorted(itf_dir.glob("*.itf.json"))
    if not files:
        raise SystemExit(f"no ITF traces in {itf_dir}")

    script = []
    for f in files:
        script.extend(to_lines(f))

    exe = tmp_path / f"{adapter.stem}.exe"
    build_adapter(adapter, exe, defines)
    result = subprocess.run([str(exe)], input="\n".join(script) + "\n",
                            capture_output=True, text=True)
    sys.stdout.write(f"[{kind}] " + result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def main(argv=None, defines: list[str] = ()) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--spec", choices=["merger", "zone", "all"], default="all")
    parser.add_argument("--traces", type=int, default=200)
    parser.add_argument("--steps", type=int, default=40)
    parser.add_argument("--seed")
    parser.add_argument("--itf-dir", type=Path)
    args = parser.parse_args(argv)
    kinds = ["merger", "zone"] if args.spec == "all" else [args.spec]
    if args.itf_dir is not None and len(kinds) != 1:
        parser.error("--itf-dir needs --spec merger or --spec zone")

    status = 0
    with tempfile.TemporaryDirectory() as tmp:
        for kind in kinds:
            rc = replay(kind, args, Path(tmp), defines)
            if rc != 0:
                return rc
    return status


if __name__ == "__main__":
    sys.exit(main())
