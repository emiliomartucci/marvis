"""Consumer-side acceptance for the exact Plan A security projection."""
from __future__ import annotations

import ipaddress
import hashlib
import json
import os
from pathlib import Path
import subprocess

import aiosqlite
import bcrypt
from fastapi import HTTPException
import pytest
from starlette.requests import Request

from core.api.client_identity import resolve_client_ip
from core.api.config import Settings
from core.api.mcp import _adapter
from core.api.routers import auth as auth_router


ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "deploy" / "_template"


def _request(*headers: tuple[bytes, bytes], peer: str = "203.0.113.10") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "headers": list(headers),
            "client": (peer, 41234),
        }
    )


def test_proxy_policy_accepts_only_exact_host_routes() -> None:
    assert Settings(_env_file=None).trusted_proxy_cidrs == [
        "127.0.0.1/32",
        "::1/128",
    ]
    assert Settings(
        _env_file=None,
        trusted_proxy_cidrs=["10.2.3.4/32", "2001:db8::42/128"],
    ).trusted_proxy_cidrs == ["10.2.3.4/32", "2001:db8::42/128"]


@pytest.mark.parametrize(
    "cidr",
    ["not-a-network", "10.0.0.1/8", "10.0.0.0/8", "2001:db8::/32"],
)
def test_proxy_policy_rejects_invalid_or_broad_networks(cidr: str) -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, trusted_proxy_cidrs=[cidr])


def test_client_identity_trusts_only_the_exact_configured_peer() -> None:
    trusted = ["172.31.251.20/32"]
    assert resolve_client_ip(
        peer_ip="172.31.251.20",
        forwarded_ip="198.51.100.42",
        trusted_proxy_cidrs=trusted,
    ) == "198.51.100.42"
    assert resolve_client_ip(
        peer_ip="172.31.251.21",
        forwarded_ip="198.51.100.42",
        trusted_proxy_cidrs=trusted,
    ) == "172.31.251.21"
    assert resolve_client_ip(
        peer_ip="10.2.3.4",
        forwarded_ip="198.51.100.42",
        trusted_proxy_cidrs=["10.0.0.0/8"],
    ) == "10.2.3.4"


def test_public_template_coordinates_network_and_proxy_identity() -> None:
    values: dict[str, str] = {}
    env_text = (TEMPLATE / ".env.example").read_text(encoding="utf-8")
    for raw_line in env_text.splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value

    network = ipaddress.ip_network(values["MARVIS_NETWORK_SUBNET"], strict=True)
    addresses = [
        ipaddress.ip_address(values[name])
        for name in (
            "MARVIS_API_IPV4",
            "MARVIS_CONSOLE_IPV4",
            "MARVIS_NGINX_IPV4",
            "MARVIS_CLOUDFLARED_IPV4",
            "MARVIS_CADDY_IPV4",
        )
    ]
    assert len(set(addresses)) == len(addresses)
    assert all(address in network for address in addresses)
    assert values["TRUSTED_PROXY_CIDRS"] == '["127.0.0.1/32","::1/128"]'

    compose = (TEMPLATE / "docker-compose.yml").read_text(encoding="utf-8")
    assert "${MARVIS_NGINX_IPV4:-172.31.251.20}/32" in compose
    assert "${MARVIS_CADDY_IPV4:-172.31.251.40}/32" in compose
    assert "/etc/nginx/templates/default.conf.template:ro" in compose
    assert "TRUSTED_CLOUDFLARED_IP:" in compose

    nginx = (TEMPLATE / "nginx" / "default.conf").read_text(encoding="utf-8")
    assert "set_real_ip_from ${TRUSTED_CLOUDFLARED_IP};" in nginx
    assert nginx.count('proxy_set_header CF-Connecting-IP "";') == 3
    assert nginx.count("proxy_set_header X-Marvis-Client-IP $remote_addr;") == 3

    caddy = (TEMPLATE / "caddy" / "Caddyfile.example").read_text(encoding="utf-8")
    assert caddy.count("header_up -CF-Connecting-IP") == 4
    assert caddy.count(
        "header_up X-Marvis-Client-IP {http.request.remote.host}"
    ) == 4


def test_dev_preset_resolves_user_when_environment_variable_is_absent() -> None:
    environment = os.environ.copy()
    environment.pop("USER", None)
    environment["MARVIS_PRESET"] = "dev-local"
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source core/scripts/lib/setup-server/preset.sh; "
            "apply_preset; "
            'test "$MARVIS_DEPLOY_USER" = "$(id -un)"',
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_login_uses_the_shared_trusted_peer_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_resolver(*, peer_ip, forwarded_ip, trusted_proxy_cidrs):
        observed.update(
            peer_ip=peer_ip,
            forwarded_ip=forwarded_ip,
            trusted_proxy_cidrs=trusted_proxy_cidrs,
        )
        return "198.51.100.77"

    monkeypatch.setattr(auth_router, "resolve_client_ip", fake_resolver)
    monkeypatch.setattr(
        auth_router,
        "_check_rate_limit",
        lambda client_ip: observed.update(rate_limit_ip=client_ip),
    )

    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "CREATE TABLE users (id TEXT, slug TEXT, email TEXT, "
            "password_hash TEXT, workspace_id TEXT, type TEXT, deleted_at TEXT)"
        )
        with pytest.raises(HTTPException) as exc:
            await auth_router.login(
                auth_router.LoginRequest(
                    email="missing@example.com", password="bad-password"
                ),
                _request(
                    (b"cf-connecting-ip", b"198.51.100.42"),
                    (b"x-marvis-client-ip", b"192.0.2.99"),
                    peer="127.0.0.1",
                ),
                db,
            )

    assert exc.value.status_code == 401
    assert observed == {
        "peer_ip": "127.0.0.1",
        "forwarded_ip": "198.51.100.42",
        "trusted_proxy_cidrs": auth_router.settings.trusted_proxy_cidrs,
        "rate_limit_ip": "198.51.100.77",
    }


@pytest.mark.asyncio
async def test_duplicate_email_across_workspaces_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_router, "_check_rate_limit", lambda _client_ip: None)
    password_hash = bcrypt.hashpw(
        b"CorrectHorseBatteryStaple!", bcrypt.gensalt(rounds=4)
    ).decode()
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "CREATE TABLE users (id TEXT, slug TEXT, email TEXT, "
            "password_hash TEXT, workspace_id TEXT, type TEXT, deleted_at TEXT)"
        )
        await db.executemany(
            "INSERT INTO users VALUES (?, ?, ?, ?, ?, 'human', NULL)",
            [
                ("user-a", "user-a", "shared@example.com", password_hash, "ws-a"),
                ("user-b", "user-b", "shared@example.com", password_hash, "ws-b"),
            ],
        )
        await db.commit()
        with pytest.raises(HTTPException) as exc:
            await auth_router.login(
                auth_router.LoginRequest(
                    email="shared@example.com",
                    password="CorrectHorseBatteryStaple!",
                ),
                _request(),
                db,
            )

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_first_login_reset_token_is_bound_to_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        auth_router.settings,
        "force_password_change_on_first_login",
        True,
    )
    monkeypatch.setattr(auth_router, "_check_rate_limit", lambda _client_ip: None)
    password_hash = bcrypt.hashpw(
        b"OldFirstLogin123!", bcrypt.gensalt(rounds=4)
    ).decode()

    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "CREATE TABLE users (id TEXT, slug TEXT, email TEXT, password_hash TEXT, "
            "workspace_id TEXT, type TEXT, deleted_at TEXT, "
            "password_must_change INTEGER, updated_at TEXT)"
        )
        await db.execute(
            "INSERT INTO users VALUES (?, ?, ?, ?, ?, 'human', NULL, 1, NULL)",
            (
                "first-login-user",
                "first-login",
                "first-login@example.com",
                password_hash,
                "ws-first-login",
            ),
        )
        await db.commit()
        response = await auth_router.login(
            auth_router.LoginRequest(
                email="first-login@example.com",
                password="OldFirstLogin123!",
            ),
            _request(),
            db,
        )

    assert response.status_code == 403
    payload = json.loads(response.body)
    token_data = auth_router._get_reset_serializer().loads(payload["reset_token"])
    assert token_data == {
        "user_id": "first-login-user",
        "slug": "first-login",
        "workspace_id": "ws-first-login",
    }


@pytest.mark.asyncio
async def test_oauth_slug_collision_uses_workspace_principal_fallback() -> None:
    async with aiosqlite.connect(":memory:") as db:
        await db.execute(
            "CREATE TABLE users (id TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL)"
        )
        await db.execute("INSERT INTO users VALUES ('existing', 'sam')")
        await db.commit()

        slug = await _adapter._available_oauth_slug(
            db,
            email="sam@example.com",
            user_id="oauth-user",
            workspace_id="ws-default",
        )

    expected = "workos-" + hashlib.sha256(
        b"ws-default\0oauth-user"
    ).hexdigest()[:16]
    assert slug == expected


@pytest.mark.asyncio
async def test_oauth_fallback_collision_fails_closed() -> None:
    fallback = "workos-" + hashlib.sha256(
        b"ws-default\0oauth-user"
    ).hexdigest()[:16]
    async with aiosqlite.connect(":memory:") as db:
        await db.execute(
            "CREATE TABLE users (id TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL)"
        )
        await db.executemany(
            "INSERT INTO users VALUES (?, ?)",
            [("readable-owner", "sam"), ("fallback-owner", fallback)],
        )
        await db.commit()

        with pytest.raises(RuntimeError, match="collides"):
            await _adapter._available_oauth_slug(
                db,
                email="sam@example.com",
                user_id="oauth-user",
                workspace_id="ws-default",
            )
