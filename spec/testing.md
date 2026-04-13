# Testing

The test suite is how we actually know SkillfulMCP works. This spec
documents the test pyramid, how to run each tier, what it covers, and
what the coverage gate is.

## Numbers (current)

- **379 tests passing**, 1 Postgres-gated skip.
- **Coverage 85.2%** — CI fails the suite below 85%.

## Test pyramid

```
   ┌──────────────────┐
   │  live smoke test │   /tmp/smoke.sh — not in pytest; run by hand
   │  (out of band)   │   against a live catalog + webui
   └──────────────────┘
                ▲
   ┌──────────────────┐   tests/test_e2e.py — wires real catalog + webui
   │  end-to-end      │   via httpx.ASGITransport. 6 tests. No sockets.
   └──────────────────┘
                ▲
   ┌──────────────────┐   tests/test_api_*.py — catalog HTTP via
   │  HTTP integration│   TestClient. Covers every router.
   │  (per service)   │   tests/test_webui*.py — webui via TestClient.
   └──────────────────┘
                ▲
   ┌──────────────────┐   tests/test_{auth,authorization,bundles,
   │  unit            │   catalog,cli,keyring_revocation,migrations,
   │                  │   observability,rate_limit,registry,
   │                  │   skillful_agents,webui_client}.py
   └──────────────────┘
```

## Tiers

### Unit

Service-layer functions, pure utilities, single-class tests. Live under
`tests/test_<module>.py` — one file per module. Use `db_session` for
service-level DB tests, `AsyncMock` or `httpx.MockTransport` for HTTP
clients.

Examples:
- `test_keyring_revocation.py` — `KeyRing`, `RevocationList`, `TokenService`.
- `test_catalog.py` — skill + skillset service functions.
- `test_webui_client.py` — `MCPClient` methods via a patched
  `httpx.AsyncClient` + `MockTransport`.
- `test_cli.py` — Typer app via `CliRunner` + patched `httpx.Client`.
- `test_bundles_fuzz.py` — 300+ random / malicious inputs for
  `extract_archive`; every case must raise `BundleError` or return
  valid `BundleFile`s. No raw `Exception`.

### HTTP integration

One file per router (`test_api_skills.py`, `test_api_skillsets.py`,
`test_api_agents.py`, `test_api_bundles.py`, `test_api_token.py`) and
for the Web UI (`test_webui.py`, `test_webui_auth.py`). Each spins up
the real FastAPI app via `TestClient`, runs against in-memory SQLite,
and exercises the router + middleware + service layer as one. These
are where 401/403/409/413/429 codes are asserted.

### End-to-end

`tests/test_e2e.py`. Builds both the catalog app and the webui app
in-process, wires the webui's `MCPClient` to dispatch into the catalog
via `httpx.ASGITransport` (no sockets), logs in the test operator, and
asserts the full flow: webui page render → MCPClient call → catalog
router → service → DB → response → webui render.

These catch contract drift the per-service tests miss:
- Web UI asks for `/admin/skills` but catalog moved it? Caught.
- Catalog changes a JSON field name? Caught.
- Webui forwards a header the catalog rejects? Caught.

6 tests currently. Each is a distinct user journey — dashboard, skill
listing + skillset filter, modal quick-view, new-version flow write
verified via the catalog's admin API, bundle round-trip, clone
end-to-end.

### Live smoke test

`/tmp/smoke.sh` (tracked in ops notes, not pytest). Requires a running
stack (`make serve` + `make webui` + `.env` with a bcrypt operator).
Verifies 24 user-visible behaviors end-to-end through real curls —
bundle upload, JWT mint, token revocation, operator login, CSRF, etc.

Run before a release. Not run on every commit — TestClient + ASGI
transport already covers the interesting wire paths.

## Running

```bash
make test            # plain pytest
make test-v          # -v
make test-cov        # --cov --cov-report=term-missing (gated at 85%)
pytest tests/test_cli.py -v     # one file
pytest -k "bundle"              # substring match on test names
```

Postgres-gated migration parity tests run when
`MCP_TEST_POSTGRES_URL=postgresql://...` is set; `docker run postgres`
is the canonical way.

## Coverage gate

Configured in `pyproject.toml` under `[tool.coverage.report]`:

```toml
fail_under = 85
```

`pytest --cov` reports coverage; `coverage report` exits non-zero when
total drops below 85%. CI (`ci.yml` → `test-sqlite` job) runs
`pytest --cov --cov-report=term-missing`, so PRs that drop coverage
below the floor fail loudly.

### Exemptions

- Example-runner agent classes (`example/skillful/anthropic_agent.py`
  etc.) get partial coverage because their `.run()` loops need a real
  LLM — the translation + lazy-fetch surface IS covered via
  `test_skillful_agents.py`.
- Migration files are excluded (`migrations/*`) — the migrations
  themselves are tested via `test_migrations.py`'s schema-parity check.

## Conventions

- **One file per module** for unit tests. Cross-cutting concerns
  (observability, rate limits) get their own file.
- **Test class names** match what's being tested:
  `class TestCreateSkill`, `class TestCSRFProtection`. Method names are
  lowercase and descriptive.
- **Fixtures in `conftest.py`** for anything shared; inline fixtures
  only when the test file is the sole user.
- **Deterministic tests.** No `sleep()`. Bucket-refill tests pass an
  injected `now=...`. Bcrypt hashing is per-session (`conftest.py`
  builds the hash fresh), not frozen.
- **Autouse singletons.** `conftest.py` has two autouse fixtures that
  reset cross-test state:
    - `_reset_auth_singleton` — clears `mcp_server.auth._default_service`
      between cases so revocation state and keyring config don't leak.
    - `_reset_bundle_store` — same for `mcp_server.bundles._default_store`.
  Adding a new module-level singleton? Wire it here.

## What this spec doesn't cover yet

- **Load testing** — `k6` / `locust` against a deployed stack,
  throughput + latency SLOs. Tracked as productization §3.8 P1.
- **Real-Postgres in default CI** — the Postgres job runs against a
  service container, but the SQLite matrix is still the default. Once
  the Postgres path is the default deployment target, flip that.
- **Mypy / strict typing** — ruff lints softly today. A dedicated pass
  will tighten rules and add mypy.
- **Hypothesis-based fuzz** — the current fuzz is stdlib-only. Adding
  `hypothesis` would let `extract_archive` be tested against shrinking
  counterexamples. Tracked for when the test team grows.
- **Chaos tests** — network partition, DB drop mid-request, partial S3
  outage. Needs infra (toxiproxy / gremlin). Out of scope until the
  real multi-replica deployment lands.

## Regression discipline

Every bug found in Phase A of a test-hardening pass should land with a
regression test alongside the fix. Wave-8's lifespan-killing
`fileConfig` bug in `migrations/env.py` is the first example; see
`tests/test_migrations.py::test_env_py_does_not_disable_existing_loggers`.
New bugs should get the same treatment.
