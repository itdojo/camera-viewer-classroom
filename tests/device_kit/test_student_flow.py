"""A student builds the test-pattern Device straight from the kit, no harness overlay:
generate the certificate, then one command."""

import os
import secrets
import shutil
import ssl
import subprocess
import time

import httpx
import pytest

from harness.devices import KIT_DIR

# Drives Docker directly rather than through a harness fixture (see tests/conftest.py).
pytestmark = pytest.mark.docker


def run(args, cwd, env=None, check=True):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=check, timeout=180)


def test_the_test_pattern_device_starts_with_one_command_after_its_certificate_is_generated(tmp_path):
    kit = tmp_path / "device-kit"
    shutil.copytree(KIT_DIR, kit, ignore=shutil.ignore_patterns("certs", ".env"))
    variant = kit / "test-pattern"

    run(["./gen-cert.sh", "test-pattern"], cwd=kit, env={**os.environ, "NO_COLOR": "1"})

    # Only what the shared Docker host forces on us: a unique project and
    # ephemeral loopback ports in place of the kit's default 8322 and 9997.
    env = {
        **os.environ,
        "COMPOSE_PROJECT_NAME": f"cvt-{secrets.token_hex(4)}-student",
        "RTSPS_PORT": "127.0.0.1:0",
        "API_PORT": "127.0.0.1:0",
    }
    try:
        run(["docker", "compose", "up", "-d"], cwd=variant, env=env)

        port = run(["docker", "compose", "port", "mediamtx", "9997"], cwd=variant, env=env).stdout
        host, _, host_port = port.strip().rpartition(":")
        assert (host, host_port) != ("0.0.0.0", "9997"), "kit ignored API_PORT"
        api_url = f"https://127.0.0.1:{host_port}"
        credentials = dict(line.split("=", 1) for line in (variant / ".env").read_text().split())
        trust = ssl.create_default_context(cafile=str(variant / "certs" / "server.crt"))

        deadline = time.monotonic() + 30
        with httpx.Client(base_url=api_url, verify=trust, timeout=3,
                          auth=(credentials["INSTANCE_USER"], credentials["INSTANCE_PASS"])) as api:
            while True:
                try:
                    path = api.get("/v3/paths/get/test-pattern")
                    if path.status_code == 200 and path.json()["available"]:
                        break
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline, "test-pattern never became available"
                time.sleep(0.5)
    finally:
        run(["docker", "compose", "down", "-v", "-t", "2"], cwd=variant, env=env, check=False)
