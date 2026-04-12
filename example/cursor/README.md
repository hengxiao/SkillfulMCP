# Cursor integration

Cursor is an IDE, not an agent framework — it **consumes** tools through the
[Model Context Protocol](https://modelcontextprotocol.io). SkillfulMCP, despite
the name, is a REST-shaped skill *catalog* (authorization + metadata + bundle
storage), not an MCP-protocol server. To plug it into Cursor you put a tiny
MCP adapter in between that:

1. Connects to the MCP protocol client over stdio (what Cursor speaks).
2. Fetches the list of skills the configured JWT authorizes from SkillfulMCP.
3. Exposes each skill as an MCP `tool`, forwarding arguments on invocation.

This directory does **not** ship a working adapter — writing one is a
straightforward exercise using the official
[`mcp`](https://pypi.org/project/mcp/) Python package, but it's a separate
service with its own lifecycle. What follows is the exact sketch.

## 1. Issue a JWT for Cursor

Register a Cursor-specific agent in the catalog, give it the skillsets you
want Cursor to see, and mint a long-lived token:

```bash
# Create the agent
curl -X POST http://localhost:8000/agents \
  -H 'X-Admin-Key: YOUR_ADMIN_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"id":"cursor-ide","name":"Cursor","skillsets":["billing","technical-support"],"scope":["read","execute"]}'

# Mint a token
curl -X POST http://localhost:8000/token \
  -H 'X-Admin-Key: YOUR_ADMIN_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"cursor-ide","expires_in":2592000}'   # 30 days
```

Store the returned `access_token` somewhere your adapter can read it (env var,
keychain, 1Password CLI, etc.).

## 2. Write the adapter

Minimal shape (pseudo-code — see [`mcp` SDK docs](https://github.com/modelcontextprotocol/python-sdk)
for the real APIs, which evolve):

```python
# cursor_mcp_adapter.py
import os, json, httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server

CATALOG = os.environ["SKILLFUL_MCP_URL"]          # e.g. http://localhost:8000
TOKEN   = os.environ["SKILLFUL_MCP_TOKEN"]        # agent JWT from step 1

server = Server("skillful-mcp-bridge")

@server.list_tools()
async def list_tools():
    r = httpx.get(f"{CATALOG}/skills",
                  headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10)
    r.raise_for_status()
    out = []
    for skill in r.json():
        schema = (skill.get("metadata") or {}).get("input_schema") \
                 or {"type": "object", "properties": {}}
        out.append({
            "name": skill["id"].replace("-", "_"),
            "description": skill.get("description") or skill["name"],
            "inputSchema": schema,
        })
    return out

@server.call_tool()
async def call_tool(name: str, arguments: dict):
    # Map `name` back to a real skill invocation — either:
    #  (a) a side-channel HTTP call to whatever service owns the skill, or
    #  (b) a call to a gateway that the catalog's metadata points at.
    # This bridge does not own the execution — the catalog is metadata only.
    raise NotImplementedError(
        "Wire this to your actual skill backend. Use `name` to pick a handler."
    )

if __name__ == "__main__":
    stdio_server(server).run()
```

Key point: SkillfulMCP gives the adapter the **list** of skills the JWT
authorizes, but the catalog doesn't execute skills itself. Your adapter must
either embed the handlers or call an execution gateway, exactly the same way
the existing framework examples (`anthropic_sdk`, `openai_sdk`, `langchain_app`,
`langgraph_app`) use the shared `dispatch_skill` stub under `common/`.

## 3. Register the adapter with Cursor

Edit `~/.cursor/mcp.json` (or use Cursor's MCP settings UI):

```json
{
  "mcpServers": {
    "skillful-mcp": {
      "command": "python",
      "args": ["/absolute/path/to/cursor_mcp_adapter.py"],
      "env": {
        "SKILLFUL_MCP_URL": "http://localhost:8000",
        "SKILLFUL_MCP_TOKEN": "eyJhbGciOiJIUzI1Ni..."
      }
    }
  }
}
```

Restart Cursor. In Composer / Agent mode you should now see the skills
exposed as tools, scoped exactly by the JWT's skillsets/skills claims —
which is the whole point of running skills through SkillfulMCP instead of
hard-coding them into the adapter.

## Rotating the token

JWTs issued by SkillfulMCP are signed but not revocable mid-flight. To rotate:
issue a new token, update `SKILLFUL_MCP_TOKEN` in `~/.cursor/mcp.json`,
restart the adapter. For more aggressive rotation, issue short-lived tokens
and have the adapter refresh via `POST /token` using a long-lived admin
credential kept in the adapter's env — out of scope for this starter.
