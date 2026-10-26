"""Validation helpers for an administrator-selected local ROSBridge target."""

from __future__ import annotations

import ipaddress


_ROBOT_LAN_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_EXPLICIT_LOCAL_ADDRESSES = {ipaddress.IPv4Address("192.0.0.4")}


def rosbridge_url(robot_ip: str, port: int) -> str:
    """Build a ws URL for a literal RFC1918 robot address and valid TCP port."""
    try:
        address = ipaddress.ip_address(robot_ip.strip())
    except ValueError as exc:
        raise ValueError("Enter a valid private IPv4 address") from exc
    if not isinstance(address, ipaddress.IPv4Address) or not (
        address in _EXPLICIT_LOCAL_ADDRESSES
        or any(address in network for network in _ROBOT_LAN_NETWORKS)
    ):
        raise ValueError("Robot address must be on a private IPv4 network")
    if isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    return f"ws://{address}:{port}"
