"""OIDC login (item G).

Minimal Authorization-Code flow against any standards-compliant
provider (Auth0 / Keycloak / Cognito / Google Workspace / Okta).
Scope is intentionally tight:

- Discovery: read issuer's ``.well-known/openid-configuration``
  lazily on first request; cache in-process.
- /auth/oidc/login: stash state + nonce on the session cookie,
  redirect to the provider's authorize endpoint.
- /auth/oidc/callback: exchange code for tokens, verify id_token
  signature + claims, resolve the user (create or match by email),
  set the session operator.

Not in scope today (future work):
- Refresh tokens + silent renewal — on expiry, user just re-logs-in.
- PKCE — add if the provider doesn't support client_secret auth.
- Role mapping from id_token claims — enrichment hook placeholder.

Env configuration:
  MCP_OIDC_ISSUER_URL      provider root URL (e.g. https://example.auth0.com)
  MCP_OIDC_CLIENT_ID       client id issued by the provider
  MCP_OIDC_CLIENT_SECRET   client secret (or use PKCE — future)
  MCP_OIDC_REDIRECT_URI    the absolute URL of /auth/oidc/callback on this deployment
  MCP_OIDC_SCOPES          default "openid email profile"

`is_enabled()` returns True only when all four required values are
set. Callers (login template + route registration) gate on this so
deployments without OIDC show only the password form.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from jose import jwt

from mcp_server.logging_config import get_logger

_log = get_logger("mcp.oidc")


@dataclass(frozen=True)
class OIDCConfig:
    issuer_url: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str

    @classmethod
    def from_env(cls) -> "OIDCConfig | None":
        issuer = os.environ.get("MCP_OIDC_ISSUER_URL", "").strip()
        client_id = os.environ.get("MCP_OIDC_CLIENT_ID", "").strip()
        client_secret = os.environ.get("MCP_OIDC_CLIENT_SECRET", "").strip()
        redirect_uri = os.environ.get("MCP_OIDC_REDIRECT_URI", "").strip()
        if not (issuer and client_id and client_secret and redirect_uri):
            return None
        return cls(
            issuer_url=issuer.rstrip("/"),
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=os.environ.get("MCP_OIDC_SCOPES", "openid email profile"),
        )


def is_enabled() -> bool:
    return OIDCConfig.from_env() is not None


# ---------------------------------------------------------------------------
# Discovery + JWKS cache (simple in-process TTL).
# ---------------------------------------------------------------------------

_discovery_cache: dict[str, dict[str, Any]] = {}
_jwks_cache: dict[str, dict[str, Any]] = {}


def _discovery(cfg: OIDCConfig, client: httpx.Client | None = None) -> dict[str, Any]:
    if cfg.issuer_url in _discovery_cache:
        return _discovery_cache[cfg.issuer_url]
    url = cfg.issuer_url + "/.well-known/openid-configuration"
    http = client or httpx.Client(timeout=10)
    try:
        r = http.get(url)
        r.raise_for_status()
        data = r.json()
    finally:
        if client is None:
            http.close()
    _discovery_cache[cfg.issuer_url] = data
    return data


def _jwks(cfg: OIDCConfig, *, jwks_uri: str,
          client: httpx.Client | None = None) -> dict[str, Any]:
    if jwks_uri in _jwks_cache:
        return _jwks_cache[jwks_uri]
    http = client or httpx.Client(timeout=10)
    try:
        r = http.get(jwks_uri)
        r.raise_for_status()
        data = r.json()
    finally:
        if client is None:
            http.close()
    _jwks_cache[jwks_uri] = data
    return data


def clear_caches() -> None:
    """Test / operational hook — force refetch of discovery + JWKS."""
    _discovery_cache.clear()
    _jwks_cache.clear()


# ---------------------------------------------------------------------------
# Authorization URL + token exchange
# ---------------------------------------------------------------------------

def build_login_url(cfg: OIDCConfig, *, state: str, nonce: str) -> str:
    disc = _discovery(cfg)
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": cfg.scopes,
        "state": state,
        "nonce": nonce,
    }
    return f"{disc['authorization_endpoint']}?{urlencode(params)}"


class OIDCError(Exception):
    """Callback failed validation."""


def exchange_and_verify(
    cfg: OIDCConfig,
    *,
    code: str,
    nonce: str,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Exchange an auth code for tokens and verify the id_token.

    Returns the decoded id_token claims on success (contains at
    least `email` and `sub`). Raises :class:`OIDCError` when
    anything goes wrong — the caller is expected to convert it to a
    login-page flash + redirect.
    """
    disc = _discovery(cfg, http_client)
    http = http_client or httpx.Client(timeout=10)
    try:
        resp = http.post(
            disc["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": cfg.redirect_uri,
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
            },
        )
    finally:
        if http_client is None:
            http.close()
    if resp.status_code != 200:
        raise OIDCError(
            f"token exchange failed: {resp.status_code} {resp.text[:200]}"
        )
    payload = resp.json()
    id_token = payload.get("id_token")
    if not id_token:
        raise OIDCError("provider did not return an id_token")

    # Verify.
    try:
        headers = jwt.get_unverified_header(id_token)
    except jwt.JWTError as exc:
        raise OIDCError(f"malformed id_token: {exc}")
    jwks = _jwks(cfg, jwks_uri=disc["jwks_uri"], client=http_client)
    kid = headers.get("kid")
    key = None
    for k in jwks.get("keys", []):
        if k.get("kid") == kid:
            key = k
            break
    if key is None:
        raise OIDCError(f"id_token kid={kid!r} not found in JWKS")

    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=[headers.get("alg", "RS256")],
            audience=cfg.client_id,
            issuer=disc.get("issuer"),
            options={"verify_at_hash": False},
        )
    except jwt.JWTError as exc:
        raise OIDCError(f"id_token verification failed: {exc}")

    if claims.get("nonce") != nonce:
        raise OIDCError("nonce mismatch")
    if not claims.get("email"):
        raise OIDCError("id_token missing 'email' claim")

    return claims


# ---------------------------------------------------------------------------
# Session helpers for the /auth/oidc/login + /callback routes
# ---------------------------------------------------------------------------

_SESSION_KEY_STATE = "_oidc_state"
_SESSION_KEY_NONCE = "_oidc_nonce"


def stash_state(request, state: str, nonce: str) -> None:
    request.session[_SESSION_KEY_STATE] = state
    request.session[_SESSION_KEY_NONCE] = nonce


def pop_state(request) -> tuple[str | None, str | None]:
    return (
        request.session.pop(_SESSION_KEY_STATE, None),
        request.session.pop(_SESSION_KEY_NONCE, None),
    )


def fresh_state() -> tuple[str, str]:
    return secrets.token_urlsafe(24), secrets.token_urlsafe(24)
