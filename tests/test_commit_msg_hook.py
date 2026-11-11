"""The commit-msg hook, driven through real `git commit` runs in a throwaway repository, so
what is tested is what git actually hands the hook (and whether it then strips comments)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-commit-msg.py"


@pytest.fixture
def repo(tmp_path):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    env.pop("GIT_EDITOR", None)

    def git(*args, editor=None):
        run_env = env if editor is None else {**env, "GIT_EDITOR": editor}
        return subprocess.run(["git", *args], cwd=tmp_path, env=run_env, capture_output=True, text=True)

    git("init", "-q", "-b", "main")
    hook = tmp_path / ".git" / "hooks" / "commit-msg"
    hook.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{SCRIPT}" "$1"\n')
    hook.chmod(0o755)
    git("commit", "-q", "--allow-empty", "-m", "init")
    return git


def last_message(git):
    return git("log", "-1", "--format=%B").stdout.strip()


def test_a_message_carrying_the_conflicts_block_is_rejected(repo):
    result = repo("commit", "--allow-empty", "-m", "Merge main", "-m", "# Conflicts:\n#\tbackend/app.py")
    assert result.returncode != 0
    assert "# Conflicts:" in result.stderr
    assert last_message(repo) == "init"


def editor_writing(tmp_path, text):
    """A GIT_EDITOR that puts `text` above git's commented template and saves, as a person would."""
    script = tmp_path / "editor.py"
    script.write_text("import sys\n"
                      "path = sys.argv[1]\n"
                      "with open(path) as f:\n"
                      "    template = f.read()\n"
                      "with open(path, 'w') as f:\n"
                      f"    f.write({text!r} + template)\n")
    return f'"{sys.executable}" "{script}"'


def test_an_editor_commit_passes_because_git_strips_its_own_template(repo, tmp_path):
    result = repo("commit", "--allow-empty", editor=editor_writing(tmp_path, "Subject from the editor\n"))
    assert result.returncode == 0, result.stderr
    assert last_message(repo) == "Subject from the editor"


def test_a_conflicted_merge_finished_with_no_edit_is_rejected(repo, tmp_path):
    """How main got its nine `# Conflicts:` merges: resolve, then `git commit --no-edit`."""
    (tmp_path / "app.py").write_text("base\n")
    repo("add", "app.py")
    repo("commit", "-q", "-m", "base")
    repo("checkout", "-q", "-b", "ticket")
    (tmp_path / "app.py").write_text("ticket\n")
    repo("commit", "-q", "-am", "ticket change")
    repo("checkout", "-q", "main")
    (tmp_path / "app.py").write_text("main\n")
    repo("commit", "-q", "-am", "main change")
    assert repo("merge", "--no-edit", "ticket").returncode != 0  # the conflict
    (tmp_path / "app.py").write_text("resolved\n")
    repo("add", "app.py")

    result = repo("commit", "--no-edit")

    assert result.returncode != 0
    assert "# Conflicts:" in result.stderr
    assert last_message(repo) == "main change"


def test_comments_pass_when_commit_cleanup_strips_them_anyway(repo):
    repo("config", "commit.cleanup", "strip")
    result = repo("commit", "--allow-empty", "-m", "Merge main", "-m", "# Conflicts:\n#\tbackend/app.py")
    assert result.returncode == 0, result.stderr
    assert last_message(repo) == "Merge main"


def test_comments_are_rejected_when_commit_cleanup_keeps_them_even_from_an_editor(repo, tmp_path):
    repo("config", "commit.cleanup", "whitespace")
    result = repo("commit", "--allow-empty", editor=editor_writing(tmp_path, "Subject from the editor\n"))
    assert result.returncode != 0
    assert last_message(repo) == "init"


def test_scissors_cleanup_checks_only_what_is_kept_above_the_scissors_line(repo, tmp_path):
    """With -v, scissors mode puts the template and diff below a line it cuts at, so they pass."""
    repo("config", "commit.cleanup", "scissors")
    (tmp_path / "notes.md").write_text("# A heading\n")
    repo("add", "notes.md")
    result = repo("commit", "-v", editor=editor_writing(tmp_path, "Add notes\n"))
    assert result.returncode == 0, result.stderr
    assert last_message(repo) == "Add notes"


def test_a_line_starting_with_an_issue_reference_is_not_a_comment(repo):
    result = repo("commit", "--allow-empty", "-m", "Fix the badge", "-m", "#23 asked for it.")
    assert result.returncode == 0, result.stderr
