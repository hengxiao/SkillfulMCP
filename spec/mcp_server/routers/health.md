# mcp_server/routers/health.py

Liveness probe.

## `GET /health` → `{"status": "ok"}`

Unauthenticated. Always returns 200 as long as the ASGI worker is up.

## What it does **not** check

- Database connectivity.
- JWT secret presence (enforced at startup instead).
- Bundle store reachability.

## Future work

Split into `/livez` (process alive) and `/readyz` (DB reachable, JWT secret
loaded, bundle store reachable). See `../../productization.md` §3.6.
