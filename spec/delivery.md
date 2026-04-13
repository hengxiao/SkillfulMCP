# SkillfulMCP — Delivery Overview

A rolling snapshot of what has actually shipped, organized by wave,
cross-referenced to the detailed specs. Scope covers every commit
from the prototype through Wave 9.0 (accounts + memberships data
layer).

Use this doc to answer "what can the system do today?" without
reading a dozen wave-specific files.

---

## 1. Headline capabilities

| Capability | Ships as | Spec |
| ---------- | -------- | ---- |
| JWT-scoped skill catalog | FastAPI catalog server + `POST /token` + bearer-gated reads | [architecture.md](architecture.md), [jwt-access-control.md](jwt-access-control.md) |
| Skill bundles (SKILL.md + files) | multipart upload, zip/tar/tar.gz/bz2/xz, 100 MB cap, inline or S3 store | [skill-bundles.md](skill-bundles.md) |
| Agent registry + tokens | `agents` table, `POST /token` with bounded `expires_in`, key-ring rotation + revocation list | [mcp_server/auth.md](mcp_server/auth.md), [mcp_server/routers/token.md](mcp_server/routers/token.md) |
| Web UI | FastAPI + HTMX + Bootstrap browser console with sidebar nav, modals, file viewer, immutable versions, clone-to-rename | [webui.md](webui.md) |
| Public / private visibility | Per-resource flag; public items readable by any authenticated agent | [visibility-and-accounts.md §1](visibility-and-accounts.md#1-visibility-model) |
| DB-backed operators | `users` table + bcrypt, env-bootstrap, `/admin/users/*` CRUD surface | [visibility-and-accounts.md §2](visibility-and-accounts.md#2-accounts-and-roles) |
| Interactive JWT issuance | `/agents` → Mint Token wizard with narrowing, one-time copyable token | [visibility-and-accounts.md §3](visibility-and-accounts.md#3-token-issuance-ux) |
| Accounts + memberships foundation | Tenant tables + last-admin guard + env-hardcoded superadmin | [user-management.md](user-management.md) (spec), Wave 9.0 (implementation) |
| Multi-framework example agents | Anthropic / OpenAI / LangChain / LangGraph runners + skill bundles for Cursor | [example-frameworks.md](example-frameworks.md) |
| Productization groundwork | Rate limits, structured logs, request IDs, typed errors, S3 bundle store, Dockerfiles, Helm chart, CI matrix | [productization.md](productization.md), [deployment.md](deployment.md) |

**Tests:** 445 cases, 85.2% line coverage enforced at CI. See [testing.md](testing.md).

---

## 2. Features by wave

### Prototype waves (pre-productization)

| Wave | Commit | Delivered |
| ---- | ------ | --------- |
| P0 | initial | FastAPI catalog; skills + skillsets + agents + JWT issuance; `/token`, `/skills`, `/skills/{id}`, `/skillsets`, `/agents` endpoints. SQLite + SQLAlchemy 2.0. |
| P1 | bundle storage | Multipart upload, zip/tar archives, SKILL.md inline render, per-file SHA256 + size, `GET /skills/{id}/versions/{ver}/files/{path}`. |
| P2 | Web UI v1 | Jinja2 templates, sidebar nav, skill/skillset management pages. |
| P3 | Web UI v2 | Quick-view modals, version-centric immutable-version workflow, clone-to-rename, bundle file viewer with syntax highlighting (highlight.js) + markdown render (marked). |
| P4 | Cross-skill bundle copy | `POST /skills/{dst}/versions/{dst_v}/bundle/copy-from/{src}/{src_v}`. |
| P5 | Multi-framework examples | `SkillfulAnthropicAgent`, `SkillfulOpenAIAgent`, `SkillfulLangChainAgent`, `SkillfulLangGraphAgent` base classes in `example/skillful/`; per-framework runners; Cursor bridging via skill bundle format. |

### Productization waves

| Wave | Commit | Delivered |
| ---- | ------ | --------- |
| 1 | [7bdd65a] | Structured JSON logs, per-request IDs (`X-Request-ID` round-trip), typed errors with machine codes (`SKILL_NOT_FOUND`, `CSRF_FAILED`, `VALIDATION_ERROR`, ...), request-scoped log context. |
| 2 | [11bd6fc] | Alembic migrations (`0001_initial`, `0002_visibility`, `0003_users`, `0004_accounts_and_memberships`). Postgres parity in CI. `alembic upgrade head` on startup when `MCP_DATABASE_URL` is Postgres. |
| 3 | [0dd3acf] | Per-IP token-bucket rate limit middleware (`MCP_RATE_LIMIT_PER_MINUTE`, default 600), app-level request-body size cap (`MCP_MAX_REQUEST_BODY_MB`, default 101), `?limit=` on `GET /skills`. |
| 4 | [04e3e49] | JWT key-ring with `kid` header; `MCP_JWT_KEYS` + `MCP_JWT_ACTIVE_KID` for rotation; `jti` on every mint; `/admin/tokens/revoke` + in-process revocation list; server-side clamp on `expires_in` via `MCP_MAX_TOKEN_LIFETIME_SECONDS` (default 86400). Legacy `MCP_JWT_SECRET` auto-wraps as `kid=primary`. |
| 5 | [751fb93] | `BundleStore` abstraction; S3 backend gated on `MCP_BUNDLE_STORE=s3`; `moto` in tests. Enables horizontal scaling of the catalog. |
| 6a | [928d7e9] | Web UI operator auth: bcrypt passwords via `MCP_WEBUI_OPERATORS` (JSON), signed session cookies (Starlette `SessionMiddleware`), `AuthMiddleware` redirect, CSRF via FastAPI dep (body-consumption safe). |
| 7 | [3505d89] | Docker images (`deploy/Dockerfile.catalog`, `deploy/Dockerfile.webui`), `docker-compose.yml` local stack with Postgres 16, Helm chart (`deploy/helm/skillful-mcp`), GitHub Actions CI matrix (SQLite py3.11/3.12 + Postgres 16 + lint + Docker build + Helm lint), 85% coverage gate. |
| 8a | [3f43d8c] | `visibility` column on skills + skillsets; public items bypass the grant requirement for authenticated agents. Additive authorization — no deny path. |
| 8b | [6a01edf] | `users` table, `admin`/`viewer` roles, `/admin/users/*` CRUD, server-side `authenticate` endpoint keeping bcrypt hashes off the wire, `MCP_WEBUI_OPERATORS` becomes bootstrap-only, `/users` + `/account` Web UI pages, last-active-admin delete guard. |
| 8c | [f7468f2] | Interactive JWT issuance: `/agents` list, `/agents/{id}/tokens/new` wizard with checkbox narrowing (skills / skillsets / scope), one-time copyable token view. `POST /token` extended with narrowing lists + subset-of-agent-grants validation. |
| Public landing | [9b75439] | `/` no longer requires auth; shows public catalog to anonymous visitors plus Sign-In button. Logged-in users see the same list + dashboard counts. |

### Wave 9 (account-based multi-tenant model)

| Step | Commit | Delivered | Status |
| ---- | ------ | --------- | ------ |
| 9.0 | [fdd9c81] | Data layer + service + superadmin (see §3 below). | **shipped** |
| 9.1+ | — | Public/authenticated routes, account-scoped routes, catalog-account stamping, sharing endpoints, Web UI account switcher, delete-user modal. | planned in [user-management.md §11](user-management.md#11-sequencing) |

---

## 3. Wave 9.0 in detail

Data model additions (migration `0004_accounts_and_memberships`):

- `accounts` — id (uuid4 hex), name (unique), timestamps.
- `account_memberships` — composite PK `(user_id, account_id)` + `role ∈ {account-admin, contributor, viewer}`. Secondary index on `account_id`. Cascades on both FK deletes.
- `pending_memberships` — email-keyed invitations that resolve on signup; UNIQUE `(email, account_id)`; FK to inviter `ON DELETE SET NULL`.
- `users.role` dropped; `users.last_active_account_id` added (FK `ON DELETE SET NULL`); CHECK `users.id != '0'` reserves the id space for the superadmin.

Service layer ([mcp_server/accounts.py](mcp_server/accounts.py)):

- `create_account` atomically inserts the account + an `account-admin` membership for the caller + stamps their `last_active_account_id`.
- `add_membership` / `remove_membership` / `update_membership_role` with `VALID_MEMBERSHIP_ROLES` validation.
- **Last-admin guard** via `SELECT ... FOR UPDATE` inside the transaction, counting only active (non-disabled) admins. Raises `LastAdminError` → 409.
- `add_pending_membership` / `consume_pending_for_user` — resolves pending rows at signup time, silently skipping already-existing memberships so an admin pre-invite + admin direct-add race doesn't fail.
- `bootstrap_default_account` pairs with `users.bootstrap_from_env` so fresh deployments land env operators in a shared `default` account.

Env-hardcoded superadmin:

- Email: `superadmin@skillfulmcp.com` (hardcoded). `.strip().lower()` normalization runs before the reserved-email check, blocking case/whitespace variants.
- Password: `MCP_SUPERADMIN_PASSWORD_HASH` (bcrypt). Catalog startup refuses the process if the var is empty.
- `POST /admin/users/authenticate` short-circuits the hardcoded email against the env hash and returns `AuthenticateResponse(id="0", is_superadmin=True)` without touching the DB.
- Web UI `Operator` gains `is_superadmin`. Existing templates keep working — every logged-in user still appears as `role="admin"` of the old admin-users UI until Wave 9.5 replaces those pages.

Tests (in addition to the pre-9.0 suite):

- [tests/test_accounts.py](../tests/test_accounts.py) — 15 cases covering atomic account-creation, last-admin guard (incl. disabled-admin exclusion), pending-invite consume, default-account bootstrap.
- [tests/test_users.py](../tests/test_users.py) rewritten for the role-less model; adds reserved-email normalization + superadmin auth paths.
- [tests/test_webui_users.py](../tests/test_webui_users.py) updated for role-less payloads.

---

## 4. Current system at a glance

### Top-level components

```
                        ┌─────────────────────────────────────────────┐
                        │              Web UI (webui/)                │
                        │  FastAPI + HTMX + Bootstrap; sessions +     │
                        │  CSRF; landing /, /skills, /skillsets,      │
                        │  /agents, /users, /account, /login          │
                        └──────────────┬──────────────────────────────┘
                                       │  HTTP (MCPClient, admin key)
                                       ▼
┌────────────────────────────────────────────────────────────────────┐
│                   mcp_server/ (catalog)                            │
│                                                                    │
│   Routers:                                                         │
│     /token                 ← POST mint JWT (bounded, narrowed)     │
│     /skills + /skillsets   ← JWT reads + admin writes              │
│     /agents                ← admin CRUD                            │
│     /admin/*               ← admin-key variants +                  │
│                              /admin/users/* (Wave 8b)              │
│                              /admin/tokens/revoke                  │
│     /health, /livez, /readyz                                        │
│                                                                    │
│   Services (mcp_server/*.py):                                      │
│     catalog, registry, bundles, authorization, auth (keyring +     │
│     revocation), ratelimit, errors, logging_config, users,         │
│     accounts (Wave 9.0), pwhash                                    │
│                                                                    │
│   Middleware: RequestID → RequestSizeLimit → RateLimit → handler   │
└──────────────────┬─────────────────────────────────────────────────┘
                   │  SQLAlchemy 2.0
                   ▼
      ┌─────────────────────────────────┐
      │  SQLite (dev) / Postgres (prod) │
      │  + BundleStore (inline / S3)    │
      └─────────────────────────────────┘
```

### Routes shipped today

**JWT-protected (agent-facing):**

- `GET /skills`, `GET /skills/{id}[?version=]`, `GET /skills/{id}/versions`
- `GET /skills/{id}/versions/{ver}/files`, `GET /.../files/{path:path}`
- `GET /skillsets/{id}/skills`

**Admin-key (operator / CI):**

- `POST /token` (with optional `skills` / `skillsets` / `scope` narrowing)
- `POST|PUT|DELETE /skills`, `POST|PUT|DELETE /skillsets`, `POST|PUT|DELETE /agents`
- Bundle upload / delete / copy under `/skills/{id}/versions/{ver}/bundle`
- `GET /admin/skills`, `/admin/skills/{id}[?version=]`, `/admin/skills/{id}/versions`, `/admin/skills/{id}/versions/{ver}/files[/{path:path}]`
- `GET /admin/skillsets/{id}/skills`
- `GET|POST|PUT|DELETE /admin/users` + `/admin/users/{id}` + `POST /admin/users/authenticate`
- `POST /admin/tokens/revoke`, `GET /admin/tokens/revoked-count`

**Web UI (session-gated):**

- `/` (public landing), `/login`, `/logout`
- `/skillsets`, `/skillsets/{id}`, `/skillsets/{id}/modal`
- `/skills`, `/skills/{id}`, `/skills/{id}/modal`, `/skills/{id}/clone`, `/skills/{id}/new-version`
- `/agents`, `/agents/{id}/tokens/new`, `/agents/{id}/tokens`
- `/users`, `/users/new`, `/users/{id}`, `/account`
- `/skills/{id}/versions/{ver}/files/{path:path}` (file viewer)

### Configuration surface

Required:

- `MCP_JWT_SECRET` (or `MCP_JWT_KEYS` + `MCP_JWT_ACTIVE_KID` for rotation)
- `MCP_ADMIN_KEY`
- `MCP_WEBUI_SESSION_SECRET` (Web UI)
- `MCP_SUPERADMIN_PASSWORD_HASH` (Wave 9)

Common:

- `MCP_DATABASE_URL` (SQLite default; Postgres for prod)
- `MCP_WEBUI_OPERATORS` (bootstrap-only since Wave 8b)
- `MCP_MAX_TOKEN_LIFETIME_SECONDS` (default 86400)
- `MCP_RATE_LIMIT_PER_MINUTE` (default 600; 0 disables)
- `MCP_MAX_REQUEST_BODY_MB` (default 101)
- `MCP_BUNDLE_STORE` (`inline` or `s3`) + `MCP_BUNDLE_S3_BUCKET`
- `MCP_LOG_LEVEL` (default `INFO`)
- `MCP_WEBUI_CSRF_ENABLED` (default on)

Full listing: [mcp_server/config.md](mcp_server/config.md), [webui/config.md](webui/config.md).

---

## 5. Test + CI posture

- **445 tests** across 22 modules. Unit (services), integration (FastAPI TestClient), HTTP fuzz on bundle uploads, migration parity (SQLite + Postgres).
- **Coverage gate:** 85% enforced in CI (currently 85.2%). Configured in `[tool.coverage.report]`.
- **CI jobs** ([.github/workflows/ci.yml](../.github/workflows/ci.yml)):
  1. SQLite on py3.11 + py3.12 (full suite + coverage gate)
  2. Postgres 16 (migrations-driven schema parity)
  3. Ruff lint (soft-fail; full strictness in a follow-up wave)
  4. Docker build (both images)
  5. Helm lint + template render
- **Framework-SDK tests** (`test_skillful_agents.py`) use `pytest.importorskip` so the Postgres job (no `examples` extras) skips them cleanly.

---

## 6. Deployment paths

| Target | Entry point | Notes |
| ------ | ----------- | ----- |
| Local dev | `make serve` / `make webui` (two uvicorn processes) | SQLite default; reload=True. |
| docker-compose | `docker compose up` | Postgres 16 service, both images built locally. |
| Kubernetes | `deploy/helm/skillful-mcp` | Chart with catalog + webui deployments, ingress, externalSecret hook. |
| Manual | `python -m uvicorn mcp_server.main:create_app --factory` | Standard ASGI process. |

Runbook + `.env.example` + `kubectl` recipes: [deployment.md](deployment.md).

---

## 7. Deferred / in-flight

Planned but not yet implemented — see linked specs for detail:

- **Wave 9.1+** (user management): self-service signup, account CRUD surfaces, membership management endpoints, agent account-scoping, three-tier visibility (`public` / `account` / `private`), email-based allow lists. Full design in [user-management.md](user-management.md).
- **SMTP invitations** (Wave 9.x) — signup-by-email verification closes the pending-invitation squatter gap.
- **Transfer-superadmin** (Wave 9.x) — CLI rotate for `MCP_SUPERADMIN_PASSWORD_HASH`.
- **OIDC** (Wave 6b) — external identity providers for the Web UI.
- **Audit log** (productization §3.1 P2) — queryable `{tenant_id, operator_id, action, target, diff, ts}` table.
- **KMS-backed asymmetric keys + JWKS** — productization §3.1 (agent auth).
- **Agent sandboxing / signed bundles** — productization §3.2.

---

## 8. Where to read next

Ordered for a new reader:

1. [architecture.md](architecture.md) — top-level system view.
2. [requirements.md](requirements.md) — what the system must do.
3. [jwt-access-control.md](jwt-access-control.md) + [agent-model.md](agent-model.md) — authorization model.
4. [prototype.md](prototype.md) — P0 codebase walkthrough.
5. [skill-bundles.md](skill-bundles.md) — bundle storage + archive handling.
6. [webui.md](webui.md) — browser console.
7. [productization.md](productization.md) — productization roadmap + wave log.
8. [visibility-and-accounts.md](visibility-and-accounts.md) — Wave 8 design + ship notes.
9. [user-management.md](user-management.md) — Wave 9 design (accounts + memberships + allow lists).
10. [testing.md](testing.md) + [migrations.md](migrations.md) + [deployment.md](deployment.md) — operational depth.

Per-module specs under [`mcp_server/`](mcp_server/), [`webui/`](webui/), [`cli/`](cli/), [`example/`](example/) track one source file each.
