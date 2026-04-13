# Productization Plan

This document is a running plan for turning the SkillfulMCP prototype into a
cloud-deployable, multi-tenant service with scalability, account management,
and observability as first-class concerns.

It is intentionally opinionated about *what* needs to change and *why*, and
deliberately vague on implementation details that depend on the chosen cloud
(AWS vs GCP vs Azure vs bare Kubernetes). When a choice is forced, it picks
the pragmatic default but calls out the alternatives.

---

## 1. Gaps the prototype exposes

These are the issues that the current code makes evident as soon as you
imagine running it in production.

### 1.1 Identity and auth

- **Single shared admin key (`MCP_ADMIN_KEY`) protects every write endpoint.**
  Anyone with the key can mutate any skill, skillset, or agent, including
  minting a JWT for any agent (`POST /token`). That is a root-equivalent
  secret and is unsuitable for multi-user or multi-tenant deployments.
- **Agent JWTs are signed with a single static secret (`MCP_JWT_SECRET`)**,
  HS256. Key rotation is manual and disruptive; there is no `kid` header and
  no key-ring.
- **No concept of a user or tenant.** Skills, skillsets, and agents live in a
  single flat namespace.
- **Tokens are not revocable mid-flight.** A compromised JWT is valid until
  `exp`; there is no deny list or version counter.
- **`POST /token` accepts any `agent_id` that exists** once you present the
  admin key — there is no policy ("who can mint tokens for which agent").

### 1.2 Storage and data model

- **SQLite with `StaticPool` and a process-local file.** No horizontal
  scaling; every replica would diverge. Write lock contention even within
  one process under load. No point-in-time recovery, replication, or backup.
- **Bundle bytes live in the `skill_files.content` BLOB column.** Files up
  to 100 MB per bundle in a relational database is fine for a prototype and
  wrong at scale (hot-row contention, backup size, replication lag).
  [spec/skill-bundles.md](skill-bundles.md) already anticipates this move.
- **No migrations framework.** Schema changes rely on
  `Base.metadata.create_all()` and dropping the DB; production migrations
  must be additive, reversible, and run during deploy.
- **`semver.Version.parse` failures during `_refresh_is_latest` crash the
  whole transaction.** The catalog assumes well-formed data; there is no
  data-repair path if a bad row gets in.

### 1.3 API surface and contracts

- **No versioning of the API itself.** The URL path is unprefixed (`/skills`
  not `/v1/skills`); any breaking change is visible to every client.
- **`GET /skills` returns the full set in one response.** No pagination,
  no filtering server-side (filtering is done client-side in the Web UI).
  Catalogs with thousands of skills will thrash.
- **No rate limiting, no request size caps beyond bundle uploads.**
  `POST /token` is unauthenticated-except-for-admin-key and could be used
  to DoS the JWT signer.
- **No caching headers / ETags.** The list endpoints regenerate JSON from
  scratch on every call.
- **`X-Admin-Key` and bearer JWT coexist awkwardly** — most endpoints take
  JWT, some take admin-key, a few take either. The distinction is implicit
  in the router structure (`/admin/...` vs `/skills/...`), not declared.

### 1.4 Web UI

- **`MCPClient` in the Web UI uses the admin key for everything**, so the
  UI has full god-mode against any server it can reach. OK for a local
  operator; unacceptable as a hosted console.
- **No CSRF protection** on the state-changing POSTs (the UI and API share
  an origin but there's no token).
- **No session concept.** Any operator who opens the UI *is* the admin.
- **Error handling exposes raw MCP error strings** which may contain paths,
  internal ids, or stack traces.

### 1.5 Examples and SDK

- `example/skillful/*Agent` fetches skills synchronously inside `.run()`.
  For real agent apps this should be warmed up at startup and refreshed
  periodically, with clean failure modes if the catalog is unreachable.
- `common.dispatch_skill` returns canned responses. In prod the skill needs
  a real invocation backend (endpoint URL in metadata, or a handler
  registry, or an MCP-protocol bridge).
- No retry / backoff on catalog calls. A 5xx from the catalog tears down
  the agent.

### 1.6 Ops

- **Logs are `print()` to stdout.** No structured logging, no request id
  correlation, no log levels.
- **No metrics, no traces.** A failing request is visible only to whoever
  ran the `uvicorn` command.
- **No health readiness/liveness distinction.** `GET /health` reports
  "alive" with no check on DB connectivity, JWT secret presence, or disk.
- **No deployment artifacts.** No Dockerfile, no Helm chart, no Terraform,
  no CI pipeline that builds one.
- **`.env`-based config** with no hierarchy (env > file), no typed schema
  checked at startup, no secret-manager integration.

---

## 2. Target architecture (post-productization)

One picture, then the pieces.

```
                    ┌───────────────────────────────────────┐
                    │            Public HTTPS LB            │
                    │   (ALB / GCLB / Azure Front Door)     │
                    └────────────────┬──────────────────────┘
                                     │
         ┌───────────────────────────┼───────────────────────────────┐
         │                           │                               │
 ┌───────▼────────┐          ┌───────▼────────┐             ┌────────▼────────┐
 │   Catalog API  │ × N      │    Web UI      │ × N         │   MCP Bridge    │
 │   (FastAPI)    │          │   (FastAPI)    │             │  (stdio proxy)  │
 └────────────────┘          └────────┬───────┘             └────────┬────────┘
        │ read/write                  │ admin API calls              │ MCP wire
        │                             ▼                              ▼
        │                      (same Catalog API)               per-Cursor JWT
        ▼
 ┌──────────────────────────┐    ┌────────────────────────┐
 │   Postgres (primary)     │    │  Object store (S3/GCS) │
 │   + read replicas        │    │  bundle content        │
 │  RDS/Cloud SQL           │    │                        │
 └──────────────────────────┘    └────────────────────────┘
        │
        ▼
 ┌──────────────────────────┐
 │  Redis                   │
 │  - token deny-list       │
 │  - rate limits           │
 │  - response cache        │
 └──────────────────────────┘

 Observability:     OTel SDK → Collector → {Prometheus, Tempo, Loki} / Datadog / Cloud-native
 Identity:          OIDC (Auth0 / Cognito / Clerk) for operators
                    Static service accounts for agents
 Deploy:            Container images → Kubernetes (EKS/GKE/AKS) or serverless containers (Cloud Run / Fargate)
```

---

## 3. Action items

Grouped by theme. Each item lists the **goal** and a concrete **first-step
deliverable**. Items are tagged `[P0]` / `[P1]` / `[P2]` to mark rollout
order.

### 3.1 Identity, accounts, authorization

- **[P0] Introduce tenants.** Add a `tenants` table; every skill, skillset,
  and agent row gets a `tenant_id` FK. All catalog endpoints filter by
  the caller's tenant. Deliverable: migration + a `Depends(get_tenant)`
  that resolves from either OIDC claims (operators) or JWT claims (agents).
- **[P0] Replace the shared admin key with operator accounts.** Operators
  authenticate via OIDC (Auth0, Cognito, Clerk, or self-hosted Keycloak).
  Roles: `tenant_admin`, `catalog_editor`, `read_only`. Deliverable: middleware
  that accepts an OIDC id token, validates `iss`/`aud`/`exp`, maps claims to
  roles, and populates `request.state.operator`.
- **[P0] Move `POST /token` behind an operator session + policy.** Only a
  `tenant_admin` can mint tokens for agents in their tenant, and only with a
  bounded `expires_in` (e.g. ≤ 24h by default, configurable per tenant).
- **[P1] Token revocation list.** Redis-backed set of revoked `jti`s;
  validator checks on each request. TTL = token exp.
- **[P1] Key-ring for JWT signing.** Support multiple active keys keyed by
  `kid`; rotation is "add new key as primary, keep old key as verifier for
  the deny period, then remove." Store keys in AWS KMS / Google KMS /
  Azure Key Vault.
- **[P1] Per-agent scope policies.** Extend the `agents` table with a
  `policy` JSON blob; enforce during token mint (e.g. "agents of role=X
  can only hold skillsets tagged as public").
- **[P2] Audit log.** Every write produces an audit event
  `{tenant_id, operator_id, action, target, diff, ts}` to an append-only
  store (Postgres table partitioned by month, replicated to S3 Glacier).

### 3.2 Storage

- **[P0 — partially SHIPPED] Postgres as the default DB** (behind a
  SQLAlchemy URL; dev keeps SQLite). Added `[postgres]` extra
  (`psycopg2-binary`). `ondelete=CASCADE` now fires in dev via a
  `PRAGMA foreign_keys=ON` connect listener. Remaining: explicit
  `QueuePool` tuning + `pool_size` / `max_overflow` defaults.
- **[P0 — SHIPPED] Alembic for schema migrations.** `alembic.ini` +
  `migrations/env.py` + `0001_initial_schema.py` matching current models.
  Production / on-disk dev runs `alembic upgrade head` on startup; the
  `:memory:` test path keeps using `create_all`. `tests/test_migrations.py`
  enforces parity between migrations and `Base.metadata` (catches the
  classic "PR forgot the migration" drift) and round-trips
  `downgrade base`. Postgres parity covered by the same tests when
  `MCP_TEST_POSTGRES_URL` is set.
- **[P0] Move bundle bytes to object storage.** Keep the `skill_files` row
  as the index; swap the `content BLOB` column for `storage_key TEXT`,
  `storage_backend TEXT`, `size`, `sha256`. Implement a `BundleStore`
  interface with two backends: SQLite-BLOB (for dev) and S3-compatible.
  The abstraction already exists in the spec ([skill-bundles.md](skill-bundles.md));
  ship the S3 implementation.
- **[P1] Read replicas for catalog reads.** Route `GET /skills*` and
  `GET /admin/*` to replicas via a separate `session_factory_readonly`.
  Writes stay on primary.
- **[P1] Point-in-time recovery + daily backups.** Managed DB service
  handles this; explicitly document the RPO/RTO targets.
- **[P2] Tiered bundle storage.** Bundles older than N days / not
  accessed for M days move to cheaper tier (S3 IA / Glacier Deep Archive).

### 3.3 API surface

- **[P0 — DEFERRED] Path-versioned API.** Adding `/v1/` with identical
  behavior on both paths is busywork without payoff. Deferred until the
  first breaking change to the API shape (e.g. cursor-paginated list
  envelopes); the new shape lands under `/v1/` and unversioned routes
  gain `Deprecation:` / `Sunset` headers at that point.
- **[P0 — partially SHIPPED] Pagination on list endpoints.** Wave 3
  added `?limit=` on `GET /skills` (capped at 10000, ordered by id for
  determinism) as a stopgap. Cursor-based pagination with a response
  envelope is tracked as follow-up — keyset on `(created_at, pk)`,
  returned as `{items, next_cursor}` under a future `/v1/` surface so
  the response shape change doesn't break legacy callers.
- **[P0 — SHIPPED (in-process)] Request size and rate limits.**
  `mcp_server/ratelimit.TokenBucket` + `RateLimitMiddleware` enforce
  per-IP limits (default 600 req/min, env-tunable, `0` disables).
  `RequestSizeLimitMiddleware` rejects oversize bodies with 413 before
  the handler runs. Both emit the standard error envelope with
  `Retry-After` / `request_id`. Remaining: Redis-backed bucket for
  multi-replica accuracy, per-endpoint limits (`POST /token` should be
  stricter than `GET /skills`), proxy-aware client key resolution
  (`MCP_TRUST_PROXY_HEADERS` knob) once the ingress story is settled.
- **[P0 — partially SHIPPED] Typed errors.** `mcp_server/errors.py` wraps
  every response in `{detail, code, request_id}`. Backwards-compatible —
  the legacy `detail` field is preserved. 500s are scrubbed to a generic
  `"Internal Server Error"` message. Remaining work: per-endpoint stable
  `code` values beyond `HTTP_<status>`, dropping `detail` in a future
  `/v1/` surface, and Sentry-style error tracking.
- **[P1] ETags and conditional requests** on skill and skillset reads.
  `If-None-Match` returns 304. Cuts agent poll traffic sharply.
- **[P1] Batch endpoints** (e.g. `GET /v1/skills?ids=a,b,c`) so the
  Web UI's per-skillset membership scan stops being N round-trips.
- **[P2] OpenAPI linting in CI** (Spectral) to catch accidental
  breaking changes to the spec.

### 3.4 Agent / SDK improvements

- **[P0] Real skill execution layer.** Two compatible options:
  - `skill.metadata.executor_url` — a plain HTTP endpoint the Skillful*
    agent calls with the validated input.
  - `skill.metadata.executor_mcp` — an MCP server URL; the agent speaks
    MCP to the handler. Same contract the Cursor bridge already uses.
- **[P0] Retry + circuit breaker** around catalog calls in the
  `SkillFetcher`. Token fetch failures fall back to a cached token while
  the previous one is still valid.
- **[P1] Async skill loading at agent startup** (instead of lazy on first
  `.run`). Fail fast if the agent's JWT cannot list any authorized skills.
- **[P1] Skill-list refresh on a TTL.** The SkillFetcher periodically
  re-fetches in the background so new versions become visible without
  restart.
- **[P2] Streaming `.run(message)`** for all four frameworks. Each backend
  supports native streaming; surface token deltas + tool-call events.

### 3.5 Web UI

- **[P0] Replace admin-key with operator session.** Login via OIDC;
  session cookie with `HttpOnly; Secure; SameSite=Lax`. All API calls
  forward the operator's bearer, not a shared admin key.
- **[P0] CSRF tokens** on every POST/PUT/DELETE form. Submitted via
  hidden input or `X-CSRF-Token` header.
- **[P1] Row-level permission checks** in the UI so editors in tenant A
  never see tenant B's skills even if they guess the URL.
- **[P2] Audit log view.** Per-tenant UI for recent mutations.

### 3.6 Observability

- **[P0 — SHIPPED] Structured logging.** `mcp_server/logging_config.py`
  provides a stdlib JSON formatter with request-id context injection.
  Every log line carries `ts`, `level`, `logger`, `msg`, `request_id`,
  plus caller-supplied `extra={}` fields. Tenant / operator / agent
  context lands when multi-tenancy does.
- **[P0 — SHIPPED] Request IDs.** `mcp_server/middleware.RequestIDMiddleware`
  reads or generates `X-Request-ID`, sets a `ContextVar`, emits one
  access log per request with latency, and echoes the header on the
  response. The typed error envelope embeds `request_id`.
- **[P0] OpenTelemetry instrumentation.** Autowire FastAPI, SQLAlchemy,
  httpx, Anthropic, OpenAI. Export OTLP to a collector; deployer picks
  the backend (Datadog, Grafana Cloud, Honeycomb, etc.).
- **[P0] Key metrics.**
  - Catalog: `skills_list_latency_ms`, `skill_create_total`,
    `bundle_upload_bytes`, `bundle_upload_latency_ms`.
  - Auth: `token_mint_total`, `token_validate_fail_total{reason=}`.
  - Agents (SDK-side): `skill_fetch_fail_total`, `tool_call_latency_ms`.
- **[P0 — SHIPPED] Health checks.** `/livez` returns 200 as long as the
  worker responds (no deps). `/readyz` runs `SELECT 1` through the normal
  DB session and checks `settings.jwt_secret` is non-empty; returns 503
  + per-component status on failure. Legacy `/health` kept as alias.
- **[P1] Real tracing of agent runs.** Each `agent.run(message)` is a
  trace; each tool call a child span; the catalog's response shows up on
  the same trace via OTel propagation.
- **[P1] Error tracking.** Sentry / GCP Error Reporting capture of
  unhandled exceptions with full stack + request context.
- **[P2] SLOs.** 99.9% success rate for `GET /skills`, p95 latency < 150ms;
  burn-rate alerts.

### 3.7 Deployment / packaging

- **[P0] Dockerfile** (multi-stage, slim runtime, non-root user). One
  image per service (`catalog`, `webui`). Pinned Python, pinned deps via
  `pip-tools` or `uv lock`. Deliverable: `docker build && docker run`
  boots a healthy instance.
- **[P0] Helm chart** (or equivalent Kustomize / Pulumi / CDK) with:
  - Deployment + HPA on CPU and request latency.
  - Service + Ingress (TLS via cert-manager).
  - ConfigMap for non-secret settings, External Secrets for the rest.
  - PodDisruptionBudget, liveness/readiness, resource requests.
- **[P0] CI pipeline** (GitHub Actions): lint, type check (`mypy`),
  tests, build image, push to registry, deploy to staging on merge.
- **[P1] Blue/green deploy** with automated rollback on SLO burn.
- **[P1] Secrets** via AWS Secrets Manager / GCP Secret Manager /
  External Secrets Operator. No secrets in container env at build time.
- **[P2] Managed offerings.** Optional packaging for Cloud Run / Lambda /
  Azure Container Apps for teams that don't run Kubernetes.

### 3.8 Testing

- **[P0] Postgres in the test matrix.** Docker-based fixture that spins
  up a throwaway Postgres; existing SQLite tests stay for speed.
- **[P0] Migration round-trip test.** Every PR that changes a model must
  also include a passing up-and-down Alembic migration.
- **[P1] Contract tests for the Skillful* classes** against a live
  test server (not the AsyncMock), per framework — one hit per runner to
  catch SDK drift.
- **[P1] Load tests** (k6 or Locust) hitting `GET /skills` and
  `POST /token`, with targets baked into CI as acceptance gates.
- **[P2] Fuzz test for bundle extraction.** The `bundles.py` archive
  decoder is the highest-risk surface; feed it random bytes + crafted
  archive variants.

---

## 4. Rollout sequencing

**Milestone A — "Safe single-tenant service" (P0 subset).**
Operator OIDC + JWT key-ring + Postgres + S3 bundles + Docker image +
structured logs + /livez/readyz + typed errors + rate limits. Good enough
to run for one company in one region.

**Milestone B — "Multi-tenant SaaS" (remaining P0 + P1).**
Tenants everywhere + per-tenant rate limits + audit log + token revocation
+ OTel tracing end-to-end + CI/CD + Helm chart. Good enough to onboard
external tenants.

**Milestone C — "Scale and polish" (P2).**
Read replicas + tiered bundle storage + SLOs + blue/green + streaming
runners + fuzz tests.

---

## 5. Things we're deliberately *not* doing yet

- **Multi-region active/active.** Needs conflict-free data layout; wait
  until Milestone C is stable.
- **Skill execution inside the catalog process.** Execution stays in the
  skill's own backend; the catalog is a metadata + authorization store.
- **A bespoke policy DSL.** Roles + claim-based checks cover the first
  two milestones. If a customer wants OPA / Rego, add it as a pluggable
  policy resolver later.
- **UI redesign.** The current Web UI is fine for operator use; making it
  customer-facing is a separate track.

---

## 6. Open questions

- **Where does skill execution live?** Do we expect tenants to host their
  own skill backends and just register URLs, or does the service include
  a "function hosting" layer? This choice changes every downstream
  assumption about resource limits, cold-start, and pricing.
- **Pricing model.** Per-agent? Per-token-mint? Per-skill-invocation?
  Flat per-tenant? Drives metric design.
- **Compliance scope.** SOC 2 targets dictate audit-log retention,
  encryption-at-rest options, backup durability, and access review cadence.
- **Region data residency.** If tenants are EU-first, shard the catalog
  per region before Milestone B; retrofitting that later is painful.
- **Bundle content policy.** Skills can upload arbitrary files. Do we
  AV-scan, size-cap per tenant, or refuse executables? Legal + security
  input required.
