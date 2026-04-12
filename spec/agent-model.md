# Agent Model

This document describes how agents interact with the MCP server and the boundaries of access control.

## Agent Types

- `chat-agent`
  - Consumes conversational skills and receives context-limited skill data.
- `workflow-agent`
  - Uses broader skillsets for task orchestration.
- `analytics-agent`
  - Accesses reporting and insight skills within a restricted context.

## Agent Capabilities

Each agent is bound to a set of capabilities through its JWT:

- authorized skillsets
- authorized individual skills
- allowed context metadata
- allowed operations (`read`, `execute`, `configure`)

## Skillsets vs Skills

- Skillset
  - A named collection of related skills.
  - Easier to grant broad permissions.
- Skill
  - A specific capability or action.
  - Use when permission must be tightly scoped.

## Use Cases

1. Agent A needs only `sales-assistant` skills.
   - JWT includes `skillsets: ["sales-assistant"]`.
   - MCP returns only skill context for that set.

2. Agent B needs a specific admin skill.
   - JWT includes `skills: ["user-management"]`.
   - MCP allows only that skill and rejects unrelated requests.

3. Agent C should not see skill context from other agents.
   - MCP filters the response strictly by claims.
   - No cross-agent skill listing is allowed.

## Future Considerations

- Add per-agent rate limiting and quota enforcement.
- Support multi-tenant skill catalogs.
- Introduce dynamic policy evaluation for runtime access decisions.
- Support skill versioning and version-aware delivery so agents can request specific skill releases.
