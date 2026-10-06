#!/usr/bin/env python3
"""Headless half of the machine-layer feasibility check (docs/machine-feasibility.md).

Runs Pumpkin with only the mfeas plugin (pumpkin/feas-machine) on the forceload-patched build, and
checks from the console: container access on barrels, hoppers and furnaces; hopper -> barrel
transfer; plugin data on block entities, chunks and the plugin's data folder; a ticking crusher;
and what survives a chunk reload and a full server restart. Every check prints PASS/FAIL/INFO.

  python3 scripts/feas_machine_headless.py [--bin ~/psb/bin/pumpkin-2c7931a-fl]
"""

import argparse
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "harness"))
from psb import PSB, SERVERS, BENCH, Console, retry_command  # noqa: E402

WASM = BENCH / "pumpkin" / "target" / "wasm32-wasip2" / "release" / "psb_feas_machine.wasm"
ANY = r"mfeas [a-z]+ .*|Unknown|Incorrect|not loaded|no block entity|not a|players only|no overworld"
BARREL, HOPPER, FURNACE = (4, 100, 4), (4, 101, 4), (8, 100, 4)
FAR_BARREL = (36, 100, 4)  # chunk (2, 0)


class Run:
    def __init__(self, con):
        self.con = con
        self.results = []

    def cmd(self, command, pattern=ANY, timeout=30):
        return self.con.command(command, pattern, timeout).group(0)

    def check(self, ok, what, detail=""):
        tag = "PASS" if ok else "FAIL"
        self.results.append((tag, what, detail))
        print(f"{tag} {what}" + (f"  [{detail}]" if detail else ""), flush=True)

    def info(self, what, detail=""):
        self.results.append(("INFO", what, detail))
        print(f"INFO {what}  [{detail}]", flush=True)

    def be(self, p):
        return self.cmd(f"mfeas be {p[0]} {p[1]} {p[2]}")

    def ok(self, command, pattern, timeout=30):
        return self.con.command(command, rf"(?i)({pattern})|not loaded|unknown|incorrect|could not|too many",
                                timeout).group(0)

    def log_grep(self, rx):
        text = Path(self.con.log.name).read_text(errors="replace")
        return re.findall(rx, text)


def prepare(run_dir, keep=False):
    if run_dir.exists() and not keep:
        shutil.rmtree(run_dir)
    if not run_dir.exists():
        shutil.copytree(SERVERS / "pumpkin", run_dir, ignore=shutil.ignore_patterns("world", "logs", "plugins"))
        toml = run_dir / "pumpkin.toml"
        text = toml.read_text().replace(
            "[plugins.overrides]\n",
            '[plugins.overrides]\n\n[plugins.overrides.mfeas]\nenabled = true\n'
            'allowed_permissions = ["fs.read.data", "fs.write.data"]\n')
        toml.write_text(text)
    plugins = run_dir / "plugins"
    plugins.mkdir(exist_ok=True)
    shutil.copy(WASM, plugins / "psb_feas_machine.wasm")


def start(binary, run_dir, tag):
    log = run_dir / f"server-{tag}.log"
    con = Console(["taskset", "-c", "5-9,15-19", str(binary)], run_dir, log)
    con.wait_for(r"Server is now running", timeout=300)
    retry_command(con, "mfeas machines", r"mfeas machines", attempts=60)
    return con


def session_one(r):
    r.ok("forceload add 0 0 47 15", "marked|already|forc")
    time.sleep(4)
    r.ok(f"setblock {BARREL[0]} {BARREL[1]} {BARREL[2]} minecraft:barrel", "changed")
    r.ok(f"setblock {HOPPER[0]} {HOPPER[1]} {HOPPER[2]} minecraft:hopper", "changed")
    r.ok(f"setblock {FURNACE[0]} {FURNACE[1]} {FURNACE[2]} minecraft:furnace", "changed")
    r.ok(f"setblock {FAR_BARREL[0]} {FAR_BARREL[1]} {FAR_BARREL[2]} minecraft:barrel", "changed")

    out = r.be(BARREL)
    r.check("kind=barrel size=27 slots=empty" in out, "barrel block entity reachable, 27 slots, empty", out)
    out = r.be(FURNACE)
    r.check("kind=furnace size=3" in out, "furnace block entity reachable as a container", out)
    out = r.be(HOPPER)
    r.check("kind=hopper size=5" in out, "hopper block entity reachable as a container", out)

    out = r.cmd(f"mfeas put {BARREL[0]} {BARREL[1]} {BARREL[2]} 0 5 minecraft:cobblestone")
    r.check("0:cobblestonex5" in out, "plugin writes a stack into a barrel slot", out)

    r.cmd(f"mfeas put {HOPPER[0]} {HOPPER[1]} {HOPPER[2]} 0 3 minecraft:iron_ingot")
    time.sleep(4)
    out = r.be(BARREL)
    hop = r.be(HOPPER)
    r.check("iron_ingot" in out, "a vanilla hopper pushes into the barrel (automation works)", f"{out} | {hop}")

    out = r.cmd(f"mfeas mark {BARREL[0]} {BARREL[1]} {BARREL[2]} hello-be")
    r.check("value=hello-be" in out, "plugin data set on a block entity", out)
    out = r.cmd(f"mfeas read {BARREL[0]} {BARREL[1]} {BARREL[2]}")
    r.check("value=hello-be" in out, "plugin data read back from the block entity", out)

    out = r.cmd("mfeas chunkmark 0 0 hello-chunk")
    r.check("value=hello-chunk" in out, "plugin data set on a chunk", out)
    out = r.cmd("mfeas chunkread 0 0")
    r.check("value=hello-chunk" in out, "plugin data read back from the chunk", out)

    out = r.cmd("mfeas filewrite persist-me")
    r.check("filewrite ok" in out, "plugin writes a file in its data folder", out)
    out = r.cmd("mfeas fileread")
    r.check("value=persist-me" in out, "plugin reads the file back", out)

    out = r.cmd(f"mfeas crusher {BARREL[0]} {BARREL[1]} {BARREL[2]}")
    r.check("registered" in out, "crusher registered on the barrel", out)
    time.sleep(8)
    out = r.be(BARREL)
    r.check("26:gravelx5" in out and "0:cobblestone" not in out,
            "crusher turned 5 cobblestone into 5 gravel over 100 ticks", out)
    out = r.cmd("mfeas machines")
    r.check("crushed=5" in out, "machine counter in plugin memory", out)

    # A machine in another chunk, then unload and reload that chunk.
    r.cmd(f"mfeas crusher {FAR_BARREL[0]} {FAR_BARREL[1]} {FAR_BARREL[2]}")
    r.cmd(f"mfeas put {FAR_BARREL[0]} {FAR_BARREL[1]} {FAR_BARREL[2]} 0 2 minecraft:cobblestone")
    out = r.ok("forceload remove 32 0 47 15", "unmarked|no longer|removed|forc")
    r.info("forceload remove reply", out)
    time.sleep(6)
    out = r.cmd("mfeas chunkread 2 0")
    r.info("chunk (2,0) after forceload remove", out)
    out = r.ok("forceload add 32 0 47 15", "marked|already|forc")
    time.sleep(6)
    loads = r.log_grep(r"mfeas chunk 2 0 loaded with machines=[^\n]*")
    r.info("chunk-load after forceload remove/add (the chunk may never have unloaded)",
           loads[-1] if loads else "no chunk-load line for (2,0)")


def session_two(r):
    out = r.cmd("mfeas machines")
    r.info("machines right after restart, before any forceload", out)
    out = r.cmd("mfeas chunkread 0 0")
    r.info("chunk (0,0) right after restart (are forced chunks reloaded?)", out)
    r.ok("forceload add 0 0 47 15", "marked|already|forc")
    time.sleep(6)
    loads = r.log_grep(r"mfeaslog chunk -?\d+ -?\d+ loaded with machines=[^\n]*")
    restored = r.log_grep(r"mfeaslog machine registered at [^\n]*source=chunk-load[^\n]*")
    r.info("chunk-load events seen (Pumpkin never fires ChunkLoadEvent)", f"{len(loads)} {loads[:3]}")
    out = r.cmd("mfeas machines")
    r.check("count=2" in out, "both machines restored from the data-folder index after a restart", out)
    r.info("restore log lines", f"{restored[:3]}")
    out_far = r.cmd("mfeas chunkread 2 0")
    r.info("chunk (2,0) machine list after restart", out_far)
    out = r.cmd(f"mfeas read {BARREL[0]} {BARREL[1]} {BARREL[2]}")
    r.check("value=hello-be" in out, "block-entity plugin data survives a restart", out)
    out = r.cmd("mfeas chunkread 0 0")
    r.check("value=hello-chunk" in out, "chunk plugin data survives a restart", out)
    out = r.cmd("mfeas fileread")
    r.check("value=persist-me" in out, "data-folder file survives a restart", out)
    out = r.be(BARREL)
    r.check("26:gravelx5" in out, "barrel contents survive a restart", out)
    r.cmd(f"mfeas put {BARREL[0]} {BARREL[1]} {BARREL[2]} 0 2 minecraft:cobblestone")
    time.sleep(4)
    out = r.be(BARREL)
    r.check("26:gravelx7" in out, "a restored machine keeps working", out)
    out = r.cmd("mfeas machines")
    r.check("4,100,4:crushed=7" in out, "machine counter continues from block-entity data (5 + 2)", out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=str(PSB / "bin" / "pumpkin-2c7931a-fl"))
    a = ap.parse_args()
    run_dir = PSB / "run" / "feas-machine"
    prepare(run_dir)
    all_results = []
    for tag, fn, keep in (("one", session_one, False), ("two", session_two, True)):
        if keep:
            prepare(run_dir, keep=True)
        con = start(Path(a.bin).expanduser(), run_dir, tag)
        r = Run(con)
        try:
            print(f"=== session {tag}", flush=True)
            fn(r)
        except Exception as e:  # report and still stop the server cleanly
            r.check(False, f"session {tag} aborted", repr(e))
        finally:
            con.stop("stop", timeout=120)
        all_results += r.results
    fails = [x for x in all_results if x[0] == "FAIL"]
    print(f"\n=== {sum(1 for x in all_results if x[0] == 'PASS')} passed, {len(fails)} failed, "
          f"{sum(1 for x in all_results if x[0] == 'INFO')} info")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
