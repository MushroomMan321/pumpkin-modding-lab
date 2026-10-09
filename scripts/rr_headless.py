#!/usr/bin/env python3
"""Headless checks for Pumpkin PR #3887 (runtime block and item registries).

Runs Pumpkin built from the PR branch (plus the forceload patch, so chunks load without a player)
with the rrtest plugin, a v0.2 plugin that registers two blocks and an item at load. Three server
sessions on one world:

  one   register, compare state numbering against oak_stairs, place custom blocks, try to
        register after startup
  two   restart with the plugin trying to read chunk (0, 0) before it registers anything (the
        window between world load and registry freeze from the review), then edit that chunk.
        On 6efbe86 the read does not load the chunk, so the case is reported as not exercised
  three restart normally: are the custom blocks still on disk, and was the edit from session two
        saved?

  python3 scripts/rr_headless.py --bin ~/psb/bin/pumpkin-rr-fl

The plugin is pumpkin/rr-test. Build it with the PR's v0.2 WIT copied into pumpkin/rr-test/wit
(crates/pumpkin-plugin-wit/v0.2 on the PR branch):
cargo build --release --target wasm32-wasip2
"""

import argparse
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "harness"))
from psb import PSB, SERVERS, BENCH, Console, retry_command  # noqa: E402

ANY = r"rrtlog [^\n]*|Unknown|Incorrect|not loaded"
CRUSHER, CRUSHER_LIT, CONTROL = (4, 100, 4), (6, 100, 4), (8, 100, 4)  # chunk (0, 0)
STAIRS = (36, 100, 4)  # chunk (2, 0)
HELD = False  # whether session two really read chunk (0, 0) before the registry froze


def HELD_NOTE():
    return " [chunk was read before the freeze]" if HELD else " [ordinary chunk, case not exercised]"


class Run:
    def __init__(self, con):
        self.con = con
        self.results = []
        self.seen = 0

    def check(self, ok, what, detail=""):
        tag = "PASS" if ok else "FAIL"
        self.results.append((tag, what, detail))
        print(f"{tag} {what}" + (f"  [{detail}]" if detail else ""), flush=True)

    def info(self, what, detail=""):
        self.results.append(("INFO", what, detail))
        print(f"INFO {what}  [{detail}]", flush=True)

    def log(self):
        return Path(self.con.log.name).read_text(errors="replace")

    def grep(self, rx):
        return re.findall(rx, self.log())

    def one(self, rx):
        m = self.grep(rx)
        return m[-1] if m else ""

    def rrt(self, args, timeout=30):
        return self.con.command(f"rrt {args}", ANY, timeout).group(0)

    def get(self, p):
        return self.rrt(f"get {p[0]} {p[1]} {p[2]}")

    def ok(self, command, pattern, timeout=30):
        return self.con.command(command, rf"(?i)({pattern})|not loaded|unknown|incorrect|could not|"
                                         r"invalid|too many", timeout).group(0)


def prepare(run_dir, wasm, keep=False):
    if run_dir.exists() and not keep:
        shutil.rmtree(run_dir)
    if not run_dir.exists():
        shutil.copytree(SERVERS / "pumpkin", run_dir, ignore=shutil.ignore_patterns("world", "logs", "plugins"))
        toml = run_dir / "pumpkin.toml"
        toml.write_text(toml.read_text().replace(
            "[plugins.overrides]\n",
            '[plugins.overrides]\n\n[plugins.overrides.rrtest]\nenabled = true\n'
            'allowed_permissions = ["registry.blocks", "registry.items", "fs.read.data"]\n'))
    plugins = run_dir / "plugins"
    plugins.mkdir(exist_ok=True)
    shutil.copy(wasm, plugins / "rrtest.wasm")


def start(binary, run_dir, tag):
    log = run_dir / f"server-{tag}.log"
    con = Console(["taskset", "-c", "5-9,15-19", str(binary)], run_dir, log)
    con.wait_for(r"Server is now running|rrtest[^\n]*(fail|error|denied)", timeout=300)
    retry_command(con, "rrt ids", r"rrtlog (block|item|unknown)", attempts=60)
    return con


def preread_file(run_dir):
    """The plugin's data folder, found by the folder named after it."""
    hits = [p for p in run_dir.rglob("rrtest") if p.is_dir()]
    return (hits[0] if hits else run_dir / "plugins" / "rrtest") / "preread"


def session_one(r, run_dir):
    r.info("vanilla counts", r.one(r"rrtlog vanilla [^\n]*"))
    reg = r.one(r"rrtlog register crusher -> [^\n]*")
    again = r.one(r"rrtlog register crusher again -> [^\n]*")
    r.check("Ok(" in reg, "register-block returns an id", reg)
    r.check(reg.split("->")[-1] == again.split("->")[-1], "the same definition again gives the same id",
            again)
    changed = r.one(r"rrtlog register crusher changed -> [^\n]*")
    r.check("Err(" in changed, "a different definition for a known key fails", changed)
    r.check("Ok(" in r.one(r"rrtlog register stairs -> [^\n]*"), "second block registers")
    item = r.one(r"rrtlog register item -> [^\n]*")
    r.check("Ok(" in item, "register-item returns an id", item)
    link = r.one(r"rrtlog link -> [^\n]*")
    r.check("Ok(" in link, "set-block-item links item and block", link)
    tag = r.one(r"rrtlog block tag -> [^\n]*")
    r.check("Ok(" in tag, "register-block-tag (minecraft:climbable)", tag)
    blocks = r.grep(r"rrtlog block rrtest:[^\n]*")
    r.info("registered blocks", " | ".join(blocks[:2]))
    stairs = r.one(r"rrtlog stairs [^\n]*")
    r.check("mismatches=0" in stairs and "combos=80" in stairs,
            "stairs with oak_stairs' properties: all 80 states numbered like vanilla", stairs)

    r.ok("forceload add 0 0 47 15", "marked|already|forc")
    time.sleep(4)
    out = r.ok(f"setblock {CRUSHER[0]} {CRUSHER[1]} {CRUSHER[2]} rrtest:crusher", "changed|placed|set")
    r.info("vanilla /setblock with a custom block key", out)
    out = r.get(CRUSHER)
    if "rrtest:crusher" not in out:
        r.rrt(f"set {CRUSHER[0]} {CRUSHER[1]} {CRUSHER[2]} rrtest:crusher")
        out = r.get(CRUSHER)
    r.check("rrtest:crusher" in out, "custom block placed and read back by key", out)
    r.rrt(f"set {CRUSHER_LIT[0]} {CRUSHER_LIT[1]} {CRUSHER_LIT[2]} rrtest:crusher facing=east lit=true")
    out = r.get(CRUSHER_LIT)
    r.check("rrtest:crusher" in out, "custom block with non-default state placed", out)
    r.rrt(f"set {STAIRS[0]} {STAIRS[1]} {STAIRS[2]} rrtest:stairs facing=west half=top")
    out = r.get(STAIRS)
    r.check("rrtest:stairs" in out, "second custom block placed in another chunk", out)
    r.ok(f"setblock {CONTROL[0]} {CONTROL[1]} {CONTROL[2]} minecraft:stone", "changed")

    r.rrt("late")
    late = r.one(r"rrtlog late register -> [^\n]*")
    r.check("Err(" in late, "register-block after startup is refused", late)
    late_item = r.one(r"rrtlog late item -> [^\n]*")
    r.check("Err(" in late_item, "register-item after startup is refused", late_item)

    f = preread_file(run_dir)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"{CRUSHER[0]} {CRUSHER[1]} {CRUSHER[2]}\n")
    r.info("preread file for session two", str(f.relative_to(run_dir)))


def session_two(r, run_dir):
    global HELD
    pre = r.one(r"rrtlog preread [^\n]*")
    r.info("plugin read 4 100 4 before registering anything", pre or "no preread line")
    warn = r.one(r"Chunk -?\d+, -?\d+ names blocks that are not registered yet[^\n]*")
    HELD = bool(warn)
    if HELD:
        r.info("server warning: chunk read before the registry froze", warn)
    else:
        r.info("chunk-read-before-registration case NOT exercised",
               "the early read did not load the chunk and the server printed no "
               "'not registered yet' warning, so chunk (0, 0) below is an ordinary chunk")
    r.ok("forceload add 0 0 47 15", "marked|already|forc")
    time.sleep(4)
    out = r.get(CRUSHER)
    r.info(f"custom block in chunk (0, 0) after startup, in memory{HELD_NOTE()}", out)
    out = r.get(STAIRS)
    r.check("rrtest:stairs" in out, "custom block in chunk (2, 0) survives a restart", out)
    r.ok(f"setblock {CONTROL[0]} {CONTROL[1]} {CONTROL[2]} minecraft:gold_block", "changed")
    out = r.get(CONTROL)
    r.info(f"edit in chunk (0, 0) (stone -> gold_block), in memory{HELD_NOTE()}", out)
    preread_file(run_dir).unlink(missing_ok=True)


def session_three(r, run_dir):
    r.ok("forceload add 0 0 47 15", "marked|already|forc")
    time.sleep(4)
    out = r.get(CRUSHER)
    r.check("rrtest:crusher" in out, f"custom block in chunk (0, 0) after a second restart{HELD_NOTE()}", out)
    out = r.get(CRUSHER_LIT)
    r.check("rrtest:crusher" in out, f"its non-default-state neighbour too{HELD_NOTE()}", out)
    out = r.get(STAIRS)
    r.check("rrtest:stairs" in out, "custom block in chunk (2, 0) after a second restart", out)
    out = r.get(CONTROL)
    r.check("gold_block" in out, f"the edit made in chunk (0, 0) in session two was saved{HELD_NOTE()}", out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=str(PSB / "bin" / "pumpkin-rr-fl"))
    ap.add_argument("--wasm", default=str(BENCH / "pumpkin" / "rr-test" / "target" / "wasm32-wasip2" /
                                          "release" / "rrtest.wasm"))
    a = ap.parse_args()
    run_dir = PSB / "run" / "rr"
    prepare(run_dir, a.wasm)
    all_results = []
    sessions = (("one", session_one), ("two", session_two), ("three", session_three))
    for i, (tag, fn) in enumerate(sessions):
        if i:
            prepare(run_dir, a.wasm, keep=True)
        con = start(Path(a.bin).expanduser(), run_dir, tag)
        r = Run(con)
        try:
            print(f"=== session {tag}", flush=True)
            fn(r, run_dir)
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
