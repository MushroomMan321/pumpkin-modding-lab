#!/usr/bin/env python3
"""Does a chunk keep changes made while it was loaded once it unloads? Isolated check.

Force-loads chunk (2,0), places a barrel, writes items, block-entity data and chunk data through
the mfeas plugin, removes the forceload, waits for the chunk to unload, force-loads it again and
reads everything back. Compares with chunk (0,0), which stays loaded throughout.

  python3 scripts/feas_chunk_unload.py [--bin ~/psb/bin/pumpkin-2c7931a-fl] [--wait 20]
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from feas_machine_headless import PSB, Run, prepare, start  # noqa: E402

FAR = (36, 100, 4)
NEAR = (4, 100, 4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=str(PSB / "bin" / "pumpkin-2c7931a-fl"))
    ap.add_argument("--wait", type=float, default=20.0)
    a = ap.parse_args()
    run_dir = PSB / "run" / "feas-chunk-unload"
    prepare(run_dir)
    con = start(Path(a.bin).expanduser(), run_dir, "unload")
    r = Run(con)
    try:
        r.ok("forceload add 0 0 47 15", "marked|already|forc")
        time.sleep(4)
        for p, tag in ((NEAR, "near"), (FAR, "far")):
            r.ok(f"setblock {p[0]} {p[1]} {p[2]} minecraft:barrel", "changed")
            r.cmd(f"mfeas put {p[0]} {p[1]} {p[2]} 0 7 minecraft:cobblestone")
            r.cmd(f"mfeas mark {p[0]} {p[1]} {p[2]} {tag}-be")
        r.cmd("mfeas chunkmark 0 0 near-chunk")
        r.cmd("mfeas chunkmark 2 0 far-chunk")
        r.info("far before unload", r.cmd(f"mfeas be {FAR[0]} {FAR[1]} {FAR[2]}"))

        r.info("forceload remove (far only)", r.ok("forceload remove 32 0 47 15", "unmarked|forc|not"))
        time.sleep(a.wait)
        r.info("far chunk right after the wait", r.cmd("mfeas chunkread 2 0"))
        r.ok("forceload add 32 0 47 15", "marked|already|forc")
        time.sleep(5)

        out = r.cmd(f"mfeas be {FAR[0]} {FAR[1]} {FAR[2]}")
        r.check("0:cobblestonex7" in out, "far barrel keeps its items across an unload", out)
        out = r.cmd(f"mfeas read {FAR[0]} {FAR[1]} {FAR[2]}")
        r.check("far-be" in out, "far block-entity plugin data survives an unload", out)
        out = r.cmd("mfeas chunkread 2 0")
        r.check("far-chunk" in out, "far chunk plugin data survives an unload", out)
        out = r.cmd(f"mfeas be {NEAR[0]} {NEAR[1]} {NEAR[2]}")
        r.check("0:cobblestonex7" in out, "near barrel (never unloaded) still has its items", out)
        out = r.cmd("mfeas chunkread 0 0")
        r.check("near-chunk" in out, "near chunk data (never unloaded) still there", out)
    except Exception as e:
        r.check(False, "aborted", repr(e))
    finally:
        con.stop("stop", timeout=120)
    fails = [x for x in r.results if x[0] == "FAIL"]
    print(f"\n=== {sum(1 for x in r.results if x[0] == 'PASS')} passed, {len(fails)} failed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
