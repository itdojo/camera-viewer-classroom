#!/usr/bin/env python3
"""commit-msg hook: refuse a message that would keep git's own comment lines.

Usage: check-commit-msg.py <message file>   (git passes the file; pre-commit installs it)

`git commit --no-edit` after a conflicted merge keeps git's commented `# Conflicts:` block in
the message, because git strips comments only from messages it opened an editor for. Nine
merges on main carry one. A line is a git comment when it is `#` alone or `#` then whitespace,
so `#23 fixes the badge` passes.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

COMMENT = re.compile(r"^#(\s|$)")
SCISSORS = re.compile(r"^# -+ >8 -+$")


def cleanup_mode() -> str:
    """How git will clean this message up: strip, whitespace, verbatim or scissors.

    The hook runs before the cleanup. Without `commit.cleanup` set, git strips comments only
    when it opened an editor, and when it opens none (-m, -F, --no-edit) it tells the hook by
    setting GIT_EDITOR=:. A `--cleanup=` on the command line is invisible here.
    """
    configured = subprocess.run(["git", "config", "--get", "commit.cleanup"],
                                capture_output=True, text=True).stdout.strip()
    if configured and configured != "default":
        return configured
    return "whitespace" if os.environ.get("GIT_EDITOR") == ":" else "strip"


def leftover_comments(message: str, mode: str) -> list[str]:
    if mode == "strip":
        return []
    lines = message.splitlines()
    if mode == "scissors":
        lines = lines[:next((i for i, line in enumerate(lines) if SCISSORS.match(line)), len(lines))]
    return [line for line in lines if COMMENT.match(line)]


def main(argv: list[str]) -> int:
    message = Path(argv[1]).read_text(encoding="utf-8", errors="replace")
    leftovers = leftover_comments(message, cleanup_mode())
    if not leftovers:
        return 0
    print("commit-msg: refused. These git comment lines would stay in the message:", file=sys.stderr)
    for line in leftovers:
        print(f"    {line}", file=sys.stderr)
    print("Nothing was committed. Commit again without --no-edit, -m or -F so git strips them in the\n"
          "editor, or pass a message without them.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
