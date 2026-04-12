# MCP Server Spec

This `spec` directory contains the initial design and architecture for a Minimum Control Plane (MCP) server that serves skills to different agents.

## Purpose

The aim of this project is to explore the possibility of building an MCP server that:
- serves skills to multiple consuming agents,
- uses JWT to control which skills or skillsets an agent is allowed to access,
- limits access for each agent to control the skill context an agent can get.

## Contents

- `architecture.md` — system architecture, components, and request flow.
- `requirements.md` — functional and non-functional requirements.
- `jwt-access-control.md` — JWT design, claims, and access-control rules.
- `agent-model.md` — agent types, capability boundaries, and skill delivery model.
- `prototype.md` — first prototype design using Python + FastAPI + SQLAlchemy + SQLite and CLI options.
- `skill-bundles.md` — storing and serving full skill bundles (SKILL.md plus supporting files) from the catalog.

## Scope

This specification begins the project by defining the key concepts, safety boundaries, and access-control model.
It does not yet define the implementation details for any specific programming language or framework.
