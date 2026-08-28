"""Transport-independent client IP resolution behind explicitly trusted proxies."""
from __future__ import annotations

import ipaddress
from collections.abc import Iterable


def resolve_client_ip(
    *,
    peer_ip: str | None,
    forwarded_ip: str | None,
    trusted_proxy_cidrs: Iterable[str],
) -> str:
    """Return a canonical client IP without trusting caller-controlled headers.

    The socket peer is authoritative unless it belongs to an explicitly
    configured exact proxy peer. Even then, the forwarded value must be exactly
    one valid IP address; malformed and chained values fall back to the socket
    peer.
    """
    try:
        peer = ipaddress.ip_address(peer_ip or "")
    except ValueError:
        return "unknown"

    trusted = False
    for raw_network in trusted_proxy_cidrs:
        try:
            network = ipaddress.ip_network(raw_network, strict=True)
        except ValueError:
            continue
        # Settings rejects broad networks. Keep this defensive check here too so
        # direct callers cannot accidentally turn a whole private subnet into a
        # trusted identity authority.
        if network.prefixlen != network.max_prefixlen:
            continue
        if peer.version == network.version and peer in network:
            trusted = True
            break

    if not trusted or not forwarded_ip:
        return str(peer)

    try:
        forwarded = ipaddress.ip_address(forwarded_ip.strip())
    except ValueError:
        return str(peer)
    return str(forwarded)
