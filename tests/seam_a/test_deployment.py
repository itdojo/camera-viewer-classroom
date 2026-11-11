"""Seam A: the deployable unit as a maintainer runs it from the README.

Linux runs ``compose.yaml`` alone (host networking). macOS Docker Desktop adds
``compose.macos.yaml``, which publishes the UI and WebRTC media and advertises
the one host-IP variable, ``CV_HOST_IP``, to browsers. Settings live in
``.env`` next to the compose files. These checks render the compose files the
way ``docker compose`` would, in a scratch project directory, so a
developer's own ``.env`` never leaks in. Playing a Feed over the macOS path is
covered by every Seam B test: the harness runs the same overlay."""

import json
import os
import subprocess

from harness.instance import COMPOSE, MACOS

SERVICE = "camera-viewer"


def render(project_dir, dotenv, *files):
    (project_dir / ".env").write_text("".join(f"{k}={v}\n" for k, v in dotenv.items()))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CV_", "COMPOSE_"))}
    cmd = ["docker", "compose", "--project-directory", str(project_dir)]
    for file in files:
        cmd += ["-f", str(file)]
    return subprocess.run([*cmd, "config", "--format", "json"], env=env,
                          capture_output=True, text=True, timeout=60)


def service(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["services"][SERVICE]


def published(svc):
    return {(p["protocol"], str(p.get("published")), p["target"]) for p in svc.get("ports", [])}


def test_the_macos_path_refuses_to_start_without_the_host_ip(tmp_path):
    result = render(tmp_path, {"CV_PASSWORD": "pw"}, COMPOSE, MACOS)

    assert result.returncode != 0
    assert "CV_HOST_IP" in result.stderr


def test_the_macos_path_publishes_the_ui_and_webrtc_media_and_advertises_the_host_ip(tmp_path):
    svc = service(render(tmp_path, {"CV_PASSWORD": "pw", "CV_HOST_IP": "192.0.2.10"}, COMPOSE, MACOS))

    assert svc.get("network_mode") is None
    assert published(svc) == {("tcp", "8080", 8080), ("udp", "8189", 8189)}
    assert svc["environment"]["MTX_WEBRTCADDITIONALHOSTS"] == "192.0.2.10"


def test_the_ports_can_move_and_webrtc_media_stays_one_to_one(tmp_path):
    dotenv = {"CV_PASSWORD": "pw", "CV_HOST_IP": "192.0.2.10", "CV_HTTP_PORT": "8443",
              "CV_WEBRTC_UDP_PORT": "18189"}
    svc = service(render(tmp_path, dotenv, COMPOSE, MACOS))

    assert published(svc) == {("tcp", "8443", 8443), ("udp", "18189", 18189)}
    # The Relay advertises its own port in ICE, so it must listen on the published one.
    assert svc["environment"]["MTX_WEBRTCLOCALUDPADDRESS"] == ":18189"
    assert svc["environment"]["CV_HTTP_PORT"] == "8443"


def test_linux_uses_host_networking(tmp_path):
    svc = service(render(tmp_path, {"CV_PASSWORD": "pw"}, COMPOSE))

    assert svc["network_mode"] == "host"
    assert not svc.get("ports")


def test_every_setting_in_dotenv_reaches_the_instance(tmp_path):
    dotenv = {"CV_PASSWORD": "pw", "TZ": "America/Chicago", "FEED_DOWN_AFTER_SECONDS": "7",
              "CV_TLS_CERT": "/config/tls/cert.pem", "CV_TLS_KEY": "/config/tls/key.pem"}
    svc = service(render(tmp_path, dotenv, COMPOSE))

    for key, value in dotenv.items():
        assert svc["environment"][key] == value, key
