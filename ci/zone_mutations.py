"""Mutation check for the strand-zone replay: plant each known-wrong rule in a
copy of src/strand_zone_policy.h and confirm ci/zone_replay_adapter.cpp
diverges on the spec's traces. A mutation that replays cleanly means the
traces no longer reach that rule.

    python ci/zone_mutations.py [--traces N] [--steps N] [--seed S]
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ci"))
import quint_replay  # noqa: E402

# (what the mutation breaks, text in the header, replacement)
MUTATIONS = [
    ("passage proof fires on every sample, not once per arrival",
     "s.passage = 0u;\n    return fire;", "return fire;"),
    ("an unmeasured sample keeps what was measured",
     "        s.exact = 0u;\n        s.reached_head = 0u;\n        s.passage = 0u;\n"
     "        s.consumed = 0;\n        if (driving_fwd)", "        if (driving_fwd)"),
    ("the first reading after boot is taken as an edge",
     "    if (!s.key_valid)\n    {", "    if (false)\n    {"),
    ("a backward float drops the front (the old slack)",
     "else if (s.last_dir < 0 && (!s.front_known || s.front <= 0))",
     "else if (s.last_dir < 0)"),
    ("passage without the printer consuming",
     "else if (s.exact && consuming && moved > c.head)",
     "else if (s.exact && moved > c.head)"),
    ("no write-ahead",
     "    if (s.zone == kUpstream && !s.tail_known && s.front_known && s.exact &&\n"
     "        s.front > c.down_clear)", "    if (false)"),
    ("a short tail piece never becomes a remnant",
     "if (s.front_known && s.front > 0) s.remnant = 1u;",
     "if (s.front_known && s.front > c.down_clear) s.remnant = 1u;"),
    ("the proof forgets a tail piece still in its branch",
     "        s.remnant = 0u;\n        s.reached_head = 0u;",
     "        s.remnant = 0u;\n        s.tail_known = 0u;\n        s.reached_head = 0u;"),
    ("the owner is not forced downstream at boot",
     "    if (owner && z != kTail) z = kDownstream;", ""),
    ("no record restores to the junction, not the head",
     "s.boot_front = valid ? c.down_set : c.head;", "s.boot_front = c.down_set;"),
    ("unseen forward drive is not assumed past the merger",
     "            s.front_known = 1u;\n            s.dir_acc = c.dir_band;",
     "            s.dir_acc = c.dir_band;"),
    ("a claim drops the measured front (the proof can never fire)",
     "    if (s.exact && s.front_known && !s.tail_known)\n    {", "    if (false)\n    {"),
    ("a claim is never released by the unload",
     "    if (s.claimed && s.last_dir < 0 && s.front_known && s.front < c.down_clear)\n"
     "        s.claimed = 0u;", ""),
]


def run(traces: int, steps: int, seed: str) -> list[tuple[str, int]]:
    """Returns (mutation, replay exit status) for each mutation; 1 is caught."""
    header = (ROOT / "src" / "strand_zone_policy.h").read_text(encoding="utf-8")
    spec = quint_replay.SPECS["zone"][0]
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        itf = tmp_path / "itf"
        itf.mkdir()
        for step in quint_replay.SPECS["zone"][3]:
            quint_replay.generate(spec, itf, traces // 2, steps, seed, step)
        quint_replay.scenarios(spec, itf)
        inc = tmp_path / "inc"
        inc.mkdir()
        for name, old, new in MUTATIONS:
            if old not in header:
                raise SystemExit(f"mutation no longer applies: {name}")
            (inc / "strand_zone_policy.h").write_text(header.replace(old, new, 1),
                                                      encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = quint_replay.main(["--spec", "zone", "--itf-dir", str(itf)],
                                       [f"-I{inc}"])
            results.append((name, rc))
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--traces", type=int, default=400)
    parser.add_argument("--steps", type=int, default=80)
    parser.add_argument("--seed", default="21")
    args = parser.parse_args(argv)
    missed = 0
    for name, rc in run(args.traces, args.steps, args.seed):
        print(f"{'caught' if rc == 1 else 'MISSED'}  {name}")
        missed += rc != 1
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
