#!/usr/bin/env python3
"""Qwen build loop: the local model writes one Pumpkin variant until qwen/gate.sh passes.

Runs on the benchmark host. The model gets file tools (reads anywhere in the repo and the
Pumpkin source; writes only inside the task's allowed paths), a fast compile check, and the
gate. It can only finish after the gate has passed on the current code.

Ledger mode (--ledger on, the default; adapted from GVS5H's ledger-based self-orchestration for a
tool-using agent): Qwen keeps notes.md in the run folder (what is built, a map of its own code, API
facts, open problems, next step). It rewrites it at every milestone (first clean compile, unit tests
green, each new gate stage) and the conversation restarts from it, so contexts stay short.
--ledger off keeps the plain loop (handoff notes only).

Steering, all under the run's folder in qwen/runs/:
- Stuck (no progress for a while, too many reading turns in a row without writing or compiling,
  the same gate stage failing repeatedly, or the same edit retried): the first time, the loop
  handles it without a person and restarts the conversation with reading outside Qwen's own crate
  switched off until cargo_check or the gate reports an error. Ledger mode: Qwen updates notes.md
  and a tool-less manager call turns the notes and the measured compiler and gate output into one
  next step (MANAGER-<turn>.md). Plain mode: a handoff note and a generic order to write and
  compile (NUDGE-<turn>.md). Stuck again with no progress since, or the manager repeating a step:
  the loop writes ESCALATION.md with Qwen's own note and waits for hint.md.
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
import re
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
    def __init__(self, variant, allowed, gate=None, check_tests=False):
        self.variant = variant
        self.gate = gate or f"qwen/gate.sh {variant}"
        self.allowed = [(BENCH / a).resolve() for a in allowed]
        self.check_tests = check_tests  # cargo_check also runs the native unit tests
        self.gate_passed = False
        # Set by an automatic nudge; reads are then limited to the allowed (own) paths.
        self.reads_locked = False
        # The latest measured results, for the manager (ground truth, not what Qwen claims).
        self.last_check = ""
        self.last_gate = ""

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
        env = f". $HOME/.cargo/env && cd {BENCH}/pumpkin && "
        out = run_shell(env + f"taskset -c 10-14 cargo build --release -p psb-{self.variant} 2>&1 | tail -80", 600)
        if compiled(out) and self.check_tests:
            tests = run_shell(
                env + 'host="$(rustc -vV | sed -n "s/^host: //p")" && '
                f'taskset -c 10-14 cargo test --release -p psb-{self.variant} --target "$host" --lib 2>&1 '
                "| grep -E '^(test |test result|error|failures:|---- |thread .* panicked)' | tail -60", 900)
            out += f"\n=== unit tests (native)\n{tests}"
        if not compiled(out):
            self.reads_locked = False  # it may now look up what the error is about
        self.last_check = out
        return out

    def run_gate(self):
        out = run_shell(f"cd {BENCH} && bash {self.gate}", 1800)
        self.gate_passed = "GATE PASSED" in out
        if not self.gate_passed:
            self.reads_locked = False
        self.last_gate = out
        return out


def compiled(cargo_output):
    return "could not compile" not in cargo_output and "error[" not in cargo_output


TEST_RESULT = re.compile(r"test result: (ok|FAILED)\. (\d+) passed; (\d+) failed")


def tests_green(cargo_output):
    """True when cargo_check ran unit tests, at least one ran, and none failed."""
    results = TEST_RESULT.findall(cargo_output)
    return bool(results) and all(r[0] == "ok" for r in results) and sum(int(r[1]) for r in results) > 0


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

# --- ledger mode: a notes file that survives fresh conversations, and a manager for stalls ---

NOTES_PROMPT = """Pause and rewrite the run's notes file. It is the only memory that survives: the next
conversation starts fresh from the task and this file. The current file:

{notes}

Rewrite the COMPLETE file (it replaces the old one), under 800 words, as '- ' bullets under exactly
these headings:
## Built
## Code map
(every file in your crate with its public functions, types and constants: exact names and signatures)
## API facts learned
(exact paths and signatures that compiled, and mistakes not to repeat)
## Open problems
(the exact current compiler errors or failing gate checks, if any)
## Next step
Keep what still matters; delete what is superseded or disproven. Reply with the file only. Do not call
a tool."""

MILESTONE_PROMPT = """Milestone reached: {milestone}. This is a fresh conversation; the notes above are
what you know. Continue with the next step in the notes, and keep going until run_gate passes."""

MANAGER_SYSTEM = """You manage a coding agent that has stalled. You do not write code. Read the task,
the agent's notes and the measured facts (compiler and gate output: ground truth, unlike the notes),
then give the agent the ONE next step most likely to make measurable progress. Be concrete: name the
file, the function and the change, or the exact command to run. If the facts or the earlier steps show
the same fix failing repeatedly, choose a different approach rather than repeating it. Reply with
exactly:
### NEXT
<one step, at most 5 sentences>
### WHY
<one sentence>"""

MANAGER_STEP_PROMPT = """The loop stopped you because {reason}. This is a fresh conversation; the notes
above are what you know. Your manager read the notes and the latest compiler and gate output and
gives you this next step:

{step}

Do that step now. Reading outside your own crate is switched off until cargo_check or run_gate reports
an error; then read only what that error is about."""


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
        # Milestones (first clean compile, unit tests green, each new gate stage) are reported once
        # each in `milestone` for the loop to pick up; ledger mode starts a fresh conversation there.
        self.reached = set()
        self.milestone = None
        # Counts every real progress event (never reset), so the loop can tell whether anything
        # happened since a nudge, even within the turn the nudge was given in.
        self.gains = 0
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
            self.progressed(turn)
            if name == "edit_file":
                key = (params.get("path"), params.get("old_text"), params.get("new_text"))
                self.edits[key] = self.edits.get(key, 0) + 1
        elif name == "cargo_check":
            ok = compiled(result)
            if ok and self.check_ok is False:
                self.progressed(turn)
            self.check_ok = ok
            if ok:
                self.reach(turn, "compiles", "the crate compiles")
            if tests_green(result):
                self.reach(turn, "tests", "all unit tests pass")
        elif name == "run_gate":
            if "GATE PASSED" in result:
                self.gate_fails = []
                self.progressed(turn)
                return
            stage = next((st for st in GATE_STAGES if f"GATE FAILED at stage: {st}" in result), "setup")
            if GATE_STAGES[stage] > self.best_gate:
                self.best_gate = GATE_STAGES[stage]
                self.progressed(turn)
                if GATE_STAGES[stage] >= GATE_STAGES["lint"]:
                    self.reach(turn, f"gate:{stage}", f"the gate now gets as far as its '{stage}' stage")
            self.gate_fails.append(stage)

    def progressed(self, turn):
        self.last_progress = turn
        self.gains += 1

    def reach(self, turn, key, text):
        if key not in self.reached:
            self.reached.add(key)
            self.milestone = text
            self.progressed(turn)

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


def reply_without_tools(a, model, messages, max_tokens=2048):
    """One plain-text reply (no tool call), with retries. None if the model never answered."""
    for _ in range(3):
        try:
            msg = ask(a.model_base, model, messages, False, tool_choice="none", max_tokens=max_tokens)
            return (msg.get("content") or "").strip() or None
        except Exception as e:
            print(f"plain reply failed: {e}; retrying", flush=True)
            time.sleep(15)
    return None


def handoff_note(a, model, transcript):
    msgs = transcript.messages + [{"role": "user", "content": HANDOFF_PROMPT}]
    return reply_without_tools(a, model, msgs) or "(the model did not produce a handoff note)"


def update_notes(a, model, transcript, notes_path):
    """Ledger mode: Qwen rewrites notes.md from its current conversation. Keeps the old file if the
    model gives no answer."""
    old = notes_path.read_text() if notes_path.exists() else "(empty: this is the first version)"
    msgs = transcript.messages + [{"role": "user", "content": NOTES_PROMPT.format(notes=old)}]
    new = reply_without_tools(a, model, msgs, max_tokens=3072)
    if new:
        notes_path.write_text(new + "\n")
        return new
    return old


def manager_step(a, model, task, notes, facts, earlier_steps):
    """Ledger mode: a fresh, tool-less call that turns the notes and the measured facts into one
    next step. Returns the step text, or None if the model gave no usable answer."""
    earlier = "\n".join(f"- {s}" for s in earlier_steps) or "(none)"
    msgs = [{"role": "system", "content": MANAGER_SYSTEM},
            {"role": "user", "content": f"TASK:\n{task}\n\nAGENT'S NOTES:\n{notes}\n\nMEASURED FACTS:\n{facts}\n\n"
                                        f"STEPS YOU GAVE EARLIER IN THIS RUN:\n{earlier}"}]
    reply = reply_without_tools(a, model, msgs, max_tokens=1024)
    if not reply:
        return None
    m = re.search(r"#+\s*NEXT\s*\n(.*?)(?:\n#+\s*WHY\b|\Z)", reply, re.S | re.I)
    step = (m.group(1) if m else reply).strip()
    return step or None


def same_step(a, b):
    """Whether two manager steps say the same thing (ignoring case, spacing and punctuation)."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()  # noqa: E731
    return norm(a) == norm(b)


def measured_facts(reason, ws, progress, files):
    stage = next((s for s, i in GATE_STAGES.items() if i == progress.best_gate), "never run")
    return (f"Why the agent was stopped: {reason}\n"
            f"Furthest gate stage reached so far: {stage}\n"
            f"Files in the crate:\n{files}\n\n"
            f"Latest cargo_check output (tail):\n{ws.last_check[-3000:] or '(not run yet)'}\n\n"
            f"Latest run_gate output (tail):\n{ws.last_gate[-4000:] or '(not run yet)'}")


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
    ap.add_argument("--ledger", default="on", choices=["on", "off"],
                    help="on: notes.md rewritten at milestones, fresh conversation at each milestone, a "
                         "manager call on stalls; off: handoff notes and the generic nudge only")
    ap.add_argument("--check-tests", action="store_true",
                    help="cargo_check also runs the crate's native unit tests")
    ap.add_argument("--ledger-host", default=LOCAL.get("ledger_host"),
                    help="host holding a token-usage log to append one record per run to (off if unset)")
    ap.add_argument("--ledger-path", default=LOCAL.get("ledger_path", "~/token_log.jsonl"))
    ap.add_argument("--no-ledger", action="store_true", help="do not log usage even if a ledger host is set")
    return ap.parse_args()


def run(a):
    allowed = [f"pumpkin/{a.variant}"]
    ws = Workspace(a.variant, allowed, a.gate, a.check_tests)
    scaffold(a.variant, a.template)
    model = a.model or http_json(a.model_base.rstrip("/") + "/models")["data"][0]["id"]
    run_dir = BENCH / "qwen" / "runs" / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{a.variant}"
    run_dir.mkdir(parents=True)
    USAGE.run_dir = run_dir
    log = open(run_dir / "turns.jsonl", "w")
    task_text = (BENCH / a.task).read_text()
    intro = f"{task_text}\n\nAllowed write paths: {', '.join(allowed)}.\n"
    ledger = a.ledger == "on"
    notes_path = run_dir / "notes.md"
    if ledger and a.seed_note:
        notes_path.write_text(Path(a.seed_note).read_text())
    turn = 0

    def fresh(note=None):
        if note:
            label = "Your notes file (notes.md), kept up to date by you:" if ledger else "The handoff note:"
            first = (f"{intro}\nYou are continuing work that was already started. {label}\n\n"
                     f"{note}\n\nFiles that exist now:\n{current_files(a.variant)}\n"
                     "Re-read any file before you change it.")
        else:
            first = intro + f"Start by reading {a.first_read}."
        return Transcript(SYSTEM, first)

    def note_for_handoff():
        return update_notes(a, model, tr, notes_path) if ledger else handoff_note(a, model, tr)

    def restart(note, message, turn_now):
        """Fresh conversation from `note`, keeping what Progress knows about written code."""
        nonlocal tr, segment_start
        wrote = progress.wrote_anything
        tr = fresh(note)
        if message:
            tr.add_user(message)
        progress.reset(turn_now)
        progress.wrote_anything = wrote
        segment_start = turn_now

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
    nudge_turn = None     # turn of the last automatic nudge or manager step
    nudge_gains = 0       # progress.gains at that nudge
    manager_steps = []    # every step the manager gave in this run
    print(f"run {run_dir.name}: model {model}, ledger {a.ledger}", flush=True)
    status("running")

    while True:
        turn += 1
        out_of_budget = turn > a.turns or time.monotonic() > deadline
        if out_of_budget or turn - segment_start >= a.segment_turns:
            note = note_for_handoff()
            name = "HANDOFF-final.md" if out_of_budget else f"HANDOFF-{turn}.md"
            (run_dir / name).write_text(note + "\n")
            if out_of_budget:
                print(f"HANDOFF: out of budget at turn {turn}; note in {name}", flush=True)
                status("out-of-budget", note=name)
                return
            print(f"HANDOFF: fresh conversation at turn {turn}; note in {name}", flush=True)
            restart(note, None, turn)

        hint = take_hint(run_dir)
        reason = None if hint else progress.stuck(turn)
        if reason and (nudge_turn is None or progress.gains > nudge_gains):
            # First try without a person. Ledger mode: Qwen updates its notes and a tool-less manager
            # call turns notes + measured facts into one next step. Otherwise: the generic nudge.
            # Either way the conversation restarts and reading outside the crate is switched off.
            if ledger:
                note = update_notes(a, model, tr, notes_path)
                step = manager_step(a, model, task_text, note,
                                    measured_facts(reason, ws, progress, current_files(a.variant)),
                                    manager_steps)
                if step is None:
                    reason += "; the manager gave no usable step"
                elif any(same_step(step, s) for s in manager_steps):
                    reason += f"; the manager repeated an earlier step: {step[:200]}"
                else:
                    manager_steps.append(step)
                    (run_dir / f"MANAGER-{turn}.md").write_text(f"# Stalled: {reason}\n\n## Step\n\n{step}\n")
                    print(f"MANAGER: {reason} (turn {turn}); next step: {step[:120]}", flush=True)
                    restart(note, MANAGER_STEP_PROMPT.format(reason=reason, step=step), turn)
                    nudge_turn, nudge_gains, reason = turn, progress.gains, None
            else:
                note = handoff_note(a, model, tr)
                (run_dir / f"NUDGE-{turn}.md").write_text(f"# Automatic nudge: {reason}\n\n{note}\n")
                print(f"AUTO-NUDGE: {reason} (turn {turn}); fresh conversation, reading locked", flush=True)
                restart(note, NUDGE_PROMPT.format(reason=reason), turn)
                nudge_turn, nudge_gains, reason = turn, progress.gains, None
            if reason is None:
                ws.reads_locked = True
                status("running", nudged_at=turn)
        if reason:
            if nudge_turn is not None and "manager" not in reason:
                reason += f" (after an automatic nudge at turn {nudge_turn})"
            note = note_for_handoff()
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
        milestone, progress.milestone = progress.milestone, None
        if ledger and milestone and not ws.gate_passed:
            # Measurable progress: write it into the notes and continue in a short, fresh context.
            note = update_notes(a, model, tr, notes_path)
            (run_dir / f"MILESTONE-{turn}.md").write_text(f"# {milestone}\n\n{note}\n")
            print(f"MILESTONE: {milestone} (turn {turn}); notes rewritten, fresh conversation", flush=True)
            restart(note, MILESTONE_PROMPT.format(milestone=milestone), turn)
        status("running", context_chars=sum(len(str(m.get("content", ""))) for m in tr.messages))


if __name__ == "__main__":
    main()
