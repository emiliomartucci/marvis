# OAuth tenant-scoped isolation verifiers (WorkOS org_id / Entra tid).
"""Custom fastmcp JWTVerifiers: gate the token on a configurable tenant claim.

``TenantScopedJWTVerifier`` generalizes the org-based gate (plan
2026-07-01-feat-oauth-org-based-tenant-isolation-plan.md) for the enterprise
OIDC profile (plan 2026-07-06 dockerization, IMPL §A.3): the tenant claim is
configurable — ``org_id`` for WorkOS AuthKit, ``tid`` for Microsoft Entra.
On top of the parent's signature/JWKS/issuer/exp validation it enforces an
optional ``nbf`` claim with bounded clock leeway, then rejects any token whose
tenant claim != this tenant's expected value, fail-closed.

For Entra (``tid``) it additionally enforces the chain of trust: the issuer
must embed the same tenant GUID (``https://login.microsoftonline.com/{tid}/v2.0``)
— a token whose ``tid`` matches but whose ``iss`` names another tenant is forged
or misconfigured, never valid.

``OrgScopedJWTVerifier`` stays as a thin alias for >= 1 release: the rename
touches the live auth path on the whole hosted fleet (pattern 7fdbcce5).
"""
from __future__ import annotations

import math
import time

from fastmcp.server.auth.providers.jwt import JWTVerifier


NBF_LEEWAY_SECONDS = 60


class TenantScopedJWTVerifier(JWTVerifier):
    """JWTVerifier + fail-closed tenant-claim gate (org_id WorkOS / tid Entra)."""

    def __init__(self, *, tenant_claim: str, expected_value: str, **kwargs) -> None:
        # Pass-through: do NOT re-list the parent's kwargs (immune to fastmcp renames).
        super().__init__(**kwargs)
        self._tenant_claim = tenant_claim
        self._expected_value = expected_value

    async def verify_token(self, token: str):
        # Parent validates signature/JWKS/issuer/exp; returns AccessToken | None
        # (never raises on invalid). A try/except here could only ``return None``,
        # never ``return token`` (that would bypass crypto validation).
        access = await super().verify_token(token)
        if access is None:
            return None
        claims = getattr(access, "claims", None) or {}
        not_before = claims.get("nbf")
        if not_before is not None:
            # fastmcp 3.4.2 verifies signature/issuer/audience/expiry but does
            # not reject future nbf values. Bind the shared seam itself so OSS,
            # Hosted, and Enterprise cannot disagree about token activation.
            if (
                isinstance(not_before, bool)
                or not isinstance(not_before, (int, float))
                or not math.isfinite(float(not_before))
                or float(not_before) > time.time() + NBF_LEEWAY_SECONDS
            ):
                return None
        value = claims.get(self._tenant_claim)
        # Fail-closed: None / "" / wrong type / mismatch → reject. Never
        # ``if value and value != expected`` (that bypasses on an absent claim).
        # No .lower()/.strip() on the claim (exact match; env is stripped once).
        if not isinstance(value, str) or value != self._expected_value:
            return None
        if self._tenant_claim == "tid":
            # Entra chain of trust: iss must carry the SAME tenant GUID.
            iss = claims.get("iss", "")
            if not isinstance(iss, str) or f"/{value}/" not in iss:
                return None
        return access


class OrgScopedJWTVerifier(TenantScopedJWTVerifier):
    """Thin WorkOS alias (tenant claim = ``org_id``). Use with ``audience=None`` (no RI)."""

    def __init__(self, *, expected_org: str, **kwargs) -> None:
        super().__init__(tenant_claim="org_id", expected_value=expected_org, **kwargs)
        self.expected_org = expected_org
