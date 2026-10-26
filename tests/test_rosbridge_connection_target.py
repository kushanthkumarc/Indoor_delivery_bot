import pytest

from app.rosbridge.connection import rosbridge_url


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [
        ("10.20.30.40", 9090, "ws://10.20.30.40:9090"),
        ("172.20.1.4", 8080, "ws://172.20.1.4:8080"),
        ("192.168.1.20", 9090, "ws://192.168.1.20:9090"),
        ("192.0.0.4", 9090, "ws://192.0.0.4:9090"),
    ],
)
def test_private_robot_addresses_create_ws_endpoint(host, port, expected):
    assert rosbridge_url(host, port) == expected


@pytest.mark.parametrize("host", ["robot.local", "8.8.8.8", "127.0.0.1", "169.254.1.2", "::1"])
def test_non_private_or_non_ipv4_targets_are_rejected(host):
    with pytest.raises(ValueError):
        rosbridge_url(host, 9090)


@pytest.mark.parametrize("port", [0, -1, 65536, True])
def test_invalid_ports_are_rejected(port):
    with pytest.raises(ValueError):
        rosbridge_url("192.168.1.20", port)
