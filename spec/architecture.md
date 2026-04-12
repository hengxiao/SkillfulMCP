# Architecture

## High-Level Overview

The MCP server sits between agent clients and the skill store. It authenticates agents with JWTs, authorizes skill access, and returns only the allowed context.

### Core Components

- Agent Registry
  - Stores agent identities and associated permissions.
  - Defines which skillsets each agent may access.

- JWT Issuer
  - Generates signed tokens for registered agents.
  - Embeds authorized skill and skillset claims.

- Skill Catalog
  - Stores metadata for skills and skillsets.
  - Tracks skill versions, skill descriptions, required context, and relationships.

- Authorization Engine
  - Validates incoming JWTs.
  - Checks token claims against requested skills.
  - Filters returned data to the allowed subset.

- Skill Delivery API
  - Exposes endpoints for agent requests.
  - Serves skill information and context guided by authorization decisions.

## Request Flow

1. An agent requests a JWT or is pre-provisioned with one.
2. The agent makes a request to the MCP server, including the JWT in the request header.
3. The server validates the JWT signature and checks expiry.
4. The authorization engine inspects claims for permitted skillsets or individual skills.
5. The server retrieves the requested skill metadata from the catalog.
6. The server filters the response to include only authorized skill details.
7. The server returns the filtered skill context to the agent.

## Example Flow

- Agent A is authorized for skillset `sales-assistant`.
- Agent A requests skill metadata for `customer-insights`.
- The server verifies that `customer-insights` belongs to `sales-assistant`.
- The server responds with allowed metadata, omitting any unrelated or restricted fields.

## Security Boundary

The JWT validation and authorization engine form the trust boundary. The MCP server must treat all incoming requests as untrusted until successfully authenticated and authorized.
