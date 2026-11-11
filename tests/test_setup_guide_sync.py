"""The Device setup guide (#100) and the kit's own README.md repeat the
README's Device Kit commands for students, who can't read the private repo.
Each command the guide marks `data-sync`, and each line of the kit README's
`sh` blocks, must appear in README.md as written, so a change to one without
the other fails here rather than in front of a class."""

import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class SyncedCommands(HTMLParser):
    """The text of every element carrying `data-sync`."""

    def __init__(self):
        super().__init__()
        self.commands = []
        self._open = []  # one entry per open element: the text so far, or None if not synced

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self._open.append("" if "data-sync" in dict(attrs) else None)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        text = self._open.pop()
        if text is not None:
            self.commands.append(text)

    def handle_data(self, data):
        self._open = [text + data if text is not None else None for text in self._open]


def synced_commands():
    parser = SyncedCommands()
    parser.feed((ROOT / "frontend" / "setup.html").read_text())
    return parser.commands


def test_the_guide_marks_its_commands():
    # The guide's main path alone runs a dozen commands; a parser that found
    # none would let every drift through.
    assert len(synced_commands()) >= 10


def test_every_synced_command_is_in_the_readme_as_written():
    readme = (ROOT / "README.md").read_text()

    assert [command for command in synced_commands() if command not in readme] == []


def kit_readme_commands():
    kit_readme = (ROOT / "device-kit" / "README.md").read_text()
    return [line for block in re.findall(r"```sh\n(.*?)```", kit_readme, re.S) for line in block.splitlines()]


def test_every_command_in_the_kits_readme_is_in_the_readme_as_written():
    readme = (ROOT / "README.md").read_text()
    commands = kit_readme_commands()

    assert len(commands) >= 8  # as above: finding none would let every drift through
    assert [command for command in commands if command not in readme] == []
