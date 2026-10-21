#!/usr/bin/env python3
"""A stand-in for the 1Password CLI, covering just what op-env-sync calls.

Items live as JSON files under $FAKE_OP_DIR, one per (account, vault, title), so
tests can check which account and vault a command touched. Every call is
appended to calls.log. With FAKE_OP_MULTI set, a call without --account fails
the way op does when several accounts are signed in.
"""

import json
import os
import sys
import uuid
from pathlib import Path

root = Path(os.environ["FAKE_OP_DIR"])
args = sys.argv[1:]
with (root / "calls.log").open("a") as log:
    log.write(json.dumps(args) + "\n")


def flag(name: str, default: str | None = None) -> str | None:
    if name in args:
        i = args.index(name)
        value = args[i + 1]
        del args[i : i + 2]
        return value
    return default


account = flag("--account")
vault = flag("--vault", "Private")
fmt = flag("--format")
if account is None:
    if os.environ.get("FAKE_OP_MULTI"):
        sys.exit("[ERROR] multiple accounts found. Use the --account flag or set the OP_ACCOUNT environment variable")
    account = "my.1password.com"


def path(title: str) -> Path:
    return root / f"{account}__{vault}__{title}.json"


if args[:1] == ["whoami"]:
    print(json.dumps({"url": account, "email": f"me@{account}"}))
elif args[:2] == ["item", "get"]:
    if not path(args[2]).exists():
        sys.exit(f'[ERROR] "{args[2]}" isn\'t an item. Specify the item with its UUID, name, or domain.')
    print(path(args[2]).read_text())
elif args[:3] == ["item", "create", "-"]:
    item = json.load(sys.stdin)
    item["id"] = uuid.uuid4().hex
    path(item["title"]).write_text(json.dumps(item))
    print(json.dumps(item))
elif args[:2] == ["item", "edit"]:
    item = json.load(sys.stdin)
    existing = json.loads(path(item["title"]).read_text())
    assert item["id"] == args[2] == existing["id"], "edit must target the item's own id"
    path(item["title"]).write_text(json.dumps(item))
    print(json.dumps(item))
else:
    sys.exit(f"fake op: unsupported call {args}")
