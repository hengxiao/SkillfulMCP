# SkillfulMCP

A JWT-based skill authorization server for agentic systems. Agents receive signed tokens that declare exactly which skills and skillsets they can access. The server enforces these boundaries on every request.

## Concepts

| Term | Description |
|---|---|
| **Skill** | A versioned, named capability with metadata (description, input/output schema). Versions follow semver. |
| **Skillset** | A named group of skills. Agents are granted access at the skillset level or per individual skill. |
| **Agent** | A registered consumer that holds a list of allowed skillsets and/or skills, plus a scope (`read`, `execute`). |
| **JWT** | A signed token issued to an agent. Claims embed the authorized skillsets, skills, and scope. The server enforces them on every skill-reading endpoint. |

Authorization uses an **additive union model**: an agent can access any skill granted explicitly or via a skillset. There is no deny list.

## Project layout

```
mcp_server/         FastAPI application
  config.py         Settings from environment variables
  models.py         SQLAlchemy 2.0 ORM models
  schemas.py        Pydantic v2 request/response types
  database.py       Engine and session factory
  auth.py           JWT issuance and validation
  authorization.py  Resolves the allowed skill set from token claims
  catalog.py        Skill and skillset CRUD service
  registry.py       Agent CRUD service
  dependencies.py   FastAPI dependency injection helpers
  main.py           App factory (create_app)
  routers/          One module per resource group
    health.py
    token.py
    skills.py
    agents.py
    skillsets.py

cli/
  main.py           Typer CLI (mcp-cli)

tests/              pytest suite (138 tests, all in-memory SQLite)
  conftest.py
  test_auth.py
  test_catalog.py
  test_registry.py
  test_authorization.py
  test_api_token.py
  test_api_skills.py
  test_api_agents.py
  test_api_skillsets.py

example/
  network.yaml      Declarative multi-agent network definition
  run_network.py    Network runner using the Claude API (Anthropic SDK)

spec/               Design documents
  prototype.md      Prototype spec (reviewed and annotated)
```

## Quickstart

### 1. Install

```bash
pip install -e ".[dev]"
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env and set MCP_JWT_SECRET and MCP_ADMIN_KEY
```

Required variables:

| Variable | Description |
|---|---|
| `MCP_JWT_SECRET` | Secret key for signing JWTs. Use a long random string. |
| `MCP_ADMIN_KEY` | Static key for management endpoints (`X-Admin-Key` header). If unset, checks are skipped (dev only). |

Optional:

| Variable | Default | Description |
|---|---|---|
| `MCP_JWT_ISSUER` | `mcp-server` | `iss` claim in issued tokens |
| `MCP_DATABASE_URL` | `sqlite:///./skillful_mcp.db` | SQLAlchemy database URL |

### 3. Run the server

```bash
mcp-server
# or
uvicorn "mcp_server.main:create_app" --factory --reload
```

The API is available at `http://localhost:8000`. Interactive docs at `/docs`.

### 4. Run the tests

```bash
pytest
```

All tests use an in-memory SQLite database. No running server needed.

---

## API overview

### Authentication

- **Agent endpoints** (`GET /skills`, `GET /skillsets/{id}/skills`) — require a `Bearer` JWT in the `Authorization` header.
- **Management endpoints** (all `POST`, `PUT`, `DELETE` routes, plus `GET /agents`) — require an `X-Admin-Key` header.
- `GET /health` — no authentication.

### Endpoints

**Token**

```
POST /token          Issue a JWT for a registered agent (admin only)
```

**Skills**

```
GET    /skills                       List skills authorized by the token (latest versions)
GET    /skills/{id}                  Get a skill (latest, or ?version=x.y.z)
GET    /skills/{id}/versions         List all versions of a skill
POST   /skills                       Create a skill version (admin)
PUT    /skills/{id}                  Upsert a skill version (admin)
DELETE /skills/{id}                  Delete all versions (admin); ?version= targets one
```

**Agents**

```
GET    /agents                       List agents (admin)
GET    /agents/{id}                  Get an agent (admin)
POST   /agents                       Register an agent (admin)
PUT    /agents/{id}                  Update an agent (admin)
DELETE /agents/{id}                  Delete an agent (admin)
```

**Skillsets**

```
GET    /skillsets                    List skillsets (admin)
GET    /skillsets/{id}               Get a skillset (admin)
POST   /skillsets                    Create a skillset (admin)
PUT    /skillsets/{id}               Upsert a skillset (admin)
DELETE /skillsets/{id}               Delete a skillset (admin)
GET    /skillsets/{id}/skills        List authorized skills in a skillset (Bearer JWT)
PUT    /skillsets/{id}/skills/{sid}  Associate a skill with a skillset (admin)
DELETE /skillsets/{id}/skills/{sid}  Remove an association (admin)
```

---

## CLI

```bash
# Add a skill
mcp-cli skill add \
  --id customer-insights \
  --name "Customer Insights" \
  --description "Retrieves CRM data" \
  --version 1.0.0 \
  --skillset sales-assistant

# Register an agent
mcp-cli agent add \
  --id agent-123 \
  --name "Sales Chatbot" \
  --skillsets sales-assistant \
  --scope read,execute

# Issue a token
mcp-cli token issue --agent-id agent-123 --expires-in 3600

# Bulk import from a YAML or JSON file
mcp-cli catalog import --file catalog.yaml --upsert
```

Set `MCP_SERVER_URL` and `MCP_ADMIN_KEY` in your environment before using the CLI.

---

## Example: multi-agent network

The `example/` directory contains a self-contained demonstration of a multi-agent customer support network. The network definition is in `network.yaml`; the runner is `run_network.py`.

**Network topology**

```
User message
     │
     ▼
Intent Router (orchestrator)
   ├── classify_intent skill
   └── route_to_agent meta-tool
          │
          ├── billing-agent       (lookup_invoice, apply_credit)
          └── tech-support-agent  (run_diagnostic, schedule_technician)
```

Each agent holds a JWT scoped to its assigned skillsets. The runner:
1. Bootstraps the MCP catalog from `network.yaml`
2. Issues a JWT per agent via `POST /token`
3. Fetches each agent's authorized skills via `GET /skills`
4. Converts skill metadata into Anthropic tool definitions
5. Runs a tool-use loop where the orchestrator classifies intent and delegates

**Run it**

```bash
# Terminal 1 — start the server
MCP_JWT_SECRET=example-secret MCP_ADMIN_KEY=admin-key mcp-server

# Terminal 2 — run the example
export ANTHROPIC_API_KEY=sk-ant-...
MCP_ADMIN_KEY=admin-key python example/run_network.py \
    --message "My internet has been dropping every night for a week"
```

**Customize the network**

Edit `example/network.yaml` to add agents, change skillsets, or rewrite system prompts. The Python runner does not need to change — it reads everything from the YAML.

---

## Data model notes

- Skill versions use **semver**. Non-semver strings are rejected at ingestion.
- `is_latest` is automatically maintained whenever a skill version is created or deleted.
- Skillset membership is **version-agnostic**: associating `skill-x` with `ss-1` covers all versions of `skill-x`.
- Agent `scope` is a list; valid values are `read` and `execute`.
- The JWT `scope` claim mirrors the agent's scope list.

## Security notes

- `POST /token` is protected by `X-Admin-Key`. Do not expose it publicly.
- `MCP_JWT_SECRET` must be set to a strong random value. The server refuses to start without it.
- The prototype uses SQLite. For production use, set `MCP_DATABASE_URL` to a PostgreSQL or other production-grade database URL.
