#!/usr/bin/env python3
"""todos — a tiny command-line todo app with JSON storage.

Usage:
  python3 todo.py add "Buy milk" [--priority high|medium|low]
  python3 todo.py list [--all]
  python3 todo.py done <id>
  python3 todo.py undo <id>
  python3 todo.py rm <id>
  python3 todo.py clear-done
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime

STORE = os.environ.get("TODO_FILE", os.path.expanduser("~/.todos.json"))

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
MARKS = {True: "x", False: " "}


def load():
    if not os.path.exists(STORE):
        return []
    try:
        with open(STORE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        print(f"warning: could not read {STORE}, starting fresh", file=sys.stderr)
        return []


def save(items):
    with open(STORE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)


def find(items, id_prefix):
    """Find exactly one item whose id starts with id_prefix (ids may be shortened)."""
    matches = [t for t in items if t["id"].startswith(id_prefix)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise KeyError(f"no todo with id '{id_prefix}'")
    raise KeyError(f"id '{id_prefix}' is ambiguous ({len(matches)} matches)")


def add(text, priority="medium"):
    if not text.strip():
        raise ValueError("task text cannot be empty")
    if priority not in PRIORITY_ORDER:
        raise ValueError(f"priority must be one of: {', '.join(PRIORITY_ORDER)}")
    items = load()
    item = {
        "id": uuid.uuid4().hex[:8],
        "text": text.strip(),
        "done": False,
        "priority": priority,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    items.append(item)
    save(items)
    print(f"added [{item['id']}] {item['text']} ({priority})")
    return item


def list_items(show_all=False):
    items = load()
    if not show_all:
        items = [t for t in items if not t["done"]]
    items.sort(key=lambda t: (t["done"], PRIORITY_ORDER.get(t.get("priority", "medium"), 1), t["created"]))
    if not items:
        print("nothing to do! 🎉")
        return []
    for t in items:
        mark = MARKS[t["done"]]
        prio = t.get("priority", "medium")[0].upper()
        print(f"[{mark}] {t['id']}  ({prio})  {t['text']}")
    print(f"\n{len(items)} item(s)")
    return items


def set_done(id_prefix, done=True):
    items = load()
    item = find(items, id_prefix)
    item["done"] = done
    save(items)
    print(f"{'done' if done else 'reopened'}: {item['text']}")
    return item


def remove(id_prefix):
    items = load()
    item = find(items, id_prefix)
    items.remove(item)
    save(items)
    print(f"removed: {item['text']}")
    return item


def clear_done():
    items = load()
    remaining = [t for t in items if not t["done"]]
    removed = len(items) - len(remaining)
    save(remaining)
    print(f"removed {removed} completed item(s)")
    return removed


def main(argv=None):
    p = argparse.ArgumentParser(prog="todo", description="tiny CLI todo app")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="add a new task")
    p_add.add_argument("text", nargs="+")
    p_add.add_argument("-p", "--priority", default="medium", choices=sorted(PRIORITY_ORDER))

    p_list = sub.add_parser("list", help="list open tasks")
    p_list.add_argument("-a", "--all", action="store_true", help="include completed")

    for name, fn, help_ in (("done", lambda i: set_done(i, True), "mark task done"),
                            ("undo", lambda i: set_done(i, False), "reopen task"),
                            ("rm", remove, "delete a task")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("id")

    sub.add_parser("clear-done", help="delete all completed tasks")

    args = p.parse_args(argv)
    try:
        if args.cmd == "add":
            add(" ".join(args.text), args.priority)
        elif args.cmd == "list":
            list_items(args.all)
        elif args.cmd == "done":
            set_done(args.id, True)
        elif args.cmd == "undo":
            set_done(args.id, False)
        elif args.cmd == "rm":
            remove(args.id)
        elif args.cmd == "clear-done":
            clear_done()
    except (ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
