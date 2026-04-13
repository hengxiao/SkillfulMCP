# Visibility, Accounts, and Token Issuance UX

Design notes for the four features added in Wave 8:

1. **Public vs private** flag on skills + skillsets.
2. **Admin user account** with full privileges (real user, not the
   shared admin key).
3. **Web UI for operator management** — DB-backed users + CRUD pages.
4. **Interactive JWT issuance** — operator picks an agent, narrows
   grants, sets expiry, gets a copyable token.

Sequenced as three independent sub-waves so each can ship and stabilize
on its own:

- **Wave 8a** — visibility (smallest, most contained).
- **Wave 8b** — users + roles (DB schema + bootstrap + CRUD UI).
- **Wave 8c** — token issuance UI (depends on 8b's role system).

---

## 1. Visibility model

### Goals

- Catalog admins can mark a skill or skillset as **public**.
- Public means: any authenticated agent (regardless of grant set) can
  list it, fetch its metadata, and download its bundle files.
- Default for existing rows is **private** — backwards compatible.

### Data model

Add `visibility` column to `skills` and `skillsets`:

```python
visibility: Mapped[str] = mapped_column(String, nullable=False, server_default="private")
```

Allowed values (enforced at the schema layer): `"public"` or `"private"`.
A migration adds the column with default `"private"`.

### Authorization rule update

`authorization.resolve_allowed_skill_ids(claims, db)` returns the union
of:
1. `claims.skills` (explicit grants).
2. Skill ids in any skillset listed in `claims.skillsets`.
3. **(new)** All skill ids of skills with `visibility="public"`.
4. **(new)** All skill ids in skillsets with `visibility="public"`.

The auth model stays additive — no deny path. Agents still need a valid
JWT (we don't open up unauthenticated reads here; that's a future flag).

### Edge cases

- **Public skill in a private skillset**: skill is reachable individually.
  The private skillset's listing won't surface it via the skillset path
  for unauthorized agents, but the skill itself is visible.
- **Private skill in a public skillset**: confusing if mixed. We resolve
  by **the skillset's flag**: a public skillset exposes ALL its members
  to any authenticated agent, regardless of each member's own flag.
  Operators wanting per-skill control should keep the skillset private.
- **Bundle visibility follows skill visibility**. Public skill → public
  bundle (any authenticated agent can `GET /skills/{id}/versions/{v}/files/{path}`).

### Web UI

- Badge on each row in skills + skillsets lists: `Public` (green) or
  `Private` (gray).
- Read-only display on the skill view page next to the version pill.
- Toggle on the new-version + clone forms (visibility per-version was
  considered and rejected — keeping it per-skill matches the
  immutable-version mental model better).

### API surface

- `SkillCreate`, `SkillUpsertBody`, `SkillResponse` gain a `visibility`
  field defaulting to `"private"`.
- `SkillsetCreate`, `SkillsetResponse` gain the same.
- Existing clients that don't send the field continue to work (default
  is private).

---

## 2. Accounts and roles

### Current state

- `MCP_ADMIN_KEY` — shared static catalog credential. Inter-service
  auth between Web UI and catalog. Not changed in Wave 8.
- `MCP_WEBUI_OPERATORS` — JSON list, no roles. Bootstrap-only.

### Target

Two roles in Wave 8b (intermediate; productization §3.1 will add more):

- **`admin`** — full privileges. Can manage users, manage all
  skills/skillsets/agents, mint tokens for any agent.
- **`viewer`** — read-only across all UI pages. No mutating actions
  rendered.

A future wave adds `editor` (manage catalog content, can't manage users)
when the operator org gets large enough to need the split.

### Schema (Wave 8b)

```sql
CREATE TABLE users (
    id              TEXT PRIMARY KEY,         -- uuid4
    email           TEXT NOT NULL UNIQUE,
    display_name    TEXT,
    password_hash   TEXT NOT NULL,            -- bcrypt
    role            TEXT NOT NULL,            -- 'admin' | 'viewer'
    disabled        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    last_login_at   TIMESTAMPTZ
);
```

Email is unique, normalized to lowercase. `disabled` lets an admin lock
an account without deleting it (preserves audit history).

### Bootstrap — env-to-DB migration path

On startup (lifespan), if `MCP_WEBUI_OPERATORS` is non-empty AND the
`users` table is empty, every entry in the env JSON is upserted as an
`admin` user. After that, all user management goes through the Web UI;
the env is ignored unless the table is wiped.

This lets existing deployments rotate to the DB-backed model with zero
manual work. Once a real admin exists, removing `MCP_WEBUI_OPERATORS`
from env is the recommended cleanup.

### Web UI pages

- **`/users`** (admin only) — list. Email, name, role badge, last
  login, disabled badge, edit + delete buttons.
- **`/users/new`** (admin) — create. Email, name, role, initial password.
- **`/users/{id}`** (admin) — edit. Same fields. Cannot edit own role
  (anti-lockout). Cannot delete own account.
- **`/account`** (any logged-in user) — change own password.

Self-service password change is in scope; full forgot-password / email
flow is not.

### Auth flow updates

- Login looks up the `users` table first. Falls back to env-only
  operators only when the DB has no rows (transition path).
- Session stores `{user_id, email, role}`.
- New FastAPI dep: `require_role(role)` for route guards.
- Existing `Operator` shape grows a `role` field (`"admin"` / `"viewer"`).

### Mid-flight session invalidation

- Disabling a user invalidates their session on next request (auth
  middleware checks the DB row's `disabled` flag).
- Role changes take effect on the next page load (session role is
  refreshed against the DB on every request — there's no JWT to revoke).

---

## 3. Interactive JWT issuance UI

Wave 8c. Depends on the user account model so we can audit who minted
each token.

### Pages

- **`/agents/{agent_id}/tokens`** — list active issued tokens for an
  agent. Shows jti (truncated), expires-at, issued-by, revoke button.
  Empty state explains: "Token bytes are never stored — only the jti."
- **`/agents/{agent_id}/tokens/new`** — wizard:
  1. Show the agent's grants (skillsets + skills) read-only.
  2. **Narrowing** (optional): unchecks any grant the issuer wants to
     omit from this specific token. Token claims are the intersection of
     agent grants and the chosen subset.
  3. `expires_in` — slider with snap points: 1h, 8h, 1d, 7d. Capped at
     `MCP_MAX_TOKEN_LIFETIME_SECONDS`.
  4. Submit → mint → show the token in a **one-time view** with a copy
     button. Leaving the page makes the token bytes unrecoverable
     (only the jti survives, in the catalog's listing).

### Backend support

- Optional `issued_tokens` table:

  ```sql
  CREATE TABLE issued_tokens (
      jti              TEXT PRIMARY KEY,
      agent_id         TEXT NOT NULL,
      issued_by_user_id TEXT REFERENCES users(id),
      narrowed_skills      JSON,   -- list[str], NULL = full agent grants
      narrowed_skillsets   JSON,
      expires_at       TIMESTAMPTZ NOT NULL,
      issued_at        TIMESTAMPTZ NOT NULL,
      revoked_at       TIMESTAMPTZ
  );
  ```

  This is the listing index, not the truth. Catalog auth still relies on
  the JWT signature + `revocation` deny-list — losing the index doesn't
  invalidate any tokens.

- `POST /token` extended with optional `narrowed_skills` /
  `narrowed_skillsets` arrays. The minted JWT's claims are
  `intersection(agent_grants, narrowed)`. If the narrowing is a superset
  of agent grants, the request is rejected with 400 (operators can only
  narrow, not expand).

### UX

- One-time view: the token bytes appear in a copy-to-clipboard textbox
  styled distinctively. After leaving the page, only the jti is visible
  in the listing.
- Audit log row on every issue (productization §3.1 P2). Wave 8c emits
  a structured log line; the dedicated audit-log table is its own future
  wave.

---

## Migrations

Three Alembic revisions, one per sub-wave:

| Revision | Adds |
| -------- | ---- |
| `0002_visibility` | `skills.visibility`, `skillsets.visibility` (default `'private'`) |
| `0003_users` | `users` table |
| `0004_issued_tokens` | `issued_tokens` table (optional; deployments that don't use the listing UI can skip) |

Each is additive and reversible. The `test_migrations.py` parity test
catches drift against `Base.metadata`.

---

## Sequencing

### Wave 8a (visibility) — **shipped**

Lowest dependency. Shipped first. Existing tokens, the existing Web UI,
and the existing test suite all still work without operator action —
the new field defaults to `'private'`.

### Wave 8b (users + roles) — **shipped**

Landed in commit after 8a. Ships:

- `users` table + `0003_users` migration.
- `mcp_server/users.py` CRUD + `bootstrap_from_env` seeder (run in the
  app lifespan so existing `MCP_WEBUI_OPERATORS` deployments migrate
  on first boot).
- `/admin/users/*` admin-gated CRUD + `/admin/users/authenticate` so
  the Web UI never ships password hashes over the wire.
- `mcp_server.pwhash` — shared bcrypt helper used by the server-side
  auth endpoint and the Web UI (resolves the webui→mcp_server
  dependency direction).
- Web UI login now prefers DB auth, falls back to env operators only
  when the server is unreachable or the env lookup is the only match.
- Session now carries `{email, role, user_id}`; role drives
  `require_role("admin")` dep used on `/users/*` routes.
- Templates: `users.html`, `user_new.html`, `user_detail.html`,
  `account.html` (self-service password change for DB-backed accounts).
- Sidebar hides the Users link from viewers.
- Last-admin deletion guard: refuses to 204 when it would strand the UI.
- Tests: `tests/test_users.py` (service + HTTP layers) and
  `tests/test_webui_users.py` (page rendering + role gating).

### Wave 8c (token issuance UI)

Layers on top of the user identity from 8b. Requires the optional
`issued_tokens` table for the listing UI. The narrowing endpoint is a
small extension to existing `POST /token`.

---

## Out of scope (call out for clarity)

- **Forgot-password** flow with email reset. Defer until we have email
  delivery infrastructure.
- **MFA / WebAuthn** for admins. Worth doing eventually; scope it then.
- **Operator audit log table** (productization §3.1 P2) — Wave 8 emits
  structured log lines for revoke / user create / token issue. The
  queryable audit table is a separate wave.
- **Tenant-scoped roles** (`tenant_admin`, `catalog_editor`,
  `read_only`) from productization §3.1 — those land when tenants land.
- **Anonymous public reads** (`MCP_PUBLIC_READS_NO_AUTH=1`) — public
  still requires a valid JWT in Wave 8a. If a use case appears we can
  add the toggle later.
