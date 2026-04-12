# Prototype Spec

This document describes the first prototype for the MCP server using Python + FastAPI + SQLAlchemy with SQLite.

## Goals

- Validate the JWT-based skill authorization model.
- Build a minimal backend that can serve skill metadata and enforce access control.
- Support skill versioning and agent-scoped responses.
- Provide a CLI mode for local catalog management and token generation.

## Implementation Stack

- Python 3.11+ (or latest supported Python 3.x)
- FastAPI for the HTTP API
- SQLAlchemy for ORM and SQLite for local prototyping
- Pydantic for request/response validation
- PyJWT or `python-jose` for JWT handling
- Alembic optionally for schema migrations if needed later

## Core Components

### 1. Skill Catalog Service

Stores:
- skill id
- name
- description
- version
- metadata
- skillset membership
- created / updated timestamps

Supports:
- retrieving latest skill metadata
- retrieving skill metadata by version
- listing skills within skillsets

Version resolution: "latest" is determined by semver ordering on the `version` field. The catalog must reject non-semver version strings at ingestion time so ordering is unambiguous.

### 1.1. Skill Ingestion and Onboarding

The prototype should support adding skills into the system through:
- CLI commands that create or update skill records directly in the SQLite catalog
- HTTP POST endpoints for skill ingestion or manifest upload
- bulk import from JSON/YAML files for offline catalog population

Onboarding behavior:
- new skill versions should be added as new rows with the same `id` and a new `version`
- duplicate `id` + `version` combinations should be rejected
- skillset associations should be validated when a skill is created or updated
- import operations should optionally upsert skills to support iterative versioning

### 2. Agent Registry

Stores:
- agent id
- agent name
- allowed skillsets
- allowed skill ids
- allowed scopes (`read`, `execute`, etc.) — stored as a list, not a single string

### 3. JWT Issuer and Validator

Responsibilities:
- issue signed JWTs for agents
- embed claims: `sub`, `iss`, `exp`, `iat`, `skillsets`, `skills`, `scope`
- validate token signature, expiry, issuer, and claims

### 4. Authorization Engine

Responsibilities:
- resolve the set of skills allowed by the token
- restrict responses to authorized skill metadata only
- deny unauthorized skill requests
- optionally log decisions for auditing

Conflict resolution: skillset-level grants and explicit skill-level grants are additive — the union of both sets is the allowed set. There is no deny list in this model; access is allowed if the token grants it through either path.

### 5. API Endpoints

Example HTTP endpoints:

**Token**
- `POST /token` — issue agent JWT (prototype only; restrict to localhost or require a shared admin secret — this is the highest-risk endpoint and must not be publicly open)

**Skills**
- `GET /skills` — list skills available to the requesting agent
- `GET /skills/{skill_id}` — get the latest version of a skill's metadata
- `GET /skills/{skill_id}?version={version}` — get a specific version
- `GET /skills/{skill_id}/versions` — list all versions of a skill
- `POST /skills` — create a new skill record (reject duplicate `id` + `version`)
- `PUT /skills/{skill_id}` — upsert a skill (replace or create; use `?version=` to target a specific version)
- `DELETE /skills/{skill_id}` — delete all versions of a skill by default; use `?version=` to delete a specific version only

**Agents**
- `GET /agents` — list registered agents
- `GET /agents/{agent_id}` — get agent metadata
- `POST /agents` — register a new agent
- `PUT /agents/{agent_id}` — update agent metadata
- `DELETE /agents/{agent_id}` — remove an agent

**Skillsets**
- `GET /skillsets/{skillset_id}/skills` — list skills for a skillset
- `POST /skillsets` — create a skillset
- `PUT /skillsets/{skillset_id}` — update a skillset
- `DELETE /skillsets/{skillset_id}` — delete a skillset
- `PUT /skillsets/{skillset_id}/skills/{skill_id}` — associate a skill with a skillset
- `DELETE /skillsets/{skillset_id}/skills/{skill_id}` — remove a skill from a skillset

**Health**
- `GET /health` — liveness check, no auth required

Alternative design note:
- If the system should support partial updates, consider `PATCH /skills/{skill_id}` and `PATCH /skillsets/{skillset_id}`.
- Management endpoints (`POST /skills`, `PUT /skills/{skill_id}`, `DELETE /skills/{skill_id}`, and equivalents for agents and skillsets) should require a separate admin credential in production. For the prototype, a static API key passed as a header (`X-Admin-Key`) is sufficient.

### 6. CLI Support

The CLI should allow:
- creating and updating skills
- creating and updating agents
- issuing JWTs for a given agent
- exporting/importing the catalog data

Example CLI commands:
- `mcp-cli skill add --id customer-insights --name "Customer Insights" --description "Retrieves CRM data" --version 1.0.0 --skillset sales-assistant`
- `mcp-cli agent add --id agent-123 --name "chatbot" --skillsets sales-assistant --scope read,execute`
- `mcp-cli token issue --agent-id agent-123 --expires-in 3600`

## Data Model

### Skill

- `id`: string
- `name`: string
- `description`: string
- `version`: string — must be a valid semver string; composite unique constraint on (`id`, `version`)
- `is_latest`: bool — maintained by the catalog on insert/update; marks the highest semver row for a given `id`
- `metadata`: JSON field — intended for tool invocation hints, tags, and schema definitions; structure TBD but should be validated as a JSON object at ingestion time
- `created_at`: datetime
- `updated_at`: datetime

Note: `skillset_ids` is not a column on this table. Skillset membership is owned by the `SkillSkillset` association table (see below).

### SkillSkillset

Join table linking skills to skillsets. Owns the many-to-many relationship.

- `skill_id`: string (FK → Skill.id)
- `skill_version`: string (FK → Skill.version) — optional; if null, the association applies to all versions of the skill
- `skillset_id`: string (FK → Skillset.id)

### Skillset

- `id`: string
- `name`: string
- `description`: string
- `created_at`: datetime
- `updated_at`: datetime

### Agent

- `id`: string
- `name`: string
- `skillsets`: list[string]
- `skills`: list[string]
- `scope`: list[string] — e.g. `["read", "execute"]`; must match the set of valid scope values
- `created_at`: datetime
- `updated_at`: datetime

### JWT Claims

- `sub` — agent id
- `iss` — `mcp-server`
- `exp` — expiration timestamp
- `iat` — issued-at timestamp
- `skillsets` — authorized skillset ids
- `skills` — authorized skill ids
- `scope` — list of permission modes granted (e.g. `["read", "execute"]`)
- `context` — optional metadata

## SQLite for Prototype

- Use SQLite for simplicity and fast iteration.
- Keep schema normalized enough to support skill versions and memberships.
- Store versioned skills as separate rows, with a composite unique constraint on `id` + `version`.
- Use SQLAlchemy `relationship` objects for skillset membership.
- CLI access to the database: the CLI should talk to the API (not directly to the SQLite file) when the server is running, to avoid SQLite write-lock contention. Direct DB access from the CLI is only appropriate during offline catalog population (e.g. bulk import before server start).
- JWT signing key: store the secret in an environment variable (`MCP_JWT_SECRET`). The server reads it at startup and raises a fatal error if it is unset. Do not hardcode or commit it.

## External Dependencies

- `fastapi`
- `uvicorn`
- `sqlalchemy`
- `pydantic`
- `python-jose` or `PyJWT`
- `alembic` (optional)
- `typer` or `click` for CLI interface

## Prototype Scope

This first prototype should focus on:
- data modeling for skills, versions, and agents
- signed JWT generation and validation
- authorization filtering based on token claims
- a small HTTP API surface with protected endpoints
- a CLI to manage catalog data and issue tokens

## Review Points

- confirm the schema layout and versioning strategy — semver ordering and `is_latest` flag approach assumed here
- validate the authorization flow for skillset + skill claims — additive union model assumed; revisit if deny semantics are needed later
- confirm that the `SkillSkillset` association is version-aware (per-version FK) vs. version-agnostic (id only); the spec currently supports both via the nullable `skill_version` column
- confirm the `metadata` field structure — what schema or shape is expected at minimum for a skill to be usable
- determine whether `POST /token` needs even a lightweight guard for the prototype (e.g. IP restriction, static admin key)
- confirm valid values for `scope` — define the closed list before implementation so validation is consistent across the agent model, JWT claims, and authorization engine
