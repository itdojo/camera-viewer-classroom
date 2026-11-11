"""Seam A: the instance as one deployable unit — one compose command, and
nothing but the UI and WebRTC media facing the network."""


def listeners(instance, proto):
    """(address, port) of every socket listening inside the instance, read from
    /proc/net: TCP sockets in LISTEN, UDP sockets bound but unconnected."""
    found = set()
    for family in (proto, proto + "6"):
        result = instance.exec("cat", f"/proc/net/{family}")
        for line in result.stdout.splitlines()[1:]:
            local, remote, state = line.split()[1:4]
            if proto == "tcp" and state != "0A":
                continue
            if proto == "udp" and not remote.endswith(":0000"):
                continue
            address, port = local.split(":")
            found.add((address, int(port, 16)))
    return found


def is_loopback(hex_address):
    # /proc/net prints addresses as little-endian hex words, so 127.0.0.0/8
    # ends in 7F. Docker's embedded DNS listens on 127.0.0.11.
    if len(hex_address) == 8:
        return hex_address.endswith("7F")
    return hex_address == "00000000000000000000000001000000"


def test_one_compose_command_brings_up_an_instance_with_no_cameras(instances):
    fresh = instances.start("fresh")

    with fresh.client() as api:
        assert api.get("/api/cameras").json() == []
        assert api.get("/").status_code == 200


def test_only_the_ui_and_webrtc_media_listen_beyond_localhost(instance):
    tcp, udp = listeners(instance, "tcp"), listeners(instance, "udp")

    assert {port for address, port in tcp if not is_loopback(address)} == {8080}
    assert {port for address, port in udp if not is_loopback(address)} == {instance.webrtc_udp_port}
    # The Relay is up: its API and WHEP signalling listen, on loopback only.
    docker_dns = "0B00007F"
    assert len({port for address, port in tcp if is_loopback(address) and address != docker_dns}) >= 2
