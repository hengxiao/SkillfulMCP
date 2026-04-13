# mcp_server/config.py

Environment-driven configuration.

## `Settings`

Plain class (not pydantic-settings in the prototype — kept explicit so the
failure mode on a missing `MCP_JWT_SECRET` is obvious).

| Attribute         | Env var              | Default                            | Required |
| ----------------- | -------------------- | ---------------------------------- | -------- |
| `jwt_secret`      | `MCP_JWT_SECRET`     | —                                  | Yes (raises at init if empty) |
| `jwt_issuer`      | `MCP_JWT_ISSUER`     | `"mcp-server"`                     |          |
| `jwt_algorithm`   | `MCP_JWT_ALGORITHM`  | `"HS256"`                          |          |
| `admin_key`       | `MCP_ADMIN_KEY`      | `""`                               | Used by `require_admin`; empty value disables the admin-key check, which is fine for local dev only |
| `database_url`    | `MCP_DATABASE_URL`   | `"sqlite:///./skillful_mcp.db"`    |          |
| `rate_limit_per_minute` | `MCP_RATE_LIMIT_PER_MINUTE` | `600`                    | Per-IP requests-per-minute. `0` disables the limiter. Tests set it to `0`. |
| `max_request_body_mb`   | `MCP_MAX_REQUEST_BODY_MB`   | `101`                    | App-level body size cap. Sits above the bundle endpoint's own 100 MB check. |

## `get_settings() -> Settings`

`@lru_cache(maxsize=1)` wrapper. Settings are read once per process. Tests
that need to change env vars must either reset the cache or set the env
before the first import of `mcp_server.*`.

## Invariants the prototype enforces

- **Missing JWT secret fails fast.** The constructor raises `RuntimeError`
  with a clear message pointing the user at `.env.example`.
- **Algorithm is HS256 only.** The prototype has no key ring or RS256 support.
  Productization plan §3.1 covers the upgrade path.

## Interactions

- `auth.issue_token` / `auth.validate_token` read `jwt_secret`, `jwt_issuer`,
  `jwt_algorithm`.
- `dependencies.require_admin` reads `admin_key`.
- `main.create_app` → `database.init_db` reads `database_url` (unless
  overridden by a test).

## Testing

`tests/conftest.py` sets the three env vars before the `mcp_server` import:

```python
os.environ.setdefault("MCP_JWT_SECRET", "test-secret-key-for-testing-only")
os.environ.setdefault("MCP_ADMIN_KEY", "test-admin-key")
os.environ.setdefault("MCP_DATABASE_URL", "sqlite:///:memory:")
```

Because of the `lru_cache`, changing these at runtime in a single test process
won't take effect unless `get_settings.cache_clear()` is called.

## Future work

- Switch to `pydantic-settings` for typed validation and layered sources
  (env → file → secrets manager).
- Add rotation support (multiple `jwt_secret`s keyed by `kid`).
- Add admin-key requirement toggle per environment (fail closed in prod).
