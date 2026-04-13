# mcp_server/auth.py

JWT issuance and validation. Built on `python-jose`, backed by
`KeyRing` + `RevocationList`.

## `TokenService`

The primary class. One per process (lazily constructed via
`get_default_service()`).

```python
TokenService(
    keyring: KeyRing,
    revocation: RevocationList,
    *,
    issuer: str,
    max_lifetime_seconds: int,
)
```

### `issue_token(agent, expires_in=3600) -> str`

Claims layout:

```python
{
    "sub":       agent.id,
    "iss":       self.issuer,
    "iat":       int(now.timestamp()),
    "exp":       int((now + timedelta(seconds=capped)).timestamp()),
    "jti":       "<uuid4-hex>",
    "skillsets": agent.skillsets or [],
    "skills":    agent.skills or [],
    "scope":     agent.scope or [],
}
```

`expires_in` is clamped to `[1, max_lifetime_seconds]`. A clamp that
changed the value is logged at INFO level so ops can spot clients that
ask for longer than policy allows. `jti` is a fresh UUID4 per call.

Signed with `keyring.active_secret`, algorithm `keyring.algorithm`;
the JWT header carries `kid: keyring.active_kid` so verifiers can pick
the right secret during rotation.

### `validate_token(token) -> dict`

1. Read `kid` from the unverified header. Unknown kid → 401
   `"Unknown signing key kid=…"`.
2. Verify signature + `exp` + issuer match. Failures → 401.
3. Check `jti` against the revocation list. Revoked → 401 `"Token has
   been revoked"`.
4. Return decoded claims.

Malformed tokens (not a real JWT) raise 401 at step 1.

## Module-level shims — backwards compatibility

```python
issue_token(agent, expires_in=3600) -> str
validate_token(token) -> dict
```

Thin wrappers that delegate to `get_default_service()`. `tests/test_auth.py`
and the legacy public API use these; nothing else calls them.

### `get_default_service() -> TokenService`

Returns the process-wide default. Built lazily on first call from
`get_settings()`:
- `build_keyring(settings)` for the ring.
- `RevocationList()` for the deny list.
- `issuer = settings.jwt_issuer`
- `max_lifetime_seconds = settings.max_token_lifetime_seconds`

### `reset_default_service()`

Clears the cached singleton. Called by the `_reset_auth_singleton` autouse
fixture in `conftest.py` so revocation state doesn't leak between tests.

## Token lifecycle

1. Operator creates an `Agent` via admin API.
2. Operator mints a token: `POST /token` with `{agent_id, expires_in}`.
   `expires_in` clamped server-side.
3. Token carries `kid` (header) + `jti` (claim).
4. Every protected endpoint runs through `validate_token` → key lookup →
   signature check → revocation check → claims.
5. Token expires on `exp`, or is revoked early via
   `POST /admin/tokens/revoke {jti}`.

## Threat model

- **Secret theft in legacy mode** → full impersonation until
  `MCP_JWT_SECRET` is rotated. All tokens become invalid on rotation.
- **Secret theft in multi-key mode** → rotate the affected `kid`
  (remove from `MCP_JWT_KEYS`, deploy). Tokens signed with the stolen
  kid stop verifying; tokens signed with any other kid keep working.
- **Token theft** → revoke the `jti`. `POST /admin/tokens/revoke`. Takes
  effect on the next request (no TTL wait).
- **Algorithm confusion** → mitigated by `algorithms=[keyring.algorithm]`
  on decode; no "alg=none" or RS→HS confusion path.

## Testing

- `tests/test_auth.py` (existing, 13 tests) — issuance/validation round-
  trip, expiry, tamper, issuer mismatch.
- `tests/test_keyring_revocation.py` (new, 21 tests) — keyring modes,
  revocation list, jti presence, kid routing (including rotation where
  an old-kid token still verifies), `expires_in` clamp, admin revoke
  endpoint.

## Future work

- **RS256 + KMS-backed keys** — asymmetric signing so verifiers can hold
  public keys without the signer's secret.
- **`aud` claim** — scope tokens to specific audiences (services).
- **Refresh tokens** — short `access_token` + long-lived `refresh_token`
  with dedicated endpoint.
- **Agent-level "revoke all tokens"** — `not_before` epoch on the agent
  row; validate reject if `iat < not_before`.
- **Stricter `scope` enforcement** — today `scope` is stored but not
  checked at validation. Bundle download should require `execute`; metadata
  reads get `read`.
