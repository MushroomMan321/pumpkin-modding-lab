#!/usr/bin/env python3
"""Smoke test for the chain-mining plugin, used by qwen/gate-chainmine.sh. The Qwen loop may
not edit this file.

Starts Pumpkin with only the chainmine plugin, builds small scenes with /fill and /setblock,
runs `/chainmine test` on each and then checks every block of the scene with an independent
probe: `/loot spawn ... mine <pos>` answers "Block air has no loot table" for air and
"Dropped ..." for anything else, so the check does not trust the plugin's own output.

Needs a Pumpkin build whose /forceload actually loads chunks (stock Pumpkin's does not, see
docs/chainmine-feasibility.md): no player is connected, so nothing else loads them.
"""

import argparse
import datetime as dt
import os
import queue
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "harness"))
from psb import PSB, SERVERS, BENCH, Console, retry_command  # noqa: E402

WASM = Path(os.environ.get("CHAINMINE_WASM") or
            BENCH / "pumpkin" / "target" / "wasm32-wasip2" / "release" / "psb_plugin_chainmine.wasm")
PROBE_TOOL = "minecraft:diamond_pickaxe"
PROBE_DROP = "16 240 16"
REPLY = re.compile(r"Block air has no loot table|Dropped |has no loot table|not loaded|Unknown|Incorrect")


class Smoke:
    def __init__(self, con):
        self.con = con
        self.failures = []

    def drain(self, settle=1.0):
        time.sleep(settle)
        while True:
            try:
                self.con.lines.get_nowait()
            except queue.Empty:
                return

    def check(self, ok, what):
        print(("PASS " if ok else "FAIL ") + what, flush=True)
        if not ok:
            self.failures.append(what)

    def cmd(self, command, pattern, timeout=30):
        return self.con.command(command, pattern, timeout)

    def is_air(self, x, y, z):
        m = self.cmd(f"loot spawn {PROBE_DROP} mine {x} {y} {z} {PROBE_TOOL}", REPLY)
        if "not loaded" in m.group(0) or "Unknown" in m.group(0) or "Incorrect" in m.group(0):
            raise RuntimeError(f"probe at {x} {y} {z} failed: {m.group(0)}")
        return m.group(0) == "Block air has no loot table"

    def build(self, command, ok):
        """Runs a scene-building command; anything but its success reply aborts the smoke run."""
        m = self.cmd(command, rf"(?i)({ok})|not loaded|too many|unknown|incorrect|expected|could not")
        if not m.group(1):
            raise RuntimeError(f"scene command failed: /{command} -> {m.group(0)}")

    def fill(self, a, b, block):
        self.build(f"fill {a} {b} {block}", "filled")

    def setblock(self, pos, block):
        self.build(f"setblock {pos} {block}", "changed")

    def chain(self, label, origin, tool, block, extra):
        """Runs /chainmine test and checks the announced and the finished extra counts."""
        self.drain(0.2)
        m = self.cmd(f"chainmine test {origin} {tool}",
                     r"chainmine (test origin=\S+ extra=\d+|skip reason=\S+)")
        want = rf"test origin=(minecraft:)?{block} extra={extra}$"
        self.check(re.search(want, m.group(1)) is not None,
                   f"{label}: announced '{m.group(1)}', want origin={block} extra={extra}")
        if not m.group(1).startswith("test"):
            return
        try:
            done = self.con.wait_for(r"chainmine done extra=(\d+)", 60)
            self.check(int(done.group(1)) == extra, f"{label}: done extra={done.group(1)}, want {extra}")
        except TimeoutError:
            self.check(False, f"{label}: no 'chainmine done' line within 60 s")
        self.drain()

    def skip(self, label, origin, tool, reason):
        self.drain(0.2)
        m = self.cmd(f"chainmine test {origin} {tool}",
                     r"chainmine (test origin=\S+ extra=\d+|skip reason=\S+)")
        self.check(m.group(1) == f"skip reason={reason}",
                   f"{label}: got '{m.group(1)}', want 'skip reason={reason}'")
        self.drain()

    def expect(self, label, positions, air):
        wrong = [p for p in positions if self.is_air(*p) != air]
        state = "air" if air else "intact"
        self.check(not wrong, f"{label}: {len(positions)} blocks {state}" +
                   (f", wrong at {wrong[:6]}" if wrong else ""))


def scenes(s):
    # Clear the build volume (fill is capped at 32768 blocks per call).
    for y0 in (196, 212, 228):
        s.fill(f"0 {y0} 0", f"31 {y0 + 15} 31", "minecraft:air")

    # 1. A small vein with edge and corner links, next to a different ore, plus an isolated
    #    ore of the same kind and a stone block on top of the origin.
    vein = [(2, 200, 2), (3, 200, 2), (3, 201, 2), (4, 202, 3), (2, 200, 3), (1, 199, 1)]
    for p in vein:
        s.setblock("%d %d %d" % p, "minecraft:iron_ore")
    s.setblock("2 201 2", "minecraft:stone")
    s.setblock("2 200 1", "minecraft:deepslate_iron_ore")
    s.setblock("8 200 2", "minecraft:iron_ore")
    s.chain("vein", "2 200 2", PROBE_TOOL, "iron_ore", len(vein) - 1)
    # The broken blocks must have dropped something (items may merge, so at least one entity).
    m = s.cmd("kill @e[type=item]", r"Killed (\d+) entit|No entity was found")
    dropped = int(m.group(1)) if m.group(1) else 0
    s.check(dropped >= 1, f"vein: {dropped} item entities dropped, want at least 1")
    s.expect("vein", vein, air=True)
    s.expect("vein neighbours", [(2, 201, 2), (2, 200, 1), (8, 200, 2)], air=False)

    # 2. A 5x5x5 stone cube is more than the 64-block cap. Growth is nearest-first, so every
    #    block with squared distance <= 5 from the centre goes (57), 7 of the 24 at distance 6
    #    go, and everything at 8 or more stays.
    s.fill("10 200 10", "14 204 14", "minecraft:stone")
    s.chain("cube", "12 202 12", PROBE_TOOL, "stone", 63)
    cube = [(x, y, z) for x in range(10, 15) for y in range(200, 205) for z in range(10, 15)]
    d2 = {p: (p[0] - 12) ** 2 + (p[1] - 202) ** 2 + (p[2] - 12) ** 2 for p in cube}
    s.expect("cube core", [p for p in cube if d2[p] <= 5], air=True)
    s.expect("cube outer", [p for p in cube if d2[p] >= 8], air=False)
    shell_air = sum(s.is_air(*p) for p in cube if d2[p] == 6)
    s.check(shell_air == 7, f"cube shell: {shell_air} of 24 at distance 6 broken, want 7")

    # 3. A straight line of 20 ores is longer than the 16-block reach.
    s.fill("0 215 20", "19 215 20", "minecraft:iron_ore")
    s.chain("line", "0 215 20", PROBE_TOOL, "iron_ore", 16)
    s.expect("line in reach", [(x, 215, 20) for x in range(0, 17)], air=True)
    s.expect("line out of reach", [(x, 215, 20) for x in range(17, 20)], air=False)

    # 4. A log column; similar but different blocks touching it stay. (Pumpkin's /setblock
    #    cannot set block states, so ignoring states is left to the unit tests.)
    logs = [(25, 200, 25), (25, 201, 25), (25, 202, 25), (25, 203, 25)]
    for p in logs:
        s.setblock("%d %d %d" % p, "minecraft:oak_log")
    others = [((26, 201, 25), "minecraft:oak_planks"), ((24, 202, 25), "minecraft:stripped_oak_log"),
              ((25, 204, 25), "minecraft:birch_log")]
    for p, block in others:
        s.setblock("%d %d %d" % p, block)
    s.chain("logs", "25 200 25", "minecraft:diamond_axe", "oak_log", 3)
    s.expect("logs", logs, air=True)
    s.expect("other wood", [p for p, _ in others], air=False)

    # 5. Block entities and unbreakable blocks never chain and are not touched.
    s.setblock("28 200 5", "minecraft:barrel")
    s.setblock("29 200 5", "minecraft:barrel")
    s.skip("barrel", "28 200 5", "minecraft:diamond_axe", "excluded")
    s.expect("barrels", [(28, 200, 5), (29, 200, 5)], air=False)
    s.setblock("28 200 8", "minecraft:bedrock")
    s.skip("bedrock", "28 200 8", PROBE_TOOL, "excluded")
    s.expect("bedrock", [(28, 200, 8)], air=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pumpkin-bin", default=str(PSB / "bin" / "pumpkin-2c7931a-fl"))
    ap.add_argument("--cores", default="10-14")
    a = ap.parse_args()

    run_dir = PSB / "run" / f"{dt.datetime.now():%Y%m%d-%H%M%S}-chainmine-smoke"
    shutil.copytree(SERVERS / "pumpkin", run_dir, ignore=shutil.ignore_patterns("world", "logs", "plugins"))
    (run_dir / "plugins").mkdir()
    shutil.copy(WASM, run_dir / "plugins")
    log = run_dir / "server.log"
    con = Console(["taskset", "-c", a.cores, str(Path(a.pumpkin_bin).expanduser())], run_dir, log)
    s = Smoke(con)
    try:
        con.wait_for(r"Server is now running", 300)
        # Plugins load in the background: an air origin answers once the plugin is up.
        retry_command(con, "chainmine test 31 319 31 minecraft:stick", r"chainmine skip reason=air", attempts=60)
        s.check(True, "plugin loaded, air origin skipped")
        con.command("forceload add 0 0 31 31", r"(?i)force", timeout=30)
        retry_command(con, "setblock 0 250 0 minecraft:stone", r"(?i)changed", attempts=60)
        scenes(s)
    except (TimeoutError, RuntimeError) as e:
        s.check(False, f"smoke aborted: {e}")
        print(f"server log: {log}")
    finally:
        con.stop("stop")
    if s.failures:
        print(f"SMOKE FAILED: {len(s.failures)} check(s); server log: {log}")
        return 1
    shutil.rmtree(run_dir, ignore_errors=True)
    print("SMOKE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
