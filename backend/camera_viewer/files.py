"""Writing the config folder's files so a crash never leaves half of one.

``write_atomically`` writes a temporary sibling with the final mode, fsyncs
it and renames it over the target, so a container killed mid-write leaves
the previous file intact.
"""

from __future__ import annotations

import os
from pathlib import Path


def write_atomically(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.unlink(missing_ok=True)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    os.chmod(path, mode)
