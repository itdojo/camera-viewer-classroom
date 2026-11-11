"""Seam A: the instance serves the Device Kit it was built with (#99), so a
student can build a Device without access to this repo."""

import io
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def tracked_kit_files():
    """The kit as committed: what a Device should get, and nothing a
    maintainer's own Devices generated beside it."""
    listed = subprocess.run(["git", "ls-files", "device-kit"], cwd=ROOT, capture_output=True, text=True,
                            check=True).stdout
    return set(listed.split())


def kit_archive(api):
    response = api.get("/device-kit.tar.gz")
    assert response.status_code == 200
    return response, tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz")


def test_the_kit_downloads_with_a_session_and_is_revalidated(instance):
    with instance.client() as api:
        response, _ = kit_archive(api)

    assert response.headers["cache-control"] == "no-cache"


def test_the_kit_needs_a_session(instance):
    with instance.client(login=False) as api:
        response = api.get("/device-kit.tar.gz")

    assert response.status_code == 303


def test_the_kit_holds_exactly_the_committed_files_under_one_folder(instance):
    with instance.client() as api:
        _, archive = kit_archive(api)

    files = {member.name for member in archive.getmembers() if member.isfile()}
    assert files == tracked_kit_files()


def test_gen_cert_stays_executable(instance):
    with instance.client() as api:
        _, archive = kit_archive(api)

    assert archive.getmember("device-kit/gen-cert.sh").mode & 0o777 == 0o755
