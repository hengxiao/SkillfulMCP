# mcp_server/routers/token.py

JWT minting.

## `POST /token`

**Auth**: `X-Admin-Key` (admin-gated — this is the most privileged endpoint in the system).

**Request body** (`schemas.TokenRequest`):
```json
{ "agent_id": "billing-agent", "expires_in": 3600 }
```

**Behavior**:
1. `registry.get_agent(agent_id)`.
2. If not found → 404.
3. `auth.issue_token(agent, expires_in)`.
4. Return `{ "access_token": "...", "token_type": "bearer", "expires_in": 3600 }`.

**Notes**:
- `expires_in` is honored as-is (no server-side cap). Productization §3.1 clamps this.
- No throttling on this endpoint. Anyone with the admin key can mint tokens at any rate.

## Testing

`tests/test_api_token.py` — covers:
- Successful mint → response contains a decodable JWT with expected claims.
- Missing admin key → 403.
- Unknown agent → 404.

## Threat notes

- A compromised `MCP_ADMIN_KEY` = ability to mint a token for any agent = full
  skill access across the catalog. Treat as root credential; rotate via
  env change + restart. All previously-issued tokens keep working until
  their own `exp` (no revocation in prototype).
