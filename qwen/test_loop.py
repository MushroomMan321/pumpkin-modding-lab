"""Offline tests for the loop's stall detection and transcript trimming. Run: python3 qwen/test_loop.py"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import loop  # noqa: E402

FAIL = "GATE FAILED at stage: {}"


class ProgressTest(unittest.TestCase):
    def p(self):
        return loop.Progress(explore_turns=90, stall_turns=20, gate_repeats=3)

    def test_reading_is_allowed_until_the_explore_limit(self):
        p = self.p()
        self.assertIsNone(p.stuck(89))
        self.assertIn("edited no file", p.stuck(90))

    def test_tighter_limit_once_code_exists(self):
        p = self.p()
        p.note(10, "write_file", {"path": "a"}, "wrote a")
        self.assertIsNone(p.stuck(29))
        self.assertIn("no progress", p.stuck(30))

    def test_failed_write_is_not_progress(self):
        p = self.p()
        p.note(10, "write_file", {"path": "x"}, "error: not allowed")
        self.assertFalse(p.wrote_anything)

    def test_compile_fixed_counts_as_progress(self):
        p = self.p()
        p.note(5, "write_file", {"path": "a"}, "wrote a")
        p.note(6, "cargo_check", {}, "error: could not compile `x`")
        p.note(20, "cargo_check", {}, "Finished `release` profile")
        self.assertEqual(p.last_progress, 20)

    def test_same_gate_stage_three_times_is_stuck(self):
        p = self.p()
        p.note(1, "write_file", {"path": "a"}, "wrote a")
        for t in (2, 3, 4):
            p.note(t, "run_gate", {}, FAIL.format("smoke"))
        self.assertIn("stage 'smoke' 3 times", p.stuck(5))

    def test_gate_getting_further_resets_the_streak(self):
        p = self.p()
        p.note(1, "write_file", {"path": "a"}, "wrote a")
        p.note(2, "run_gate", {}, FAIL.format("build"))
        p.note(3, "run_gate", {}, FAIL.format("build"))
        p.note(4, "run_gate", {}, FAIL.format("lint"))
        self.assertIsNone(p.stuck(5))
        self.assertEqual(p.last_progress, 4)

    def test_repeating_the_same_edit_is_stuck(self):
        p = self.p()
        edit = {"path": "a", "old_text": "x", "new_text": "y"}
        for t in (1, 2, 3):
            p.note(t, "edit_file", edit, "edited a")
        self.assertIn("same edit", p.stuck(4))

    def test_long_reading_streak_is_stuck_before_the_explore_limit(self):
        p = self.p()
        for t in range(1, 40):
            p.note(t, "read_file", {"path": "x"}, "contents")
        self.assertIsNone(p.stuck(39))
        p.note(40, "search", {"path": "x", "pattern": "y"}, "hits")
        self.assertIn("40 reading turns in a row", p.stuck(40))

    def test_writing_or_compiling_ends_the_reading_streak(self):
        p = self.p()
        for t in range(1, 30):
            p.note(t, "read_file", {"path": "x"}, "contents")
        p.note(30, "cargo_check", {}, "error: could not compile `x`")
        for t in range(31, 45):
            p.note(t, "read_file", {"path": "x"}, "contents")
        self.assertIsNone(p.stuck(45))

    def test_shorter_reading_streak_once_code_exists(self):
        p = self.p()
        p.note(1, "write_file", {"path": "a"}, "wrote a")
        for t in range(2, 17):
            p.note(t, "read_file", {"path": "x"}, "contents")
        self.assertIn("15 reading turns in a row", p.stuck(17))

    def test_reading_own_crate_does_not_count_toward_the_streak(self):
        p = loop.Progress(explore_turns=90, stall_turns=20, gate_repeats=3, own=["pumpkin/plugin-x"])
        p.note(1, "write_file", {"path": "pumpkin/plugin-x/src/lib.rs"}, "wrote it")
        for t in range(2, 19):
            p.note(t, "read_file", {"path": "pumpkin/plugin-x/src/plan.rs"}, "contents")
        p.note(19, "search", {"path": "pumpkin/plugin-x", "pattern": "pub fn"}, "hits")
        self.assertIsNone(p.stuck(19))
        p.note(20, "read_file", {"path": "../Pumpkin/crates/x.wit"}, "contents")
        self.assertEqual(p.reads, 1)
        self.assertIn("no progress", p.stuck(21))  # the stall limit still applies

    def test_gate_pass_clears_failures(self):
        p = self.p()
        p.note(1, "write_file", {"path": "a"}, "wrote a")
        p.note(2, "run_gate", {}, FAIL.format("smoke"))
        p.note(3, "run_gate", {}, FAIL.format("smoke"))
        p.note(4, "run_gate", {}, "GATE PASSED")
        p.note(5, "run_gate", {}, FAIL.format("smoke"))
        self.assertIsNone(p.stuck(6))


class MilestoneTest(unittest.TestCase):
    def p(self):
        return loop.Progress(explore_turns=90, stall_turns=20, gate_repeats=3)

    def test_first_clean_compile_is_a_milestone_once(self):
        p = self.p()
        p.note(1, "cargo_check", {}, "error[E0425]: x\nerror: could not compile")
        self.assertIsNone(p.milestone)
        p.note(2, "cargo_check", {}, "Finished `release` profile")
        self.assertEqual(p.milestone, "the crate compiles")
        p.milestone = None
        p.note(3, "cargo_check", {}, "Finished `release` profile")
        self.assertIsNone(p.milestone)

    def test_green_unit_tests_are_a_milestone(self):
        p = self.p()
        p.note(1, "cargo_check", {}, "Finished\n=== unit tests (native)\ntest result: FAILED. 7 passed; 1 failed;")
        p.milestone = None
        p.note(2, "cargo_check", {}, "Finished\n=== unit tests (native)\ntest result: ok. 8 passed; 0 failed;")
        self.assertEqual(p.milestone, "all unit tests pass")

    def test_each_new_gate_stage_from_lint_on_is_a_milestone(self):
        p = self.p()
        p.note(1, "run_gate", {}, FAIL.format("build"))
        self.assertIsNone(p.milestone)
        p.note(2, "run_gate", {}, FAIL.format("lint"))
        self.assertIn("'lint'", p.milestone)
        p.milestone = None
        p.note(3, "run_gate", {}, FAIL.format("lint"))
        self.assertIsNone(p.milestone)
        p.note(4, "run_gate", {}, FAIL.format("smoke"))
        self.assertIn("'smoke'", p.milestone)

    def test_milestones_survive_a_reset(self):
        p = self.p()
        p.note(1, "cargo_check", {}, "Finished")
        p.reset(5)
        p.milestone = None
        p.note(6, "cargo_check", {}, "Finished")
        self.assertIsNone(p.milestone)

    def test_tests_green_needs_at_least_one_test_and_no_failures(self):
        self.assertFalse(loop.tests_green("test result: ok. 0 passed; 0 failed;"))
        self.assertFalse(loop.tests_green("test result: ok. 3 passed; 0 failed;\ntest result: FAILED. 1 passed; 2 failed;"))
        self.assertTrue(loop.tests_green("test result: ok. 3 passed; 0 failed;\ntest result: ok. 0 passed; 0 failed;"))
        self.assertFalse(loop.tests_green("Finished `release` profile"))


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.real_ask = loop.ask
        self.args = type("A", (), {"model_base": "http://x"})()

    def tearDown(self):
        loop.ask = self.real_ask

    def test_step_is_the_next_section(self):
        loop.ask = lambda *a, **k: {"content": "### NEXT\nFix the import in src/lib.rs.\n### WHY\nIt fails."}
        step = loop.manager_step(self.args, "m", "task", "notes", "facts", [])
        self.assertEqual(step, "Fix the import in src/lib.rs.")

    def test_unstructured_reply_is_used_whole(self):
        loop.ask = lambda *a, **k: {"content": "Run cargo_check."}
        self.assertEqual(loop.manager_step(self.args, "m", "task", "notes", "facts", []), "Run cargo_check.")

    def test_same_step_ignores_case_and_punctuation(self):
        self.assertTrue(loop.same_step("Fix the import in src/lib.rs.", "fix the import in src lib rs"))
        self.assertFalse(loop.same_step("Fix the import.", "Rewrite plan.rs."))

    def test_notes_are_kept_when_the_model_gives_nothing(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            notes = tmp / "notes.md"
            notes.write_text("old notes\n")
            loop.ask = lambda *a, **k: {"content": ""}
            tr = loop.Transcript("sys", "task")
            self.assertEqual(loop.update_notes(self.args, "m", tr, notes), "old notes\n")
            self.assertEqual(notes.read_text(), "old notes\n")
        finally:
            shutil.rmtree(tmp)


class ReadLockTest(unittest.TestCase):
    """After an automatic nudge, reading is limited to the task's own crate until a compile or gate
    error, so Qwen has to write code instead of reading more of the API."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        (self.tmp / "pumpkin" / "plugin-x" / "src").mkdir(parents=True)
        (self.tmp / "pumpkin" / "plugin-x" / "src" / "lib.rs").write_text("// mine\n")
        (self.tmp / "api.wit").write_text("func\n")
        self.saved = (loop.BENCH, loop.READ_ROOTS, loop.run_shell)
        loop.BENCH, loop.READ_ROOTS = self.tmp, [self.tmp]
        self.ws = loop.Workspace("plugin-x", ["pumpkin/plugin-x"])
        self.ws.reads_locked = True

    def tearDown(self):
        loop.BENCH, loop.READ_ROOTS, loop.run_shell = self.saved
        shutil.rmtree(self.tmp)

    def test_own_crate_stays_readable(self):
        self.assertIn("// mine", self.ws.read_file("pumpkin/plugin-x/src/lib.rs"))

    def test_everything_else_is_blocked(self):
        for call in (lambda: self.ws.read_file("api.wit"), lambda: self.ws.search("func", "."),
                     lambda: self.ws.list_dir(".")):
            with self.assertRaisesRegex(PermissionError, "switched off"):
                call()

    def test_compile_error_unlocks_but_a_clean_build_does_not(self):
        loop.run_shell = lambda cmd, timeout: "Finished `release` profile"
        self.ws.cargo_check()
        self.assertTrue(self.ws.reads_locked)
        loop.run_shell = lambda cmd, timeout: "error[E0425]: cannot find value\nerror: could not compile"
        self.ws.cargo_check()
        self.assertFalse(self.ws.reads_locked)
        self.assertIn("func", self.ws.read_file("api.wit"))

    def test_failed_gate_unlocks(self):
        loop.run_shell = lambda cmd, timeout: "GATE FAILED at stage: smoke"
        self.ws.run_gate()
        self.assertFalse(self.ws.reads_locked)

    def test_check_tests_runs_unit_tests_only_after_a_clean_build(self):
        ws = loop.Workspace("plugin-x", ["pumpkin/plugin-x"], check_tests=True)
        cmds = []

        def shell(cmd, timeout):
            cmds.append(cmd)
            return "test result: ok. 9 passed; 0 failed;" if "cargo test" in cmd else self.build_out

        loop.run_shell = shell
        self.build_out = "error[E0425]: x\nerror: could not compile"
        self.assertNotIn("unit tests", ws.cargo_check())
        self.build_out = "Finished `release` profile"
        out = ws.cargo_check()
        self.assertIn("=== unit tests (native)\ntest result: ok. 9 passed", out)
        self.assertEqual(sum("cargo test" in c for c in cmds), 1)
        self.assertEqual(ws.last_check, out)


class ScriptedRunTest(unittest.TestCase):
    """Drives run() end to end with a scripted model and a fake shell, to check the wiring of
    milestones, the manager and escalation."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        (self.tmp / "task.md").write_text("Write the plugin.\n")
        (self.tmp / "api.wit").write_text("func\n")
        self.saved = (loop.BENCH, loop.READ_ROOTS, loop.run_shell, loop.ask, loop.wait_for_hint, sys.argv,
                      loop.USAGE)
        loop.BENCH, loop.READ_ROOTS = self.tmp, [self.tmp]
        loop.USAGE = loop.Usage()
        self.calls = []       # scripted tool calls, consumed in order
        self.manager = []     # scripted manager replies, consumed in order
        self.first_messages = []

        def ask(base, model, messages, thinking, tool_choice="required", max_tokens=16384):
            if tool_choice == "none":
                if messages[0]["content"] == loop.MANAGER_SYSTEM:
                    return {"content": self.manager.pop(0)}
                return {"content": "## Built\n- lib.rs\n## Next step\n- run the gate"}
            if not self.first_messages or self.first_messages[-1] is not messages[1]:
                self.first_messages.append(messages[1])
            name, args = self.calls.pop(0)
            return {"content": "", "tool_calls": [
                {"id": str(len(self.calls)), "function": {"name": name, "arguments": loop.json.dumps(args)}}]}

        loop.ask = ask
        loop.run_shell = lambda cmd, timeout: self.shell(cmd)

    def tearDown(self):
        (loop.BENCH, loop.READ_ROOTS, loop.run_shell, loop.ask, loop.wait_for_hint, sys.argv,
         loop.USAGE) = self.saved
        shutil.rmtree(self.tmp)

    def run_loop(self, *extra):
        sys.argv = ["loop.py", "--task", "task.md", "--variant", "plugin-x", "--model", "m", "--no-ledger",
                    "--read-budget", "3", "--read-streak", "3", *extra]
        loop.run(loop.parse_args())
        run_dir = next((self.tmp / "qwen" / "runs").iterdir())
        return run_dir, loop.json.loads((run_dir / "status.json").read_text())

    def test_milestone_rewrites_notes_and_restarts_then_finishes(self):
        self.shell = lambda cmd: "GATE PASSED" if "bash" in cmd else "Finished `release` profile"
        self.calls = [("write_file", {"path": "pumpkin/plugin-x/src/lib.rs", "content": "// x\n"}),
                      ("cargo_check", {}), ("run_gate", {}), ("finish", {"summary": "done"})]
        run_dir, status = self.run_loop()
        self.assertEqual(status["state"], "finished")
        self.assertIn("## Built", (run_dir / "notes.md").read_text())
        self.assertTrue((run_dir / "MILESTONE-2.md").exists())
        self.assertEqual(len(self.first_messages), 2)  # the original conversation and one fresh one
        self.assertIn("notes file (notes.md)", self.first_messages[1]["content"])

    def test_stall_gets_a_manager_step_and_no_progress_after_it_escalates(self):
        self.shell = lambda cmd: "Finished"
        self.calls = [("read_file", {"path": "api.wit"})] * 6
        self.manager = ["### NEXT\nWrite src/lib.rs now.\n### WHY\nNothing written."]
        loop.wait_for_hint = lambda run_dir, poll=10: loop.STOP
        run_dir, status = self.run_loop()
        self.assertEqual(status["state"], "stopped")
        self.assertIn("Write src/lib.rs now.", (run_dir / "MANAGER-4.md").read_text())
        self.assertIn("after an automatic nudge at turn 4", (run_dir / "ESCALATION-7.md").read_text())
        # After the manager step, reading outside the crate was refused.
        turns = [loop.json.loads(l) for l in (run_dir / "turns.jsonl").read_text().splitlines()]
        self.assertIn("switched off", turns[-1]["result"])

    def test_progress_in_the_turn_of_a_manager_step_earns_another_step(self):
        self.shell = lambda cmd: "Finished"
        reads = [("read_file", {"path": "api.wit"})] * 3
        write = ("write_file", {"path": "pumpkin/plugin-x/src/lib.rs", "content": "// x\n"})
        self.calls = reads + [write] + reads + reads
        self.manager = ["### NEXT\nWrite src/lib.rs now.\n### WHY\nNothing written.",
                        "### NEXT\nRun cargo_check.\n### WHY\nCode exists."]
        loop.wait_for_hint = lambda run_dir, poll=10: loop.STOP
        run_dir, status = self.run_loop()
        self.assertTrue((run_dir / "MANAGER-4.md").exists())
        self.assertTrue((run_dir / "MANAGER-8.md").exists())  # the write at turn 4 counted
        self.assertIn("after an automatic nudge at turn 8", (run_dir / "ESCALATION-11.md").read_text())

    def test_manager_repeating_a_step_escalates(self):
        self.shell = lambda cmd: "Finished"
        reads = [("read_file", {"path": "api.wit"})] * 3
        write = ("write_file", {"path": "pumpkin/plugin-x/src/lib.rs", "content": "// x\n"})
        self.calls = reads + [write, write] + reads
        self.manager = ["### NEXT\nWrite src/lib.rs now.\n### WHY\nNothing written.",
                        "### NEXT\nwrite src/lib.rs now\n### WHY\nStill stuck."]
        loop.wait_for_hint = lambda run_dir, poll=10: loop.STOP
        run_dir, status = self.run_loop()
        self.assertTrue((run_dir / "MANAGER-4.md").exists())
        self.assertIn("repeated an earlier step", (run_dir / "ESCALATION-9.md").read_text())
        self.assertEqual(status["state"], "stopped")

    def test_plain_mode_uses_the_generic_nudge(self):
        self.shell = lambda cmd: "Finished"
        self.calls = [("read_file", {"path": "api.wit"})] * 6
        loop.wait_for_hint = lambda run_dir, poll=10: loop.STOP
        run_dir, status = self.run_loop("--ledger", "off")
        self.assertTrue((run_dir / "NUDGE-4.md").exists())
        self.assertFalse((run_dir / "notes.md").exists())
        self.assertEqual(status["state"], "stopped")


class ScaffoldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        (self.tmp / "tpl" / "src").mkdir(parents=True)
        (self.tmp / "tpl" / "Cargo.toml").write_text('name = "psb-VARIANT"\n')
        (self.tmp / "tpl" / "src" / "lib.rs").write_text("// skeleton\n")
        self.saved = loop.BENCH
        loop.BENCH = self.tmp

    def tearDown(self):
        loop.BENCH = self.saved
        shutil.rmtree(self.tmp)

    def test_template_is_copied_with_the_name_filled_in(self):
        loop.scaffold("plugin-x", "tpl")
        crate = self.tmp / "pumpkin" / "plugin-x"
        self.assertEqual((crate / "Cargo.toml").read_text(), 'name = "psb-plugin-x"\n')
        self.assertEqual((crate / "src" / "lib.rs").read_text(), "// skeleton\n")

    def test_existing_crate_is_left_alone(self):
        crate = self.tmp / "pumpkin" / "plugin-x"
        crate.mkdir(parents=True)
        (crate / "Cargo.toml").write_text("mine\n")
        loop.scaffold("plugin-x", "tpl")
        self.assertEqual((crate / "Cargo.toml").read_text(), "mine\n")
        self.assertFalse((crate / "src").exists())


class TranscriptTest(unittest.TestCase):
    def test_reread_replaces_the_older_read(self):
        t = loop.Transcript("sys", "task")
        t.add_tool("1", "read_file", {"path": "src/lib.rs"}, "old contents")
        t.add_tool("2", "read_file", {"path": "src/lib.rs"}, "new contents")
        tools = [m["content"] for m in t.messages if m["role"] == "tool"]
        self.assertTrue(tools[0].startswith("[older read of src/lib.rs"))
        self.assertEqual(tools[1], "new contents")

    def test_write_replaces_older_reads_but_not_other_files(self):
        t = loop.Transcript("sys", "task")
        t.add_tool("1", "read_file", {"path": "a"}, "aaa")
        t.add_tool("2", "read_file", {"path": "b"}, "bbb")
        t.add_tool("3", "write_file", {"path": "a"}, "wrote a")
        tools = [m["content"] for m in t.messages if m["role"] == "tool"]
        self.assertTrue(tools[0].startswith("[older read of a"))
        self.assertEqual(tools[1], "bbb")

    def test_old_results_are_trimmed(self):
        t = loop.Transcript("sys", "task")
        for i in range(loop.KEEP_FULL_RESULTS + 5):
            t.add_tool(str(i), "search", {"pattern": "x", "path": "."}, "y" * 1000)
        tools = [m["content"] for m in t.messages if m["role"] == "tool"]
        self.assertTrue(tools[0].endswith("[... older result trimmed]"))
        self.assertEqual(len(tools[-1]), 1000)
        self.assertEqual(sum(len(c) == 1000 for c in tools), loop.KEEP_FULL_RESULTS)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        loop.USAGE = loop.Usage()
        self.sent = []

        def fake_run(argv, input=None, **kw):
            self.sent.append((argv, input))
            return type("R", (), {"returncode": 0})()

        self.real_run = loop.subprocess.run
        loop.subprocess.run = fake_run

    def tearDown(self):
        loop.subprocess.run = self.real_run

    def args(self, **kw):
        base = {"no_ledger": False, "variant": "wasm-test", "ledger_host": "h", "ledger_path": "~/log.jsonl"}
        return type("A", (), {**base, **kw})()

    def test_record_matches_the_tracker_schema(self):
        loop.USAGE.add({"prompt_tokens": 1000, "completion_tokens": 50})
        loop.USAGE.add({"prompt_tokens": 2000, "completion_tokens": 70})
        loop.USAGE.state = "finished"
        loop.append_to_ledger(self.args())
        (argv, line), = self.sent
        self.assertEqual(argv[-2:], ["h", "cat >> ~/log.jsonl"])
        rec = loop.json.loads(line)
        self.assertEqual(rec["label"], "psb-loop:wasm-test")
        self.assertEqual((rec["turns"], rec["output"], rec["prompt_tokens_unsplit"]), (2, 120, 3000))
        self.assertEqual((rec["input"], rec["cache_read"], rec["cache_creation"]), (0, 0, 0))
        self.assertTrue(rec["split_unmeasured"])
        self.assertEqual(rec["status"], "finished")

    def test_nothing_logged_without_calls_or_when_disabled(self):
        loop.append_to_ledger(self.args())
        loop.USAGE.add({"prompt_tokens": 1, "completion_tokens": 1})
        loop.append_to_ledger(self.args(no_ledger=True))
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
