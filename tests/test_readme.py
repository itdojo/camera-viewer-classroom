"""The README is the maintainer's deployment guide: it must name every
environment variable the instance reads, and say how Device credentials are
stored. No Docker needed.

Variables are collected from the backend's string literals that look like
environment variable names, from the Feed Status thresholds (named
``FEED_<FIELD>``), Auto's timings (``QUALITY_<FIELD>``) and the Position Service's timings
(``POSITION_<FIELD>``, each in a table row), and from every ``${VAR}`` the compose files interpolate.
A variable built some other way must be added to ``extra_variables``. The
Device Kit's GPS Sidecar settings (``GPS_*`` in ``device-kit/gps/compose.yaml``)
each need a table row too.
"""

import ast
import dataclasses
import re
from pathlib import Path

from camera_viewer.feed_status import Thresholds
from camera_viewer.positions import PositionTimings
from camera_viewer.quality_auto import AutoThresholds

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
BACKEND = REPO / "backend" / "camera_viewer"
COMPOSE_FILES = [REPO / "compose.yaml", REPO / "compose.macos.yaml"]
GPS_COMPOSE = REPO / "device-kit" / "gps" / "compose.yaml"
# Any SHOUTY_SNAKE string literal in the backend counts as a variable name.
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$")


def extra_variables():
    # Read by the Python runtime, not by our code: local time for file names.
    return {"TZ"}


def backend_variables():
    names = set()
    for source in BACKEND.rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and ENV_NAME.match(node.value):
                names.add(node.value)
    names |= {f"FEED_{field.name.upper()}" for field in dataclasses.fields(Thresholds)}
    names |= {f"QUALITY_{field.name.upper()}" for field in dataclasses.fields(AutoThresholds)}
    names |= position_variables()
    return names


def position_variables():
    """The Position Service's timings, each read as ``POSITION_<FIELD>``."""
    return {f"POSITION_{field.name.upper()}" for field in dataclasses.fields(PositionTimings)}


def compose_variables():
    names = set()
    for file in COMPOSE_FILES:
        names |= set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", file.read_text()))
    return names


def gps_sidecar_settings():
    """The kit settings the GPS Sidecar's shared compose file reads (``GPS_*``)."""
    return set(re.findall(r"\$\{(GPS_[A-Z0-9_]*)", GPS_COMPOSE.read_text()))


def documented():
    return set(re.findall(r"`([A-Z][A-Z0-9_]+)`", README.read_text()))


def test_the_readme_documents_every_gps_sidecar_setting_in_a_table_row():
    rows = set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \|", README.read_text(), re.MULTILINE))

    assert gps_sidecar_settings(), "no GPS_* settings found; has the Sidecar's compose file moved?"
    missing = sorted(gps_sidecar_settings() - rows)

    assert not missing, f"README.md has no table row for: {missing}"


def test_the_readme_documents_every_position_setting_in_a_table_row():
    rows = set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \|", README.read_text(), re.MULTILINE))

    missing = sorted(position_variables() - rows)

    assert not missing, f"README.md has no table row for: {missing}"


def test_the_readme_documents_every_environment_variable_the_instance_reads():
    read = backend_variables() | compose_variables() | extra_variables()

    missing = sorted(read - documented())

    assert not missing, f"README.md does not document: {missing}"


def test_the_readme_says_device_credentials_are_stored_in_plain_text_with_mode_0600():
    text = README.read_text().lower()

    assert "plain text" in text
    assert "0600" in text
