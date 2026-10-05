#!/usr/bin/env python3
"""Runs one benchmark configuration on the benchmark host and writes a JSON result.

Drives the server through its console (stdin/stdout), because Pumpkin has no RCON.
Every run starts from a fresh world, is pinned to the same CPU cores, and is checked
against the reference checksum before its timings are kept.

Example:
  python3 harness/psb.py --server pumpkin --variant wasm-batched --n 10000 --rate 10
  python3 harness/psb.py --server neoforge --variant block_entity --n 10000 --rate 10 --repeats 3
"""

import argparse
import atexit
import datetime as dt
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

PSB = Path.home() / "psb"
BENCH = PSB / "bench"
SERVERS = PSB / "servers"
RESULTS = BENCH / "results" / "raw"
JAVA = PSB / "opt" / "jdk-25" / "bin" / "java"
CHECKSUM = BENCH / "workload" / "target" / "release" / "checksum"

# Fast cores on GB10 (Cortex-X925). Efficiency cores are 0-4 and 10-14.
DEFAULT_CORES = "5-9,15-19"
INFERENCE_PROCESS = "gb10_inference"

PROBE_RE = re.compile(
    r"probe ticks=(\d+) mean_us=([\d.]+) p50_us=([\d.]+) p95_us=([\d.]+) p99_us=([\d.]+) max_us=([\d.]+)")
STATUS_RE = re.compile(r"psb status tick=(\d+) n=(\d+) rate=(\d+) energy_sum=([0-9a-f]{16}) writes=(\d+)")


class Console:
    """A server process whose stdout is collected line by line."""

    def __init__(self, argv, cwd, log_path, env=None):
        self.log = open(log_path, "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1, errors="replace")
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.log.write(line)
            self.log.flush()
            self.lines.put(line.rstrip("\n"))
        self.lines.put(None)

    def send(self, command):
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()

    def wait_for(self, pattern, timeout):
        rx = re.compile(pattern) if isinstance(pattern, str) else pattern
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for {rx.pattern!r}")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise RuntimeError(f"server exited (code {self.proc.poll()}) while waiting for {rx.pattern!r}")
            m = rx.search(line)
            if m:
                return m

    def command(self, command, pattern, timeout=30):
        self.send(command)
        return self.wait_for(pattern, timeout)

    def stop(self, stop_command, timeout=120):
        if self.proc.poll() is None:
            try:
                self.send(stop_command)
                self.proc.wait(timeout=timeout)
            except Exception:
                self.proc.kill()
                self.proc.wait()
        self.log.close()


def proc_cpu_seconds(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    utime, stime = int(fields[11]), int(fields[12])
    return (utime + stime) / os.sysconf("SC_CLK_TCK")


def proc_rss_mib(pid):
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return None


def file_sha256(path):
    h = hashlib.sha256()
    with open(Path(path).expanduser(), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def expected_checksum(n, rate, ticks):
    out = subprocess.run([str(CHECKSUM), str(n), str(rate), str(ticks)],
                         check=True, capture_output=True, text=True).stdout
    m = re.search(r"energy_sum=([0-9a-f]{16}) writes=(\d+)", out)
    return m.group(1), int(m.group(2))


def side(n):
    s = 1
    while s * s < n:
        s += 1
    return s


class Inference:
    """Pauses the model-serving process for the duration of a run (SIGSTOP/SIGCONT).
    Resuming is guaranteed on every exit path Python can see: normal exit, exceptions,
    Ctrl-C, SIGTERM/SIGHUP (turned into SystemExit) and interpreter shutdown (atexit)."""

    def __init__(self, enabled):
        self.enabled = enabled
        self.pids = []

    def __enter__(self):
        if self.enabled:
            for sig in (signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, lambda *_: sys.exit(1))
            atexit.register(self.resume)
            out = subprocess.run(["pgrep", "-x", INFERENCE_PROCESS], capture_output=True, text=True).stdout
            self.pids = [int(p) for p in out.split()]
            for pid in self.pids:
                os.kill(pid, signal.SIGSTOP)
        return self

    def resume(self):
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass

    def __exit__(self, *exc):
        self.resume()


def launch(args, run_dir, log_path):
    cores = ["taskset", "-c", args.cores]
    if args.server == "neoforge":
        server_dir = SERVERS / "neoforge"
        shutil.copytree(server_dir, run_dir, symlinks=True,
                        ignore=shutil.ignore_patterns("world", "logs", "crash-reports"))
        mods = run_dir / "mods"
        mods.mkdir(exist_ok=True)
        shutil.copy(BENCH / "neoforge" / "build" / "libs" / "psb-1.0.0.jar", mods)
        unix_args = next((run_dir / "libraries" / "net" / "neoforged" / "neoforge").glob("*/unix_args.txt"))
        argv = cores + [str(JAVA), f"-Xms{args.heap}", f"-Xmx{args.heap}",
                        f"-Dpsb.variant={args.variant}", f"@{unix_args}", "--nogui"]
        return Console(argv, run_dir, log_path), r"Done \(", "stop"

    server_dir = SERVERS / "pumpkin"
    shutil.copytree(server_dir, run_dir, symlinks=True,
                    ignore=shutil.ignore_patterns("world", "logs", "plugins"))
    plugins = run_dir / "plugins"
    plugins.mkdir()
    wasm = BENCH / "pumpkin" / "target" / "wasm32-wasip2" / "release"
    shutil.copy(wasm / "psb_probe.wasm", plugins)
    shutil.copy(wasm / f"psb_{args.variant.replace('-', '_')}.wasm", plugins)
    argv = cores + [str(Path(args.pumpkin_bin).expanduser())]
    return Console(argv, run_dir, log_path), args.pumpkin_ready, "stop"


def run_once(args, rep):
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{args.server}-{args.variant}-n{args.n}-r{args.rate}-{rep}"
    RESULTS.mkdir(parents=True, exist_ok=True)
    run_dir = PSB / "run" / name
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    log_path = RESULTS / f"{name}.log"
    s = side(args.n)

    con, ready, stop_cmd = launch(args, run_dir, log_path)
    try:
        con.wait_for(ready, timeout=600)
        pid = con.proc.pid
        # Plugins load in the background on Pumpkin: wait until the probe answers.
        retry_command(con, "probe reset", r"probe reset", attempts=60)
        con.command(f"forceload add 0 0 {s - 1} {s - 1}", r"(?i)forc", timeout=60)
        time.sleep(args.settle)
        con.command(f"psb setup {args.n} {args.rate}", r"psb setup n=", timeout=600)
        time.sleep(args.settle)

        con.command(f"psb run {args.warmup}", r"psb running", timeout=30)
        time.sleep(args.warmup / 20)
        wait_ticks(con, args.warmup, args)

        cpu0, t0 = proc_cpu_seconds(pid), time.monotonic()
        con.command(f"probe arm {args.measure}", r"probe armed")
        con.command(f"psb run {args.measure}", r"psb running", timeout=30)
        # Status runs on the server thread, so stay quiet for the expected duration first.
        time.sleep(args.measure / 20)
        status = wait_ticks(con, args.warmup + args.measure, args)
        cpu1, t1 = proc_cpu_seconds(pid), time.monotonic()
        rss = proc_rss_mib(pid)
        probe = con.command("probe dump", PROBE_RE)
    finally:
        con.stop(stop_cmd)
        if not args.keep_run_dir:
            shutil.rmtree(run_dir, ignore_errors=True)

    total = args.warmup + args.measure
    want_sum, want_writes = expected_checksum(args.n, args.rate, total)
    got_sum, got_writes = status.group(4), int(status.group(5))
    ok = (got_sum, got_writes) == (want_sum, want_writes) and int(status.group(1)) == total

    result = {
        "name": name,
        "server": args.server,
        "variant": args.variant,
        "n": args.n,
        "rate_permille": args.rate,
        "warmup_ticks": args.warmup,
        "measure_ticks": args.measure,
        "cores": args.cores,
        "heap": args.heap if args.server == "neoforge" else None,
        "pumpkin_bin": str(Path(args.pumpkin_bin).expanduser()) if args.server == "pumpkin" else None,
        "pumpkin_bin_sha256": file_sha256(args.pumpkin_bin)[:16] if args.server == "pumpkin" else None,
        "inference_paused": not args.no_pause,
        "checksum_ok": ok,
        "energy_sum": got_sum,
        "energy_sum_expected": want_sum,
        "writes": got_writes,
        "tick_us": dict(zip(["count", "mean", "p50", "p95", "p99", "max"],
                            [int(probe.group(1))] + [float(probe.group(i)) for i in range(2, 7)])),
        "wall_s": t1 - t0,
        "cpu_s": cpu1 - cpu0,
        "cpu_us_per_tick": (cpu1 - cpu0) * 1e6 / args.measure,
        "rss_mib": rss,
    }
    (RESULTS / f"{name}.json").write_text(json.dumps(result, indent=2))
    return result


def retry_command(con, command, pattern, attempts, per_try=5):
    for _ in range(attempts):
        try:
            return con.command(command, pattern, timeout=per_try)
        except TimeoutError:
            continue
    raise TimeoutError(f"no {pattern!r} reply to {command!r} after {attempts} tries")


def wait_ticks(con, target, args):
    """Polls /psb status until the workload has completed `target` ticks and stopped."""
    deadline = time.monotonic() + args.timeout
    last_tick, last_change = -1, time.monotonic()
    while time.monotonic() < deadline:
        m = con.command("psb status", STATUS_RE)
        tick = int(m.group(1))
        if tick >= target:
            return m
        if tick != last_tick:
            last_tick, last_change = tick, time.monotonic()
        elif time.monotonic() - last_change > args.stall:
            raise RuntimeError(f"workload stuck at tick {tick} for {args.stall:.0f}s "
                               "(server paused? check pause-when-empty-seconds)")
        time.sleep(2)
    raise TimeoutError(f"workload did not reach tick {target}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", choices=["neoforge", "pumpkin"], required=True)
    p.add_argument("--variant", required=True,
                   help="neoforge: batched | block_entity. pumpkin: plugin name, e.g. wasm-batched")
    p.add_argument("--n", type=int, required=True)
    p.add_argument("--rate", type=int, default=10, help="writes per tick, per mille of n")
    p.add_argument("--warmup", type=int, default=1200, help="workload ticks before measuring")
    p.add_argument("--measure", type=int, default=2400, help="workload ticks measured")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--cores", default=DEFAULT_CORES)
    p.add_argument("--heap", default="6G")
    p.add_argument("--pumpkin-bin", default=str(PSB / "Pumpkin" / "target" / "release" / "pumpkin"),
                   help="Pumpkin server binary to run (recorded, with its hash, in the result)")
    p.add_argument("--settle", type=float, default=10.0, help="idle seconds after forceload and setup")
    p.add_argument("--timeout", type=float, default=3600.0)
    p.add_argument("--stall", type=float, default=60.0,
                   help="fail if the workload tick count does not advance for this many seconds")
    p.add_argument("--no-pause", action="store_true", help="leave the inference process running")
    p.add_argument("--keep-run-dir", action="store_true")
    p.add_argument("--pumpkin-ready", default=r"Server is now running",
                   help="regex for the Pumpkin log line that means the server is ready")
    args = p.parse_args()

    with Inference(enabled=not args.no_pause):
        for rep in range(1, args.repeats + 1):
            r = run_once(args, rep)
            t = r["tick_us"]
            print(f"{r['name']}: checksum_ok={r['checksum_ok']} mean={t['mean']:.0f}us "
                  f"p50={t['p50']:.0f}us p99={t['p99']:.0f}us cpu/tick={r['cpu_us_per_tick']:.0f}us "
                  f"rss={r['rss_mib']:.0f}MiB", flush=True)
            if not r["checksum_ok"]:
                sys.exit(f"checksum mismatch: got {r['energy_sum']} expected {r['energy_sum_expected']}")


if __name__ == "__main__":
    main()
