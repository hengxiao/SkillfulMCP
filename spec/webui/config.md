# webui/config.py

Environment-driven settings for the Web UI process. Shares the same `.env`
as the catalog server.

## `Settings`

| Attribute       | Env var           | Default                   |
| --------------- | ----------------- | ------------------------- |
| `mcp_server_url`| `MCP_SERVER_URL`  | `http://localhost:8000`   |
| `admin_key`     | `MCP_ADMIN_KEY`   | `""`                      |
| `host`          | `WEBUI_HOST`      | `127.0.0.1`               |
| `port`          | `WEBUI_PORT`      | `8080`                    |

`get_settings()` is `@lru_cache`d so tests that mutate env vars between
cases must reset the cache.

## Design notes

- **Shared admin key**. The Web UI is effectively the admin console; every
  request it makes carries `X-Admin-Key`. That collapses auth — anyone who
  can reach the Web UI has full catalog power. Productization §3.5 replaces
  this with an OIDC operator session.
- **No TLS / CSRF config**. The UI is assumed to be run locally or behind
  a trusted reverse proxy. Both are flagged as productization gaps.

## Future work

- Switch to `pydantic-settings` + validated types.
- Surface `mcp_server_url` mismatch at startup (ping `/health` before
  serving).
- Add `WEBUI_SESSION_SECRET` for cookie-based operator sessions.
