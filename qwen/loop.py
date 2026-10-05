#!/usr/bin/env python3
"""Qwen build loop: the local model writes one Pumpkin variant until qwen/gate.sh passes.

Runs on the benchmark host. The model gets file tools (reads anywhere in the repo and the
Pumpkin source; writes only inside the task's allowed paths), a fast compile check, and the
gate. It can only finish after the gate has passed on the current code.

Steering, all under the run's folder in qwen/runs/:
- Stuck (no progress for a while, too many reading turns in a row without writing or compiling,
  the same gate stage failing repeatedly, or the same edit retried): the first time, the loop
  nudges Qwen itself. It asks for a handoff note, restarts the conversation from it (NUDGE-<turn>.md)
  with an order to write and compile, and switches off reading outside Qwen's own crate until
  cargo_check or the gate reports an error. Stuck again with no progress since the nudge: the loop
  writes ESCALATION.md with Qwen's own status note and waits for hint.md.
- hint.md can also be written at any time; it is passed to Qwen at the start of the next turn.
  `stop` ends the run. The dashboard writes it for you.
- Every --segment-turns turns, and when the turn or time budget runs out, Qwen writes a
  HANDOFF note; the loop then continues in a fresh conversation seeded with it (or exits at the cap).
- Older tool results are trimmed so the conversation stays small.

  python3 qwen/loop.py --task qwen/tasks/wasm-batched.md --variant wasm-batched
  python3 qwen/loop.py ... --seed-note qwen/runs/<run>/HANDOFF-final.md   # resume a finished run
"""

import argparse
import datetime as dt
import json
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
BENCH = HOME / "psb" / "bench"
PUMPKIN = HOME / "psb" / "Pumpkin"
READ_ROOTS = [BENCH, PUMPKIN]
SKIP_DIRS = {"target", ".gradle", "build", ".git", "run"}
OUTPUT_MAX = 12_000

# Host-specific defaults (model endpoint, usage ledger) live outside the repo, in
# ~/psb/qwen-loop.json, e.g. {"model_base": "http://host:8000/v1", "ledger_host": "host",
# "ledger_path": "~/token_log.jsonl"}. Command-line flags override them.
try:
    LOCAL = json.loads((HOME / "psb" / "qwen-loop.json").read_text())
except (OSError, ValueError):
    LOCAL = {}

SYSTEM = """You are a careful Rust engineer working alone in a benchmark repository.
Your task description follows. Work in small steps: read the spec and the API you need,
write the code, run cargo_check until it compiles, then run_gate. Read compiler and gate
output closely and fix the actual cause. Do not guess API names: search the Pumpkin source.
You may only write the files the task allows. When run_gate prints GATE PASSED, call finish.
Every reply must be exactly one tool call."""


def tool(name, description, **params):
    required = [k for k, v in params.items() if not v.pop("optional", False)]
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": params, "required": required}}}


TOOLS = [
    tool("list_dir", "List a directory (relative to the repo root, or ../Pumpkin/... for the server source).",
         path={"type": "string"}),
    tool("read_file", "Read a text file. Large files: pass start_line and max_lines.",
         path={"type": "string"},
         start_line={"type": "integer", "optional": True},
         max_lines={"type": "integer", "optional": True}),
    tool("search", "grep -rn (extended regex) under a directory; returns matching lines.",
         pattern={"type": "string"}, path={"type": "string"}),
    tool("write_file", "Create or replace a whole file (allowed paths only).",
         path={"type": "string"}, content={"type": "string"}),
    tool("edit_file", "Replace one exact, unique occurrence of old_text with new_text (allowed paths only).",
         path={"type": "string"}, old_text={"type": "string"}, new_text={"type": "string"}),
    tool("cargo_check", "Build the variant crate for wasm32-wasip2 and return compiler output. Fast."),
    tool("run_gate", "Run the task's gate script (see 'Done means' in the task): build, clippy -D warnings, "
                     "and the task's checks on a real Pumpkin server. Slow (a few minutes)."),
    tool("finish", "End the task. Only accepted after run_gate has passed on the current code.",
         summary={"type": "string"}),
]


READ_TOOLS = ("list_dir", "read_file", "search")
READS_LOCKED = ("reading outside your own crate is switched off until cargo_check or run_gate reports "
                "an error. Write the code from what you already know, then run cargo_check.")


class Workspace:
    def __init__(self, variant, allowed, gate=None):
        self.variant = variant
        self.gate = gate or f"qwen/gate.sh {variant}"
        self.allowed = [(BENCH / a).resolve() for a in allowed]
        self.gate_passed = False
        # Set by an automatic nudge; reads are then limited to the allowed (own) paths.
        self.reads_locked = False

    def resolve(self, path, reading=False):
        p = (BENCH / path).resolve()
        if not any(p == r or r in p.parents for r in READ_ROOTS):
            raise ValueError(f"{path} is outside the repo and the Pumpkin source")
        if reading and self.reads_locked and not self.writable(p):
            raise PermissionError(READS_LOCKED)
        return p

    def writable(self, p):
        return any(p == a or a in p.parents for a in self.allowed)

    def list_dir(self, path):
        p = self.resolve(path, reading=True)
        rows = [f"{c.name}/" if c.is_dir() else c.name
                for c in sorted(p.iterdir()) if c.name not in SKIP_DIRS]
        return "\n".join(rows) or "(empty)"

    def read_file(self, path, start_line=1, max_lines=400):
        lines = self.resolve(path, reading=True).read_text(errors="replace").splitlines()
        start = max(1, int(start_line or 1))
        chunk = lines[start - 1:start - 1 + int(max_lines or 400)]
        body = "\n".join(f"{start + i:5}  {line}" for i, line in enumerate(chunk))
        return f"{path} ({len(lines)} lines)\n{body}"

    def search(self, pattern, path):
        p = self.resolve(path, reading=True)
        cmd = ["grep", "-rnE", "--exclude-dir=target", "--exclude-dir=.git", pattern, str(p)]
        out = subprocess.run(cmd, capture_output=True, text=True).stdout
        return out.replace(str(BENCH) + "/", "") or "(no matches)"

    def write_file(self, path, content):
        p = self.resolve(path)
        if not self.writable(p):
            raise PermissionError(f"{path} is not in the task's allowed paths")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        self.gate_passed = False
        return f"wrote {path} ({len(content)} bytes)"

    def edit_file(self, path, old_text, new_text):
        p = self.resolve(path)
        if not self.writable(p):
            raise PermissionError(f"{path} is not in the task's allowed paths")
        text = p.read_text()
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text occurs {count} times in {path}; it must occur exactly once")
        p.write_text(text.replace(old_text, new_text))
        self.gate_passed = False
        return f"edited {path}"

    def cargo_check(self):
        cmd = f". $HOME/.cargo/env && cd {BENCH}/pumpkin && taskset -c 10-14 cargo build --release -p psb-{self.variant} 2>&1 | tail -80"
        out = run_shell(cmd, 600)
        if not compiled(out):
            self.reads_locked = False  # it may now look up what the error is about
        return out

    def run_gate(self):
        out = run_shell(f"cd {BENCH} && bash {self.gate}", 1800)
        self.gate_passed = "GATE PASSED" in out
        if not self.gate_passed:
            self.reads_locked = False
        return out


def compiled(cargo_output):
    return "could not compile" not in cargo_output and "error[" not in cargo_output


def scaffold(variant, template=None):
    """Creates the variant's crate if none exists, so the workspace glob resolves: a copy of
    `template` (a directory in the repo, `VARIANT` in its Cargo.toml replaced) or an empty crate."""
    crate = BENCH / "pumpkin" / variant
    if (crate / "Cargo.toml").exists():
        return
    if template:
        shutil.copytree(BENCH / template, crate, dirs_exist_ok=True)
        manifest = crate / "Cargo.toml"
        manifest.write_text(manifest.read_text().replace("VARIANT", variant))
        return
    (crate / "src").mkdir(parents=True, exist_ok=True)
    (crate / "Cargo.toml").write_text(
        f'[package]\nname = "psb-{variant}"\nversion = "0.1.0"\nedition = "2024"\nlicense = "MIT"\n\n'
        '[lib]\ncrate-type = ["cdylib"]\n\n'
        '[dependencies]\npumpkin-plugin-api = { workspace = true }\npsb-workload = { workspace = true }\n')
    (crate / "src" / "lib.rs").write_text("// Write the plugin here.\n")


def run_shell(cmd, timeout):
    try:
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr)[-OUTPUT_MAX:]
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout}s"


def http_json(url, body=None, timeout=900):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


GATE_STAGES = {"setup": 0, "build": 1, "lint": 2, "test": 3, "smoke": 4}
KEEP_FULL_RESULTS = 40
STUB_LEN = 300

HANDOFF_PROMPT = """Stop working for a moment and write a handoff note for whoever continues this task
(it may be you with a fresh memory). Plain text, under 400 words:
1. What is done and working.
2. What is failing right now, with the exact error or gate stage.
3. What you have already tried that did not work.
4. The single next step you would take.
Reply with the note only. Do not call a tool."""

NUDGE_PROMPT = """Automatic check by the loop: {reason}. You have been reading instead of building.
The handoff note above is what you already know, and it is enough to start. Reading the API and the
server source is now switched off; you can still read the files in your own crate.

Your next tool calls: write the files the task needs (complete code, your best guess where unsure),
then cargo_check. The compiler is the fastest API reference: when cargo_check or run_gate reports an
error, reading comes back, and then read only what that error is about."""


class Progress:
    """Decides when the loop is stuck. Progress means: a file actually changed, a compile check went
    from failing to passing, or the gate got further than before. Separately, too many reading turns
    in a row (list_dir, read_file, search) without writing or compiling counts as stuck early."""

    def __init__(self, explore_turns, stall_turns, gate_repeats, read_budget=40, read_streak=15, own=()):
        self.explore_turns = explore_turns
        self.stall_turns = stall_turns
        self.gate_repeats = gate_repeats
        self.read_budget = read_budget  # reading turns in a row allowed before anything is written
        self.read_streak = read_streak  # the same, once code exists
        # Reading Qwen's own crate (repo-relative prefixes) is part of writing it, so it does not
        # count toward the reading streak; the stall limits still catch endless re-reading.
        self.own = tuple(p.rstrip("/") + "/" for p in own)
        self.best_gate = -1
        self.reset(0)

    def reset(self, turn):
        self.last_progress = turn
        self.wrote_anything = False
        self.check_ok = None
        self.gate_fails = []  # stages of consecutive failed gates
        self.edits = {}       # (path, old, new) -> times attempted
        self.reads = 0        # reading turns since the last write, compile or gate

    def note(self, turn, name, params, result):
        if name in READ_TOOLS:
            path = str(params.get("path", "")).lstrip("./") + "/"
            if not path.startswith(self.own):
                self.reads += 1
        elif name in ("write_file", "edit_file", "cargo_check", "run_gate"):
            self.reads = 0
        if name in ("write_file", "edit_file") and not result.startswith("error"):
            self.wrote_anything = True
            self.last_progress = turn
            if name == "edit_file":
                key = (params.get("path"), params.get("old_text"), params.get("new_text"))
                self.edits[key] = self.edits.get(key, 0) + 1
        elif name == "cargo_check":
            ok = compiled(result)
            if ok and self.check_ok is False:
                self.last_progress = turn
            self.check_ok = ok
        elif name == "run_gate":
            if "GATE PASSED" in result:
                self.gate_fails = []
                self.last_progress = turn
                return
            stage = next((st for st in GATE_STAGES if f"GATE FAILED at stage: {st}" in result), "setup")
            if GATE_STAGES[stage] > self.best_gate:
                self.best_gate = GATE_STAGES[stage]
                self.last_progress = turn
            self.gate_fails.append(stage)

    def stuck(self, turn):
        """Returns a reason when the loop should stop and ask for help, else None."""
        limit = self.stall_turns if self.wrote_anything else self.explore_turns
        if turn - self.last_progress >= limit:
            what = "edited no file" if not self.wrote_anything else "made no progress"
            return f"{turn - self.last_progress} turns since the last progress ({what})"
        tail = self.gate_fails[-self.gate_repeats:]
        if len(tail) == self.gate_repeats and len(set(tail)) == 1:
            return f"the gate failed at stage '{tail[0]}' {self.gate_repeats} times in a row"
        for key, count in self.edits.items():
            if count >= 3:
                return f"the same edit to {key[0]} was attempted {count} times"
        limit = self.read_streak if self.wrote_anything else self.read_budget
        if self.reads >= limit:
            return f"{self.reads} reading turns in a row without writing or compiling"
        return None


class Transcript:
    """The conversation sent to the model, kept small: a file's earlier reads are replaced by a stub
    once it is re-read or rewritten, and tool results older than the last few are cut short."""

    def __init__(self, system, first_user):
        self.messages = [{"role": "system", "content": system}, {"role": "user", "content": first_user}]
        self.meta = {}  # index of a tool message -> (tool name, path)

    def add_assistant(self, msg, calls):
        self.messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})

    def add_user(self, text):
        self.messages.append({"role": "user", "content": text})

    def add_tool(self, call_id, name, params, result):
        path = params.get("path")
        if path and name in ("read_file", "write_file", "edit_file"):
            for i, (n, p) in self.meta.items():
                if p == path and n == "read_file" and not self.messages[i]["content"].startswith("["):
                    self.messages[i]["content"] = f"[older read of {path} removed; it was re-read or changed]"
        self.messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
        self.meta[len(self.messages) - 1] = (name, path)
        tool_idx = [i for i, m in enumerate(self.messages) if m["role"] == "tool"]
        for i in tool_idx[:-KEEP_FULL_RESULTS]:
            c = self.messages[i]["content"]
            if len(c) > STUB_LEN and not c.startswith("["):
                self.messages[i]["content"] = c[:STUB_LEN] + " [... older result trimmed]"


class Usage:
    """Token usage of every model call in this run, from each response's own `usage` block.
    rust_infer reports prompt and completion tokens but no prefix-cache split, so the ledger
    record marks the input as unmeasured, like the velo_eval rows already in the log."""

    def __init__(self):
        self.calls = 0
        self.prompt = 0
        self.completion = 0
        self.state = "starting"
        self.run_dir = None

    def add(self, usage):
        self.calls += 1
        self.prompt += int(usage.get("prompt_tokens") or 0)
        self.completion += int(usage.get("completion_tokens") or 0)
        if self.run_dir:
            (self.run_dir / "usage.json").write_text(json.dumps(
                {"calls": self.calls, "prompt_tokens": self.prompt,
                 "completion_tokens": self.completion}, indent=2))


USAGE = Usage()


def append_to_ledger(a):
    """Appends this run's totals to the spark-savings token log (one record per run).
    Best-effort: on failure the record is kept in the run folder as ledger-unsent.json."""
    if a.no_ledger or not a.ledger_host or USAGE.calls == 0:
        return
    rec = {
        "ts": time.time(), "label": f"psb-loop:{a.variant}", "turns": USAGE.calls,
        "input": 0, "cache_read": 0, "cache_creation": 0, "output": USAGE.completion,
        "concurrent": 1, "source": "psb_loop", "split_unmeasured": True,
        "prompt_tokens_unsplit": USAGE.prompt, "status": USAGE.state,
        "run": USAGE.run_dir.name if USAGE.run_dir else None,
        "note": "pumpkin-server-bench qwen/loop.py; per-response usage from rust_infer "
                "(no cache split); completion tokens include reasoning",
    }
    line = json.dumps(rec) + "\n"
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", a.ledger_host,
                            f"cat >> {a.ledger_path}"], input=line, text=True,
                           capture_output=True, timeout=30)
        ok = r.returncode == 0
    except Exception:
        ok = False
    if ok:
        print(f"ledger: logged {USAGE.calls} calls, {USAGE.prompt} prompt + {USAGE.completion} "
              f"completion tokens to {a.ledger_host}:{a.ledger_path}", flush=True)
    else:
        if USAGE.run_dir:
            (USAGE.run_dir / "ledger-unsent.json").write_text(line)
        print("ledger: could not append the usage record; kept as ledger-unsent.json", flush=True)


def ask(base, model, messages, thinking, tool_choice="required", max_tokens=16384):
    body = {"model": model, "messages": messages, "tools": TOOLS, "tool_choice": tool_choice,
            "max_tokens": max_tokens, "chat_template_kwargs": {"enable_thinking": thinking}}
    resp = http_json(base.rstrip("/") + "/chat/completions", body)
    USAGE.add(resp.get("usage") or {})
    return resp["choices"][0]["message"]


def handoff_note(a, model, transcript):
    msgs = transcript.messages + [{"role": "user", "content": HANDOFF_PROMPT}]
    for _ in range(3):
        try:
            note = ask(a.model_base, model, msgs, False, tool_choice="none", max_tokens=2048)
            return (note.get("content") or "").strip() or "(empty handoff note)"
        except Exception as e:
            print(f"handoff note failed: {e}; retrying", flush=True)
            time.sleep(15)
    return "(the model did not produce a handoff note)"


def current_files(variant):
    crate = BENCH / "pumpkin" / variant
    return "\n".join(str(f.relative_to(BENCH)) for f in sorted(crate.rglob("*"))
                     if f.is_file() and "target" not in f.parts)


STOP = object()


def take_hint(run_dir):
    """Consumes run_dir/hint.md if someone wrote one. Returns its text, STOP, or None if there is none."""
    hint = run_dir / "hint.md"
    if not hint.exists():
        return None
    time.sleep(1)  # let the writer finish
    text = hint.read_text().strip()
    hint.rename(run_dir / f"hint-{dt.datetime.now():%H%M%S}.md")
    return STOP if text.lower() in ("stop", "abort") else text


def wait_for_hint(run_dir, poll=10):
    """Blocks until someone writes run_dir/hint.md."""
    while (hint := take_hint(run_dir)) is None:
        time.sleep(poll)
    return hint


def main():
    a = parse_args()
    # tmux kill-session sends SIGHUP: turn it (and SIGTERM) into a normal exit so usage is logged.
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(1))
    try:
        run(a)
    except BaseException:
        if USAGE.state in ("running", "starting", "waiting-for-hint"):
            USAGE.state = "interrupted"
        raise
    finally:
        append_to_ledger(a)


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--variant", required=True)
    ap.add_argument("--gate", default=None,
                    help="gate command relative to the repo (default: qwen/gate.sh <variant>)")
    ap.add_argument("--first-read", default="SPEC.md and pumpkin/probe/src/lib.rs",
                    help="what the first message tells Qwen to read before starting")
    ap.add_argument("--template", default=None,
                    help="directory to start a new crate from (e.g. qwen/templates/plugin)")
    ap.add_argument("--model-base", default=LOCAL.get("model_base", "http://localhost:8000/v1"))
    ap.add_argument("--model", default=None)
    ap.add_argument("--thinking", default="on", choices=["on", "off"])
    ap.add_argument("--turns", type=int, default=400, help="hard cap on turns for the whole run")
    ap.add_argument("--minutes", type=float, default=120, help="hard cap on wall time")
    ap.add_argument("--segment-turns", type=int, default=150,
                    help="turns per conversation before handing off to a fresh one")
    ap.add_argument("--explore-turns", type=int, default=90, help="stall limit before the first edit")
    ap.add_argument("--stall-turns", type=int, default=20, help="stall limit once code exists")
    ap.add_argument("--read-budget", type=int, default=40,
                    help="reading calls in a row allowed before anything is written")
    ap.add_argument("--read-streak", type=int, default=15,
                    help="reading calls in a row allowed once code exists")
    ap.add_argument("--gate-repeats", type=int, default=3,
                    help="the same failed gate stage this many times in a row counts as stuck")
    ap.add_argument("--seed-note", default=None, help="a handoff note to start from instead of scratch")
    ap.add_argument("--ledger-host", default=LOCAL.get("ledger_host"),
                    help="host holding a token-usage log to append one record per run to (off if unset)")
    ap.add_argument("--ledger-path", default=LOCAL.get("ledger_path", "~/token_log.jsonl"))
    ap.add_argument("--no-ledger", action="store_true", help="do not log usage even if a ledger host is set")
    return ap.parse_args()


def run(a):
    allowed = [f"pumpkin/{a.variant}"]
    ws = Workspace(a.variant, allowed, a.gate)
    scaffold(a.variant, a.template)
    model = a.model or http_json(a.model_base.rstrip("/") + "/models")["data"][0]["id"]
    run_dir = BENCH / "qwen" / "runs" / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{a.variant}"
    run_dir.mkdir(parents=True)
    USAGE.run_dir = run_dir
    log = open(run_dir / "turns.jsonl", "w")
    intro = f"{(BENCH / a.task).read_text()}\n\nAllowed write paths: {', '.join(allowed)}.\n"
    turn = 0

    def fresh(note=None):
        if note:
            first = (f"{intro}\nYou are continuing work that was already started. The handoff note:\n\n"
                     f"{note}\n\nFiles that exist now:\n{current_files(a.variant)}\n"
                     "Re-read any file before you change it.")
        else:
            first = intro + f"Start by reading {a.first_read}."
        return Transcript(SYSTEM, first)

    def status(state, **extra):
        USAGE.state = state
        (run_dir / "status.json").write_text(json.dumps(
            {"state": state, "turn": turn, "at": dt.datetime.now().isoformat(timespec="seconds"), **extra},
            indent=2))

    tr = fresh(Path(a.seed_note).read_text() if a.seed_note else None)
    progress = Progress(a.explore_turns, a.stall_turns, a.gate_repeats, a.read_budget, a.read_streak,
                        own=allowed)
    deadline = time.monotonic() + a.minutes * 60
    segment_start = 1
    nudge_turn = None  # turn of the last automatic nudge
    print(f"run {run_dir.name}: model {model}", flush=True)
    status("running")

    while True:
        turn += 1
        out_of_budget = turn > a.turns or time.monotonic() > deadline
        if out_of_budget or turn - segment_start >= a.segment_turns:
            note = handoff_note(a, model, tr)
            name = "HANDOFF-final.md" if out_of_budget else f"HANDOFF-{turn}.md"
            (run_dir / name).write_text(note + "\n")
            if out_of_budget:
                print(f"HANDOFF: out of budget at turn {turn}; note in {name}", flush=True)
                status("out-of-budget", note=name)
                return
            print(f"HANDOFF: fresh conversation at turn {turn}; note in {name}", flush=True)
            tr = fresh(note)
            progress.reset(turn)
            segment_start = turn

        hint = take_hint(run_dir)
        reason = None if hint else progress.stuck(turn)
        if reason and (nudge_turn is None or progress.last_progress > nudge_turn):
            # First try: restart from Qwen's own note with an order to build, reading switched off.
            note = handoff_note(a, model, tr)
            (run_dir / f"NUDGE-{turn}.md").write_text(f"# Automatic nudge: {reason}\n\n{note}\n")
            print(f"AUTO-NUDGE: {reason} (turn {turn}); fresh conversation, reading locked", flush=True)
            wrote = progress.wrote_anything
            tr = fresh(note)
            tr.add_user(NUDGE_PROMPT.format(reason=reason))
            progress.reset(turn)
            progress.wrote_anything = wrote
            segment_start = turn
            nudge_turn = turn
            ws.reads_locked = True
            status("running", nudged_at=turn)
            reason = None
        if reason:
            reason += f" (after an automatic nudge at turn {nudge_turn})"
            note = handoff_note(a, model, tr)
            (run_dir / "ESCALATION.md").write_text(
                f"# Stuck: {reason}\n\nTurn {turn}. Write guidance to `{run_dir / 'hint.md'}` or use the "
                "dashboard. Write `stop` to end the run.\n\n## Qwen's note\n\n" + note + "\n")
            print(f"ESCALATION: {reason} (turn {turn}); waiting for a hint", flush=True)
            status("waiting-for-hint", reason=reason)
            hint = wait_for_hint(run_dir)
            (run_dir / "ESCALATION.md").rename(run_dir / f"ESCALATION-{turn}.md")
        if hint is STOP:
            print("stopped by hint", flush=True)
            status("stopped")
            return
        if hint:
            print(f"hint received at turn {turn}: {hint[:100]}", flush=True)
            tr.add_user(f"Guidance from the person supervising you:\n\n{hint}")
            ws.reads_locked = False  # the supervisor's guidance decides what to read
            progress.reset(turn)
            progress.wrote_anything = bool(current_files(a.variant))
            status("running")

        t0 = time.time()
        try:
            msg = ask(a.model_base, model, tr.messages, a.thinking == "on")
        except Exception as e:
            print(f"turn {turn}: model error {e}; retrying in 15 s", flush=True)
            turn -= 1
            time.sleep(15)
            continue
        calls = msg.get("tool_calls") or []
        tr.add_assistant(msg, calls)
        if not calls:
            tr.add_user("Reply with exactly one tool call.")
            continue

        for call in calls:
            name = call["function"]["name"]
            try:
                params = json.loads(call["function"].get("arguments") or "{}")
                result = None
            except json.JSONDecodeError as e:
                result, params = f"error: arguments are not valid JSON ({e})", {}
            if result is None:
                if name == "finish":
                    if ws.gate_passed:
                        log.write(json.dumps({"turn": turn, "tool": name, "args": params}) + "\n")
                        print(f"turn {turn}: finished: {params.get('summary', '')}", flush=True)
                        shutil.copytree(BENCH / "pumpkin" / a.variant, run_dir / "final",
                                        ignore=shutil.ignore_patterns("target"))
                        status("finished", summary=params.get("summary", ""))
                        return
                    result = "rejected: run_gate has not passed on the current code"
                else:
                    try:
                        result = getattr(ws, name)(**params)
                    except Exception as e:
                        result = f"error: {e}"
            result = str(result)[-OUTPUT_MAX:]
            tr.add_tool(call.get("id", ""), name, params, result)
            progress.note(turn, name, params, result)
            log.write(json.dumps({"turn": turn, "secs": round(time.time() - t0, 1), "tool": name,
                                  "args": {k: (v if len(str(v)) < 400 else str(v)[:400] + "...")
                                           for k, v in params.items()},
                                  "result": result[:2000],
                                  "reasoning": (msg.get("reasoning_content") or "")[:2000]}) + "\n")
            log.flush()
            print(f"turn {turn}: {name} -> {result.splitlines()[-1][:120] if result else ''}", flush=True)
            if name == "run_gate" and ws.gate_passed:
                shutil.copytree(BENCH / "pumpkin" / a.variant, run_dir / f"green-{turn}",
                                ignore=shutil.ignore_patterns("target"))
        status("running", context_chars=sum(len(str(m.get("content", ""))) for m in tr.messages))


if __name__ == "__main__":
    main()
