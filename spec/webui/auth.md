# webui/auth.py + webui/middleware.py

Operator authentication, session management, and CSRF protection for the
Web UI. Shipped in Wave 6a.

## What this wave does

- **Password-based local operators.** Everyone who runs the Web UI
  configures a static list (`MCP_WEBUI_OPERATORS` JSON); each entry has
  an email and a bcrypt password hash. No database table yet.
- **Sessions in signed cookies** via Starlette's `SessionMiddleware`.
- **CSRF** on every state-changing endpoint via a FastAPI dependency.
- **Redirect to `/login`** on every unauthenticated request; `next=`
  preserved so users land where they were trying to go.

## What this wave does **not** do (Wave 6b)

- OIDC / SAML / external identity providers.
- Tenant-scoped roles.
- Operator CRUD UI or DB-backed operator store.
- Per-operator audit log.

Password flow exercises the full session + CSRF + auth-middleware
pipeline; swapping the credential source is a localized change later.

## What later waves added / plan to add

- **Wave 8b — shipped.** DB-backed operator store (`users` table),
  flat `admin` / `viewer` roles, CRUD UI at `/users`, self-service
  password change at `/account`. See
  [visibility-and-accounts.md](../visibility-and-accounts.md#wave-8b-users--roles--shipped).
- **Wave 9 — proposed.** Replaces the flat role set with an
  **account-based** tenant model: platform-level `superadmin` +
  per-account `account-admin` + `contributor` + `viewer`. Every
  non-superadmin user belongs to exactly one account; the
  account-admin is the only non-superadmin who manages users inside
  it. Adds email-based allow lists for cross-account sharing. See
  [user-management.md](../user-management.md).

## Operator config

Shipped shape — JSON in an env var:

```bash
MCP_WEBUI_OPERATORS='[
  {"email": "alice@example.com", "password_hash": "$2b$12$..."},
  {"email": "bob@example.com",   "password_hash": "$2b$12$..."}
]'
```

Generate a hash:

```bash
python -c "from webui.auth import hash_password; print(hash_password('mypassword'))"
```

Empty / missing `MCP_WEBUI_OPERATORS` means no operator can log in — the
app still starts, login always fails. Useful as a safe default when
rotating credentials.

## Module: `webui/auth.py`

### `Operator`

Frozen dataclass with `email`. Will grow `role`, `tenant_id`, `oidc_sub`
in Wave 6b.

### `hash_password(plain) -> str` / `verify_password(plain, hashed) -> bool`

Thin `bcrypt` wrappers. `bcrypt` (not `passlib`) — passlib 1.7 has a
documented compatibility issue with `bcrypt` 5.x.

Password input is truncated to 72 bytes deterministically before hashing /
verifying, matching bcrypt's own limit. Longer inputs aren't silently
accepted; users learn the cap when their 73rd char doesn't affect the
hash.

### `authenticate(email, password, settings=None) -> Operator | None`

Normalizes email to lower-case, looks up the bcrypt hash in the parsed
operator list, calls `verify_password`, returns `Operator | None`.
Never raises on bad input.

### Session accessors

```python
get_session_operator(request) -> Operator | None
set_session_operator(request, op)   # rotates the CSRF token
clear_session(request)
```

`set_session_operator` regenerates the CSRF token on login so a
pre-login token that leaked somehow can't be replayed after the user
authenticates.

### CSRF helpers

```python
get_csrf_token(request) -> str        # ensures a session-bound token exists
verify_csrf(request, submitted) -> bool   # constant-time compare
```

## Module: `webui/middleware.py`

### `AuthMiddleware`

On every request:
1. If `request.url.path` is in `_DEFAULT_AUTH_EXEMPT` (`/login`,
   `/logout`, `/favicon.ico`), pass through.
2. If there's a session operator, pass through.
3. Otherwise: 303 to `/login?next=<url-encoded target>`.

`next` includes the original query string so `?version=1.0.0` round-
trips correctly through the login flow.

### `csrf_required` — FastAPI dependency, NOT middleware

**Why a dep, not middleware**: `BaseHTTPMiddleware` reading the request
body prevents downstream `Form()` handlers from seeing it. FastAPI
dependencies run in the normal request cycle where `request.form()` is
cached properly. `dependencies=[Depends(csrf_required)]` on each
state-changing route keeps that body-sharing path intact.

Token lookup order inside the dep:
1. `X-CSRF-Token` header (HTMX global hook, direct `fetch` callers).
2. `csrf_token` form field (standard POST forms).

On mismatch: raises `HTTPException(403)` with
`headers={"X-Error-Code": "CSRF_FAILED"}`. The Web UI doesn't run the
catalog's typed-error envelope handler, so the body is FastAPI's
default `{"detail": "..."}`.

`settings.csrf_enabled` short-circuits the dep (used in the default
test env; dedicated CSRF tests spin up a separate app with it on).

## Wiring in `webui/main.py`

```python
# Middleware stack (last-added is outermost):
app.add_middleware(AuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
)

# CSRF shorthand:
CSRF = [Depends(csrf_required)]
# Every mutating endpoint carries it:
@app.post("/skills", dependencies=CSRF)
@app.delete("/skills/{skill_id}", dependencies=CSRF)
# etc.
```

The exceptions:
- `POST /login` runs its own CSRF check inline (so a mismatch can render
  a friendly "form expired" message instead of a 403 JSON).
- `POST /logout` carries CSRF — protection against hostile logouts via
  click-jacking.

## Templates

### `login.html`

Standalone (doesn't extend `base.html` — the sidebar requires an
operator, which we don't have pre-login). Form fields: email, password,
`csrf_token`, `next`. Renders an error line when present.

### `base.html` additions

- `<meta name="csrf-token" content="{{ csrf_token }}">` in `<head>` for
  HTMX's global header hook.
- Sidebar footer shows the current operator email + a POST form to
  `/logout` (with hidden CSRF field) styled as a "Sign out" button.
- HTMX `htmx:configRequest` listener attaches `X-CSRF-Token` on every
  non-GET HTMX request.

### All POST forms across templates

Every `<form method="post">` carries
`<input type="hidden" name="csrf_token" value="{{ csrf_token }}">`.
`_render` injects the token into every template context so the forms
don't need to pass it explicitly.

## Config

| Env var                       | Default                                            | Purpose                                                           |
| ----------------------------- | -------------------------------------------------- | ----------------------------------------------------------------- |
| `MCP_WEBUI_SESSION_SECRET`    | — (required at startup)                            | HMAC secret for the session cookie.                               |
| `MCP_WEBUI_OPERATORS`         | `""`                                               | JSON operator list.                                               |
| `MCP_WEBUI_CSRF_ENABLED`      | `"1"`                                              | `"0"`/`"false"`/`"no"` disables `csrf_required`. Tests set `"0"`. |

Rotating `MCP_WEBUI_SESSION_SECRET` invalidates every active session on
the next restart — fine for emergency logout-of-everyone, disruptive
otherwise.

## Testing

- `tests/test_webui.py` — 22 existing integration tests. The fixture now
  logs in the test operator before yielding the client; CSRF is
  disabled so tests don't need tokens. No shape changes to the tests.
- `tests/test_webui_auth.py` — 20 new tests:
  - Password hashing round-trip, malformed hash, 72-byte truncation.
  - Login with valid / bad / unknown credentials, case-insensitive
    email, `next=` handling (including open-redirect guard against
    external + protocol-relative URLs).
  - Unauth'd GET → 303 `/login?next=...`. `next` preserves query strings.
  - Logout clears the session cookie.
  - CSRF: POST without token → 403 + `X-Error-Code: CSRF_FAILED`; POST
    with valid token passes the gate; HTMX `X-CSRF-Token` header is
    accepted; GET isn't guarded.

## Future work (Wave 6b and beyond)

- **OIDC** — add `/auth/oidc/login` + `/auth/oidc/callback`; use `authlib`
  for the handshake; keep the session cookie shape unchanged so this
  becomes a new login entry point rather than a rewrite.
- **Roles — SHIPPED (Wave 8b) / EXTENDED (Wave 9 proposal).** The
  flat `admin` / `viewer` split shipped in Wave 8b; `require_role` is
  live. Wave 9 turns the role set into an account-based tenant model
  with `superadmin` (platform, singleton) + `account-admin`
  (per-account) + `contributor` + `viewer`. See
  [user-management.md](../user-management.md).
- **Tenant isolation — proposed (Wave 9).** Wave 9's `accounts`
  table is the tenant boundary; every non-superadmin user carries an
  `account_id` on their session.
- **DB-backed operator store — SHIPPED (Wave 8b).** Env JSON is now
  bootstrap-only; the `users` table is the source of truth.
- **Rate-limit `/login`** — currently unguarded; the catalog's
  `RateLimitMiddleware` only protects the catalog process. Add per-IP
  login throttling here.
- **MFA / WebAuthn** — operator step-up for destructive actions.
- **Per-operator audit log** — see productization §3.1 P2.
