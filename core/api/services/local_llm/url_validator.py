"""Validate external LLM gateway URLs before network clients use them."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


BLOCKED_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def validate_llm_base_url(url: str) -> None:
    """Raise ValueError when an LLM URL can hit local or private networks."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Invalid scheme: {parsed.scheme}")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("No hostname in URL")

    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"DNS resolution failed: {hostname}") from exc

    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip = ipaddress.ip_address(sockaddr[0])
        for blocked in BLOCKED_NETWORKS:
            if ip in blocked:
                raise ValueError(
                    f"LLM_BASE_URL resolves to blocked network: {ip} ({blocked})"
                )
