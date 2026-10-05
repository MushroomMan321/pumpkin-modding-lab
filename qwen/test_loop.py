"""Offline tests for the loop's stall detection and transcript trimming. Run: python3 qwen/test_loop.py"""

import sys
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

    def test_gate_pass_clears_failures(self):
        p = self.p()
        p.note(1, "write_file", {"path": "a"}, "wrote a")
        p.note(2, "run_gate", {}, FAIL.format("smoke"))
        p.note(3, "run_gate", {}, FAIL.format("smoke"))
        p.note(4, "run_gate", {}, "GATE PASSED")
        p.note(5, "run_gate", {}, FAIL.format("smoke"))
        self.assertIsNone(p.stuck(6))


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
