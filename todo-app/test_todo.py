"""Tests for todo.py — run with: python3 -m unittest test_todo.py -v"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TODO = os.path.join(HERE, "todo.py")


class TodoTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = os.path.join(self.tmp.name, "todos.json")
        self.env = dict(os.environ, TODO_FILE=self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, TODO, *args],
            capture_output=True, text=True, env=self.env,
        )

    def add(self, text, *extra):
        return self.run_cli("add", text, *extra)

    def items(self):
        if not os.path.exists(self.store):
            return []
        with open(self.store) as f:
            return json.load(f)


class TestAdd(TodoTestBase):
    def test_add_creates_task(self):
        r = self.add("Buy milk")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Buy milk", r.stdout)
        items = self.items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "Buy milk")
        self.assertFalse(items[0]["done"])

    def test_add_multiword(self):
        r = self.run_cli("add", "water", "the", "plants")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.items()[0]["text"], "water the plants")

    def test_add_priority(self):
        self.add("urgent thing", "-p", "high")
        self.assertEqual(self.items()[0]["priority"], "high")

    def test_add_empty_rejected(self):
        r = self.run_cli("add", "   ")
        self.assertEqual(r.returncode, 1)
        self.assertIn("error", r.stderr)

    def test_add_bad_priority_rejected(self):
        r = self.run_cli("add", "task", "-p", "asap")
        self.assertNotEqual(r.returncode, 0)  # argparse rejects bad choices with exit 2
        self.assertIn("invalid choice", r.stderr)


class TestList(TodoTestBase):
    def test_list_hides_done_by_default(self):
        self.add("task one")
        self.add("task two")
        first = self.items()[0]["id"]
        self.run_cli("done", first)
        r = self.run_cli("list")
        self.assertIn("task two", r.stdout)
        self.assertNotIn("task one", r.stdout)

    def test_list_all_shows_done(self):
        self.add("task one")
        first = self.items()[0]["id"]
        self.run_cli("done", first)
        r = self.run_cli("list", "--all")
        self.assertIn("task one", r.stdout)
        self.assertIn("[x]", r.stdout)

    def test_list_empty(self):
        r = self.run_cli("list")
        self.assertIn("nothing to do", r.stdout)


class TestLifecycle(TodoTestBase):
    def test_done_then_undo(self):
        self.add("task")
        tid = self.items()[0]["id"]
        self.run_cli("done", tid)
        self.assertTrue(self.items()[0]["done"])
        self.run_cli("undo", tid)
        self.assertFalse(self.items()[0]["done"])

    def test_rm(self):
        self.add("task")
        tid = self.items()[0]["id"]
        r = self.run_cli("rm", tid)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.items(), [])

    def test_clear_done(self):
        self.add("a")
        self.add("b")
        ids = self.items()
        self.run_cli("done", ids[0]["id"])
        r = self.run_cli("clear-done")
        self.assertIn("1 completed", r.stdout)
        remaining = self.items()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["text"], "b")

    def test_short_id_prefix(self):
        self.add("task")
        full_id = self.items()[0]["id"]
        r = self.run_cli("done", full_id[:4])
        self.assertEqual(r.returncode, 0)

    def test_bad_id_fails(self):
        self.add("task")
        r = self.run_cli("done", "zzzzzzzz")
        self.assertEqual(r.returncode, 1)
        self.assertIn("error", r.stderr)


if __name__ == "__main__":
    unittest.main()
