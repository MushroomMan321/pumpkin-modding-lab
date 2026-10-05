#!/usr/bin/env python3
"""Follows a Qwen loop run live: what it was thinking, which tool it called, what came back.

  python3 qwen/watch.py              # latest run, follow new turns
  python3 qwen/watch.py RUN_DIR      # a specific run
  python3 qwen/watch.py --once       # print what exists and exit
"""

import json
import sys
import time
from pathlib import Path

RUNS = Path.home() / "psb" / "bench" / "qwen" / "runs"
DIM, BOLD, GREEN, RED, CYAN, RESET = "\033[2m", "\033[1m", "\033[32m", "\033[31m", "\033[36m", "\033[0m"


def describe(entry):
    args = entry.get("args", {})
    tool = entry.get("tool", "?")
    target = args.get("path") or args.get("pattern") or args.get("summary") or ""
    if tool in ("write_file",):
        target += f"  ({len(str(args.get('content', '')))} chars)"
    return f"{BOLD}{tool}{RESET} {target}"


def show(entry):
    turn = entry.get("turn")
    secs = entry.get("secs")
    head = f"{CYAN}turn {turn}{RESET}" + (f" {DIM}{secs}s{RESET}" if secs is not None else "")
    print(f"\n{head}  {describe(entry)}")
    reasoning = (entry.get("reasoning") or "").strip()
    if reasoning:
        print(f"{DIM}  thinking: {reasoning[:600].replace(chr(10), ' ')}{RESET}")
    result = (entry.get("result") or "").strip()
    if result:
        lines = result.splitlines()
        colour = GREEN if "GATE PASSED" in result else RED if ("error" in result.lower() or "FAILED" in result) else ""
        for line in lines[-6:]:
            print(f"  {colour}{line[:160]}{RESET}")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    once = "--once" in sys.argv
    run = Path(args[0]) if args else max(RUNS.iterdir(), key=lambda p: p.stat().st_mtime)
    path = run / "turns.jsonl"
    print(f"{BOLD}{run.name}{RESET}")
    pos = 0
    while True:
        if path.exists():
            with open(path) as f:
                f.seek(pos)
                for line in f:
                    if line.endswith("\n"):
                        show(json.loads(line))
                        pos = f.tell()
        if once:
            return
        time.sleep(1)


if __name__ == "__main__":
    main()
