# mcp_server/auth.py

JWT issuance and validation. Built on `python-jose`.

## `issue_token(agent: Agent, expires_in: int = 3600) -> str`

Claims layout:

```python
{
    "sub":       agent.id,
    "iss":       settings.jwt_issuer,          # default "mcp-server"
    "iat":       <now, unix seconds>,
    "exp":       <now + expires_in>,
    "skillsets": agent.skillsets or [],        # list[str]
    "skills":    agent.skills or [],           # list[str]
    "scope":     agent.scope or [],            # list[str], e.g. ["read", "execute"]
}
```

Signed with `settings.jwt_secret`, algorithm `settings.jwt_algorithm`
(default `HS256`).

**Deliberate omissions from the prototype**:
- No `jti` → no replay protection / revocation handle.
- No `aud` → every service that trusts this issuer accepts the token.
- No `kid` → no key rotation support.

Productization §3.1 lists these as P1 items.

## `validate_token(token: str) -> dict`

1. `jose.jwt.decode(token, secret, algorithms=[alg], options={"verify_exp": True})`.
2. If decode raises `JWTError` → HTTP 401 with `WWW-Authenticate: Bearer`.
3. If `claims["iss"] != settings.jwt_issuer` → HTTP 401.
4. Return claims dict.

Consumed by `dependencies.get_current_claims`.

## Token lifecycle

1. Operator registers an `Agent` with skill grants (via admin API).
2. Operator calls `POST /token` (admin-gated) to mint a JWT for that agent.
3. Agent stores the token and presents it as `Authorization: Bearer …` on
   every catalog read.
4. `GET /skills` filters by claims via `authorization.resolve_allowed_skill_ids`.
5. Token expires → agent must request a new one (no refresh endpoint in the
   prototype).

## Threat model (prototype-level)

- **Secret theft** → full impersonation. Secret lives in env; rotate by
  restart + new `MCP_JWT_SECRET`. All existing tokens invalidate.
- **Token theft** → agent impersonation until `exp`. No revocation.
- **Replay** → possible until `exp`; mitigate with short `expires_in`.
- **Algorithm confusion** → not specifically guarded; relies on
  `algorithms=[settings.jwt_algorithm]` restricting the decoder.

## Testing

`tests/test_auth.py` covers:
- round-trip issue → validate
- expired token rejection
- signature mismatch (wrong secret) → 401
- issuer mismatch → 401
- tampered payload → signature fail → 401

## Future work

- RS256 + key-ring with `kid` lookups.
- `jti` + Redis deny-list for revocation.
- Refresh-token flow.
- Bounded `expires_in` enforced server-side per-agent policy.
