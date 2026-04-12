# example/cursor/

Cursor is an IDE that consumes tools over the **MCP protocol** (stdio
JSON-RPC). SkillfulMCP is a REST catalog, not an MCP-protocol server, so
plugging it into Cursor requires a small bridging adapter.

This directory does not currently ship a runnable adapter — writing one is
a short exercise using the official [`mcp`](https://pypi.org/project/mcp/)
Python package, but it's a separate process with its own lifecycle and
credentials. `example/cursor/README.md` walks through the shape.

## Bridging shape

1. **Register a Cursor-specific agent** in the catalog with the skillsets
   you want Cursor to see:

   ```bash
   curl -X POST http://localhost:8000/agents \
     -H 'X-Admin-Key: ...' \
     -d '{"id": "cursor-ide", "name": "Cursor", "skillsets": [...], "scope": ["read", "execute"]}'
   ```

2. **Mint a long-lived JWT** for that agent (default 30 days in the doc).

3. **Write a stdio bridge** that implements two MCP handlers:
   - `list_tools()` → call `GET /skills` with the bearer token; translate
     each skill's `metadata.input_schema` into an MCP `tool` record.
   - `call_tool(name, arguments)` → dispatch to the actual skill backend
     (embedded handlers, or a call to the execution gateway pointed at by
     the skill's metadata).

4. **Register the bridge with Cursor** via `~/.cursor/mcp.json`:

   ```json
   {
     "mcpServers": {
       "skillful-mcp": {
         "command": "python",
         "args": ["/abs/path/to/cursor_mcp_adapter.py"],
         "env": {
           "SKILLFUL_MCP_URL": "http://localhost:8000",
           "SKILLFUL_MCP_TOKEN": "eyJ..."
         }
       }
     }
   }
   ```

5. Restart Cursor. The skills appear as tools in Composer / Agent mode,
   scoped exactly by the JWT's skillsets + skills claims.

## Why the catalog doesn't embed this

- The MCP protocol is stdio JSON-RPC; the catalog is HTTP. Supporting both
  in one process complicates lifecycle and auth. A per-client adapter also
  aligns with per-client JWT rotation.
- Skill execution is out of scope for the catalog. The bridge owns (or
  delegates) execution, just like every other framework runner does via
  `common.dispatch_skill` or a real handler.

## Token rotation

JWTs issued by SkillfulMCP are signed but not revocable mid-flight. Options:

- **Manual**: issue a new token, update `mcp.json`, restart Cursor.
- **Automated**: have the adapter refresh via `POST /token` using a
  long-lived admin credential in its env, and re-load on each start.

Productization §3.1 adds a revocation list so a stolen token can be
immediately invalidated; until that lands, keep Cursor JWTs short-lived.

## Future work

- Ship a working `cursor_mcp_adapter.py` under `example/cursor/` once the
  execution model for real skills is nailed down.
- Support the SSE variant of MCP in addition to stdio (useful for
  hosted IDEs).
- A companion `skillful-mcp bridge` CLI subcommand that runs the adapter
  without requiring the user to write Python.
