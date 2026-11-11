"""The Device Kit archive the image serves (#99) never carries what a Device
generates, even when the build context holds it: a maintainer's checkout that
has run the kit has a real `.env`, certificate and key beside the committed
files. Builds only the image's `kit` stage, from a copy of the context with
those files planted in it."""

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.docker

ROOT = Path(__file__).resolve().parents[1]

PLANTED = [
    "device-kit/usb/.env",
    "device-kit/usb/certs/server.key",
    "device-kit/usb/certs/server.crt",
    "device-kit/csi/.env",
    "device-kit/csi/certs/server.key",
    "device-kit/csi/compose.override.yaml",
    "device-kit/test-pattern/certs/server.pem",
]


@pytest.fixture
def kit_listing(tmp_path):
    context = tmp_path / "context"
    context.mkdir()
    shutil.copy(ROOT / "Dockerfile", context)
    shutil.copy(ROOT / ".dockerignore", context)
    shutil.copytree(ROOT / "device-kit", context / "device-kit",
                    ignore=shutil.ignore_patterns(".env", "certs", "compose.override.yaml"))
    for name in PLANTED:
        planted = context / name
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text("INSTANCE_PASS=planted-secret\n")

    tag = f"camera-viewer-kit-test:{uuid.uuid4().hex[:12]}"
    try:
        subprocess.run(["docker", "build", "--quiet", "--target", "kit", "-t", tag, str(context)],
                       check=True, capture_output=True, timeout=300)
        listing = subprocess.run(["docker", "run", "--rm", tag, "tar", "-tzf", "/kit/device-kit.tar.gz"],
                                 check=True, capture_output=True, text=True, timeout=60).stdout
    finally:
        subprocess.run(["docker", "image", "rm", "-f", tag], capture_output=True, timeout=60)
    return set(listing.split())


def test_no_planted_secret_reaches_the_archive(kit_listing):
    assert kit_listing & set(PLANTED) == set()
    assert not any("/certs/" in name or name.endswith("/certs") for name in kit_listing)


def test_the_committed_kit_is_still_in_the_archive(kit_listing):
    assert {"device-kit/README.md", "device-kit/gen-cert.sh", "device-kit/usb/compose.yaml", "device-kit/csi/compose.yaml",
            "device-kit/test-pattern/compose.yaml", "device-kit/gps/compose.yaml"} <= kit_listing
