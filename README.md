# SkillfulMCP

A JWT-based skill authorization server for agentic systems. Agents
receive signed tokens that declare exactly which skills and skillsets
they can access. The server enforces those boundaries on every request
and also stores the full skill bundles (SKILL.md + supporting files) so
agents can fetch what they need at runtime.

Ships with:

- **Catalog API** — FastAPI service with JWT-scoped reads, admin-gated
  writes, key-ring rotation, revocation, typed error envelope, structured
  JSON logs, rate limiting, `/livez` + `/readyz` probes, Alembic
  migrations.
- **Web UI** — browser console with operator login, session cookies,
  CSRF protection, version-centric skill editing, bundle file viewer
  with syntax highlighting.
- **`mcp-cli`** — Typer CLI for catalog management and token minting.
- **Multi-framework examples** — Anthropic SDK, OpenAI SDK, LangChain,
  LangGraph, plus a Cursor bridging guide — all consuming the same
  catalog.
- **Bundle storage** — inline (SQLite BLOB) by default, optional S3
  backend behind the same interface.
- **Deploy artifacts** — two Dockerfiles, docker-compose, a Helm chart,
  a GitHub Actions CI workflow with a ≥85% coverage gate.

## Concepts

| Term | Description |
|---|---|
| **Skill** | A versioned, named capability with metadata (description, JSON-schema input) and an optional file bundle (SKILL.md + any supporting files). Versions follow semver; names are immutable within a skill id. |
| **Skillset** | A named group of skills. Agents are granted access at the skillset level or per individual skill. |
| **Agent** | A registered consumer with a list of allowed skillsets and/or skills and a scope (`read`, `execute`). |
| **JWT** | A signed token issued to an agent. Claims embed the authorized skillsets, skills, scope, plus `kid` (signing-key id) and `jti` (unique id, revocable). |
| **Operator** | A human who manages the catalog via the Web UI (or CLI). Authenticates with email + bcrypt password; session is a signed cookie. |

Authorization uses an **additive union**: an agent can access any skill
granted explicitly or via a skillset. There is no deny list. Scope is
stored on the agent + embedded in every JWT. Enforcement of scope values
beyond the grant set is future work.

## Project layout

```
mcp_server/              FastAPI catalog service
  main.py                App factory, lifespan, middleware + exception wiring
  config.py              Settings (env-driven, lru_cache'd)
  database.py            Engine + session + migration-aware bootstrap
  models.py              SQLAlchemy 2.0 ORM
  schemas.py             Pydantic v2 request/response types
  auth.py                TokenService (issuance + validation + revocation)
  keyring.py             KeyRing (single-secret legacy + multi-key rotation)
  revocation.py          In-process jti deny-list (Redis-swappable)
  authorization.py       Resolves allowed-skill set from claims
  catalog.py             Skill + skillset CRUD service
  registry.py            Agent CRUD service
  bundles.py             Archive extraction + BundleStore abstraction
  dependencies.py        FastAPI DI (get_db, get_current_claims, require_admin)
  middleware.py          RequestID + request-size + rate-limit
  ratelimit.py           Token bucket (per-IP)
  logging_config.py      JSON formatter + request-id context
  errors.py              Global exception handlers (typed envelope)
  routers/               One module per resource group
    health.py            /health, /livez, /readyz
    token.py             POST /token (admin)
    skills.py            /skills (JWT reads + admin writes)
    bundles.py           /skills/{id}/versions/{ver}/bundle + files
    agents.py            /agents (admin)
    skillsets.py         /skillsets + membership
    admin.py             /admin/* — admin-key variants + /admin/tokens/revoke

webui/                   Browser console (FastAPI + Jinja + HTMX)
  main.py                App factory, routes
  auth.py                Operator auth, bcrypt, session + CSRF helpers
  middleware.py          AuthMiddleware + csrf_required dep
  client.py              Async MCPClient (httpx → catalog API)
  config.py              Settings (operators, session secret, CSRF toggle)
  templates/             Jinja pages + modal partials + login.html

cli/
  main.py                Typer CLI (mcp-cli)

example/                 Framework-agnostic examples
  network.yaml           Shared topology (skills, skillsets, agents, prompts)
  common/                Catalog bootstrap + simulated skill dispatcher
  skillful/              SkillfulAnthropicAgent / OpenAI / LangChain / LangGraph
  anthropic_sdk/         Runner using the Anthropic SDK
  openai_sdk/            Runner using the OpenAI SDK
  langchain_app/         Runner using LangChain 1.x create_agent
  langgraph_app/         Runner using LangGraph StateGraph
  cursor/                Bridging notes (stdio MCP adapter)

migrations/              Alembic (env.py + versions/)
alembic.ini

deploy/                  Deployment artifacts
  Dockerfile.catalog     Multi-stage, non-root, /livez healthcheck
  Dockerfile.webui
  helm/skillful-mcp/     Helm chart (Deployments, HPA, PDB, Ingress, …)

docker-compose.yml       Local stack: catalog + webui + Postgres
.github/workflows/ci.yml Test matrix + docker build + helm lint

tests/                   pytest suite (379 passing, 85%+ coverage)
  test_api_*.py          HTTP integration per router
  test_auth.py           JWT issuance/validation unit
  test_keyring_revocation.py
  test_bundles.py        Archive extraction + persistence
  test_bundles_fuzz.py   300+ random inputs, malicious paths
  test_api_bundles.py    Bundle upload/download/copy over HTTP
  test_bundle_store_s3.py S3 backend via moto
  test_cli.py            Typer CLI via CliRunner + MockTransport
  test_webui.py          Webui with AsyncMock MCPClient
  test_webui_auth.py     Login, logout, CSRF
  test_webui_client.py   Real MCPClient wire via MockTransport
  test_e2e.py            Catalog + webui stitched via ASGITransport
  test_migrations.py     Alembic upgrade/downgrade parity
  test_observability.py  Request-id middleware, JSON log, typed errors
  test_rate_limit.py     Token bucket + middleware + size cap
  test_skillful_agents.py Agent-class translation + lazy fetch
  (and more)

spec/                    Design + implementation docs
  architecture.md, requirements.md, jwt-access-control.md,
  agent-model.md, prototype.md, skill-bundles.md, webui.md,
  example-frameworks.md, productization.md, migrations.md,
  deployment.md, testing.md
  mcp_server/, webui/, cli/, example/   — per-submodule specs
```

## Quickstart

### 1. Install

```bash
make install               # base + dev
make install-examples      # also pulls in openai + langchain + langgraph

# or manually:
pip install -e ".[dev]"                       # base
pip install -e ".[dev,postgres]"              # + psycopg2-binary
pip install -e ".[dev,postgres,s3,examples]"  # the works
```

Python ≥ 3.11. Details and a bcrypt hash helper are in
[`requirement.md`](requirement.md).

### 2. Configure

```bash
make env                   # copies .env.example → .env
# edit .env to set secrets; see the table below
```

**Required**:

| Variable | What it's for |
|---|---|
| `MCP_JWT_SECRET` | HMAC secret for signing JWTs. Long random string. Legacy single-key mode. |
| `MCP_ADMIN_KEY` | Static `X-Admin-Key` header for catalog write endpoints. |
| `MCP_WEBUI_SESSION_SECRET` | Signing secret for the Web UI session cookie (required when running the Web UI). |
| `MCP_WEBUI_OPERATORS` | JSON list of `{email, password_hash}` entries — **bootstrap only**. On first boot the `users` table is seeded from this list; after that, operator management happens in the Web UI `/users` pages. |

Generate a bcrypt hash with:

```bash
python -c "from webui.auth import hash_password; print(hash_password('your-password'))"
```

**Common overrides**:

| Variable | Default | Purpose |
|---|---|---|
| `MCP_DATABASE_URL` | `sqlite:///./skillful_mcp.db` | Postgres URL (`postgresql://user:pw@host/db`) enables migrations-driven schema. |
| `MCP_JWT_KEYS` / `MCP_JWT_ACTIVE_KID` | — | Multi-key rotation mode (JSON `{kid: secret}` + active kid). |
| `MCP_MAX_TOKEN_LIFETIME_SECONDS` | `86400` | Server-side cap on `expires_in`. |
| `MCP_RATE_LIMIT_PER_MINUTE` | `600` | Per-IP throttle. `0` disables. |
| `MCP_MAX_REQUEST_BODY_MB` | `101` | App-level body size cap. |
| `MCP_BUNDLE_STORE` | `inline` | `s3` switches to object-store backend. |
| `MCP_BUNDLE_S3_BUCKET` | — | Required when `MCP_BUNDLE_STORE=s3`. |
| `MCP_LOG_LEVEL` | `INFO` | Catalog log level. |

Full list with bundle S3 / logging / CSRF toggles:
[`spec/mcp_server/config.md`](spec/mcp_server/config.md),
[`spec/webui/config.md`](spec/webui/config.md).

### 3. Run the server

```bash
make serve            # catalog  → http://localhost:8000  (Swagger at /docs)
make webui            # web UI   → http://localhost:8080  (login at /login)

# or the full stack via docker-compose (catalog + webui + Postgres):
make docker-up
```

### 4. Run the tests

```bash
make test             # plain pytest (379 tests, ~20s)
make test-cov         # + coverage (fails below 85%)
```

All default tests run against in-memory SQLite. Postgres-gated migration
tests pick up `MCP_TEST_POSTGRES_URL=postgresql://…` when set.

---

## API overview

Interactive docs: `http://localhost:8000/docs`. Full router-by-router
spec: [`spec/mcp_server/routers/`](spec/mcp_server/routers/).

### Authentication

- **Agent endpoints** (`GET /skills*`, bundle reads): `Authorization: Bearer <JWT>`.
- **Write endpoints + `/admin/*`**: `X-Admin-Key: <value>`.
- **Probes** (`/health`, `/livez`, `/readyz`): no auth.

### Error envelope

Every error carries:

```json
{
  "detail": "...",
  "code": "HTTP_404" | "VALIDATION_ERROR" | "RATE_LIMIT_EXCEEDED" | "INTERNAL_ERROR" | ...,
  "request_id": "abcdef..."
}
```

The `X-Request-ID` response header matches the `request_id` field and
appears in every server log line.

### Endpoints

```
GET    /livez /readyz /health          probes (public)
POST   /token                          mint JWT for a registered agent (admin)

GET    /skills                         JWT reads (latest versions, authorized set)
                                       ?limit=N caps the response
GET    /skills/{id}                    JWT read, latest or ?version=
GET    /skills/{id}/versions           JWT, list versions
POST   /skills                         admin create (409 on duplicate id+ver)
PUT    /skills/{id}                    admin upsert
DELETE /skills/{id}[?version=]         admin delete (all versions, or one)

POST   /skills/{id}/versions/{ver}/bundle        admin upload (zip/tar/tar.gz/bz2/xz, ≤100 MB)
POST   /skills/{id}/versions/{ver}/bundle/copy-from/{src_id}/{src_ver}
                                                 admin, same-skill or cross-skill copy
DELETE /skills/{id}/versions/{ver}/bundle        admin
GET    /skills/{id}/versions/{ver}/files         JWT, list files in bundle
GET    /skills/{id}/versions/{ver}/files/{path}  JWT, fetch one file
GET    /skills/{id}/versions/{ver}/bundle        JWT, download reconstructed .tar.gz

GET    /agents, GET/POST/PUT/DELETE /agents/{id}   admin


---

## Web UI

Browser console for catalog operators.

```bash
make webui
# → http://localhost:8080
```

**Login** with an operator email + password (see `MCP_WEBUI_OPERATORS`).

**What you can do**:

- Browse skillsets + skills. Click any row for a quick-view modal.
- Search skills by substring; filter by one or more skillsets (client-side, instant).
- Open a skill's detail — **read-only** page with version selector pills, syntax-highlighted metadata JSON, bundle file list, SKILL.md preview.
- Click a bundle file → viewer modal with syntax highlighting (Python, JS, TS, HTML, CSS, JSON, YAML, shell, etc.) or rendered markdown. Binary files show a download button.
- **New version** button (immutable-version workflow): create a new version with copy/upload/none bundle semantics.
- **Clone** button: rename a skill by creating a new id prefilled from the source.
- Sidebar footer shows the logged-in operator (email + role badge) + a CSRF-protected logout.
- **Public / Private** toggle on every skill and skillset. Public items are visible to any authenticated agent regardless of grants.
- **Users** page (admins only): list / create / edit / delete DB-backed operators. Roles are `admin` (full privileges) or `viewer` (read-only UI). The guard refuses to delete the last active admin.
- **Account** page: self-service password change for DB-backed users.
- **Agents** page + **Mint token** wizard: pick an agent, optionally uncheck skills / skillsets / scope entries to narrow the JWT below the agent's registered grants, set expiry, get a one-time copyable token.
- Mobile-friendly: sidebar collapses to a hamburger offcanvas.

Every mutating form is CSRF-protected (hidden input + HTMX global header
hook). All data operations go through the MCP catalog API — the UI
proxies with the configured admin key.

Full spec: [`spec/webui.md`](spec/webui.md) +
[`spec/webui/`](spec/webui/). Next-wave plan for owner-based skill
management, email allow lists, and per-account tenant isolation:
[`spec/user-management.md`](spec/user-management.md) (introduces an
`accounts` table plus `superadmin` / `account-admin` / `contributor`
/ `viewer` roles, extending the current flat `admin` / `viewer`
model).

---

## CLI

```bash
# Skills
mcp-cli skill add --id lookup-invoice --name "Lookup" --version 1.0.0 \
  --description "Retrieve invoice details" --skillset billing
mcp-cli skill delete --id lookup-invoice --version 1.0.0

# Agents
mcp-cli agent add --id billing-agent --name "Billing" \
  --skillsets billing,support --scope read,execute
mcp-cli agent delete --id billing-agent

# Tokens (prints the raw JWT for shell composition)
TOK=$(mcp-cli token issue --agent-id billing-agent --expires-in 3600)

# Bulk import from YAML or JSON
mcp-cli catalog import --file catalog.yaml --upsert
```

Requires `MCP_SERVER_URL` and `MCP_ADMIN_KEY` in the environment.

---

## Examples: multi-framework agent runners

[`example/`](example/README.md) ships four runnable agent runners built
on the **same** catalog, each using a different framework's tool-calling
layer:

| Runner | Framework | Backend model |
|---|---|---|
| `example.anthropic_sdk.run_network` | Anthropic SDK | Claude |
| `example.openai_sdk.run_network` | OpenAI SDK | GPT-4o |
| `example.langchain_app.run_network` | LangChain 1.x | Claude |
| `example.langgraph_app.run_network` | LangGraph | Claude |
| `example/cursor/README.md` | Cursor IDE | MCP stdio bridge guide |

Each runner uses a reusable class (`SkillfulAnthropicAgent`,
`SkillfulOpenAIAgent`, `SkillfulLangChainAgent` [Runnable],
`SkillfulLangGraphAgent` [Runnable]) that fetches skills from the
catalog on first use and translates them to the framework's native tool
format — users don't write bootstrap code themselves.

```bash
export MCP_JWT_SECRET=example-secret MCP_ADMIN_KEY=admin-key
make serve &                                    # catalog
export ANTHROPIC_API_KEY=sk-ant-...
make example-anthropic MESSAGE="Look up invoice #INV-1234"
make example-openai
make example-langchain
make example-langgraph
```

Topology in [`example/network.yaml`](example/network.yaml): one
orchestrator (`intent-router`) delegating to two specialists
(`billing-agent`, `tech-support-agent`) via a `route_to_agent`
meta-tool. Every runner produces the same user-visible answer.

---

## Deploying

### Docker Compose (local / demo)

```bash
make docker-up        # catalog + webui + Postgres, wait-for-healthy
```

### Kubernetes via Helm

```bash
# Provision the Secret out-of-band (NOT managed by the chart —
# use External Secrets / SealedSecrets / your cloud's KMS).
kubectl create secret generic skillful-mcp-secrets \
  --from-literal=MCP_JWT_SECRET="$(openssl rand -hex 32)" \
  --from-literal=MCP_ADMIN_KEY="$(openssl rand -hex 32)" \
  --from-literal=MCP_DATABASE_URL="postgresql://..." \
  --from-literal=MCP_WEBUI_SESSION_SECRET="$(openssl rand -hex 32)" \
  --from-literal=MCP_WEBUI_OPERATORS='[{"email":"alice@example.com","password_hash":"$2b$..."}]'

helm upgrade --install mcp deploy/helm/skillful-mcp \
  --set image.registry=ghcr.io \
  --set image.repository=youraccount/skillful-mcp \
  --set image.catalogTag=0.1.0 --set image.webuiTag=0.1.0 \
  --set ingress.enabled=true --set ingress.host=mcp.example.com
```

Chart ships: Deployments × 2, Services × 2, HPAs × 2, PDBs × 2, Ingress
(opt-in), ConfigMap, ServiceAccount, pod + container security contexts,
`readOnlyRootFilesystem`.

Full runbook, including secret rotation, token revocation, and
bundle-store migration: [`spec/deployment.md`](spec/deployment.md).

---

## Observability

- **Logs**: JSON to stdout. Every line carries `ts`, `level`, `logger`,
  `msg`, `request_id`, plus caller-supplied extras. Map to Loki / Cloud
  Logging / Datadog via your log shipper.
- **Probes**: `/livez` (alive), `/readyz` (alive + DB reachable +
  settings loaded).
- **Request correlation**: `X-Request-ID` round-trips on every request
  (generated when missing); error responses include it in both the body
  and the header.
- **Typed errors**: consistent `{detail, code, request_id}` envelope.
- **Metrics / OTel**: not shipped yet; see `spec/productization.md` §3.6.

---

## Security posture

What's in:

- JWT signing with key-ring rotation (multi-`kid`).
- `jti` on every token + in-process revocation list (`POST /admin/tokens/revoke`).
- Server-side `expires_in` clamp (default 24h).
- Bcrypt password hashing for operators.
- Signed session cookies (`HttpOnly` + `SameSite=Lax`).
- CSRF on every mutating endpoint (header OR form field).
- Path-traversal / symlink / size-bomb guards on bundle uploads.
- Non-root containers with `readOnlyRootFilesystem`.
- Per-IP rate limit + app-level request body cap.

What's **not** in (tracked in [`spec/productization.md`](spec/productization.md)):

- OIDC operator login (Wave 6b).
- Tenant-scoped roles.
- Revocation list backed by Redis (today: per-process).
- Operator-forwarded bearer tokens (today: Web UI uses a shared admin key
  to reach the catalog).
- KMS-backed asymmetric JWT signing.

`MCP_JWT_SECRET` must be set or the server refuses to start.
`MCP_ADMIN_KEY` check is skipped when empty (dev only); set it in any
shared environment.

---

## Documentation map

- **High-level design**: [`spec/architecture.md`](spec/architecture.md),
  [`spec/requirements.md`](spec/requirements.md),
  [`spec/jwt-access-control.md`](spec/jwt-access-control.md),
  [`spec/agent-model.md`](spec/agent-model.md),
  [`spec/prototype.md`](spec/prototype.md).
- **Feature specs**:
  [`skill-bundles.md`](spec/skill-bundles.md),
  [`webui.md`](spec/webui.md),
  [`example-frameworks.md`](spec/example-frameworks.md),
  [`migrations.md`](spec/migrations.md),
  [`deployment.md`](spec/deployment.md),
  [`testing.md`](spec/testing.md).
- **Productization plan**:
  [`spec/productization.md`](spec/productization.md) — gap analysis,
  action items P0/P1/P2, rollout milestones. Tracks what each wave
  shipped vs. what's still open.
- **Per-submodule specs** (implementation-level, one file per source
  module): [`spec/mcp_server/`](spec/mcp_server/),
  [`spec/webui/`](spec/webui/),
  [`spec/cli/`](spec/cli/),
  [`spec/example/`](spec/example/).
- **Install**: [`requirement.md`](requirement.md).
