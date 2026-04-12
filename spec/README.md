# MCP Server Spec

This `spec` directory contains the initial design and architecture for a Minimum Control Plane (MCP) server that serves skills to different agents.

## Purpose

The aim of this project is to explore the possibility of building an MCP server that:
- serves skills to multiple consuming agents,
- uses JWT to control which skills or skillsets an agent is allowed to access,
- limits access for each agent to control the skill context an agent can get.

## Contents

Core design:

- `architecture.md` — system architecture, components, and request flow.
- `requirements.md` — functional and non-functional requirements.
- `jwt-access-control.md` — JWT design, claims, and access-control rules.
- `agent-model.md` — agent types, capability boundaries, and skill delivery model.

Implementation (high-level):

- `prototype.md` — first prototype using Python + FastAPI + SQLAlchemy + SQLite.
- `skill-bundles.md` — storing and serving full skill bundles (SKILL.md plus supporting files).
- `webui.md` — browser-based management interface (sidebar nav, quick-view modals, immutable-version workflow, clone-to-rename, bundle file viewer with syntax highlighting).
- `example-frameworks.md` — how Anthropic SDK / OpenAI / LangChain / LangGraph / Cursor consume the catalog; layout of the per-framework runners under `example/`.
- `productization.md` — gap analysis and action plan for turning the prototype into a deployable, multi-tenant cloud service (identity, storage, scaling, observability, rollout milestones).

Per-submodule specs (implementation-level, one file per source module):

- [`mcp_server/`](mcp_server/) — FastAPI catalog server: config, database, auth, authorization, models, schemas, catalog / registry / bundles services, and [one router-module spec each](mcp_server/routers/).
- [`webui/`](webui/) — Web UI: app factory + routes, async MCP client, config, Jinja template inventory.
- [`cli/`](cli/) — `mcp-cli` Typer app.
- [`example/`](example/) — shared helpers, `SkillfulXxxAgent` classes, per-framework runners, Cursor bridging notes.

## Scope

These specifications start with the key concepts, safety boundaries, and
access-control model, then describe the prototype implementation choices that
followed: bundle storage, a web UI on top of the catalog, and framework-agnostic
agent runners that demonstrate the catalog is consumable by any tool-calling
framework through its existing HTTP surface.
