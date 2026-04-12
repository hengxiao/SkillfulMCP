# mcp_server/registry.py

Agent CRUD. Symmetrical with `catalog.py` but much smaller — agents are a
single flat table with no version / bundle concerns.

## Functions

### `create_agent(db, data: AgentCreate) -> Agent`

Insert + flush. `IntegrityError` (primary key collision on `id`) → rollback
→ `ValueError` the router converts to 409.

### `get_agent(db, agent_id) -> Agent | None`

`db.get(Agent, id)`.

### `list_agents(db) -> list[Agent]`

All rows. No pagination (productization §3.3 P0).

### `update_agent(db, agent_id, data: AgentUpdate) -> Agent | None`

Partial update. Each field is mutated only if the incoming value is not
`None`. Returns `None` if the agent doesn't exist → router returns 404.

Note: sending `skillsets=[]` means "set to empty list", not "leave alone".
Use `skillsets=None` (omit the field) to leave unchanged.

### `delete_agent(db, agent_id) -> bool`

`db.delete(agent)` + commit. Returns `False` on miss → 404.

## Boundary with authorization

`registry.py` stores what grants an agent *should have*. `auth.issue_token`
reads from this and bakes those grants into the JWT at mint time. Once a
token is issued, it's self-contained — changes to the agent row do not
propagate to existing tokens.

That's a simplification; productization §3.1 adds a revocation list so
changes here can actually revoke live tokens.

## Testing

`tests/test_registry.py` — 15 tests covering:
- create + conflict on duplicate
- get / list
- partial update (each field independently)
- delete + 404 on missing

## Future work

- Agent identity backed by OIDC subject claim (operators register their
  own keys / service accounts).
- Separate table for skillset/skill grants so history is queryable.
- `scope` enforcement at token validation time (currently stored but not
  checked).
- Tenant scoping on every call.
