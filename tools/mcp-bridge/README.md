# SkillfulMCP ↔ Claude Code bridge

A tiny stdio MCP server that lets Claude Code talk to a deployed
SkillfulMCP catalog. The model decides when to fetch a skill
(via `list_skills` / `get_skill` / `download_skill` tools); the
bridge proxies those calls to your catalog over HTTPS.

See [`spec/integrations/claude-code.md`](../../spec/integrations/claude-code.md)
for the full integration guide and the alternative patterns
(local skill sync, slash command).

## Install

```bash
cd tools/mcp-bridge
pip install -r requirements.txt
```

## Configure

Required env (export before registering with Claude Code):

| Variable | What |
| -------- | ---- |
| `MCP_CATALOG_URL` | e.g. `https://catalog.skillful-mcp.example.com` (no trailing slash needed) |
| `MCP_CATALOG_TOKEN` | Agent JWT (preferred). Mint via `POST /token` |

Optional:

| Variable | What |
| -------- | ---- |
| `MCP_CATALOG_ADMIN_KEY` | Use admin-key auth instead of JWT. Dev only. Required for `require_signature=True` (the JWT response doesn't carry the `verified` flag). |
| `MCP_CATALOG_LOCAL_SKILLS` | Where `download_skill` writes bundles. Default: `~/.claude/skills/` |

## Register with Claude Code

```bash
claude mcp add skillful-bridge \
  -- python /absolute/path/to/tools/mcp-bridge/skillful_bridge.py
```

Or hand-edit `~/.claude.json`:

```json
{
  "mcpServers": {
    "skillful-bridge": {
      "command": "python",
      "args": ["/absolute/path/to/tools/mcp-bridge/skillful_bridge.py"],
      "env": {
        "MCP_CATALOG_URL": "https://catalog.skillful-mcp.example.com",
        "MCP_CATALOG_TOKEN": "<agent JWT>"
      }
    }
  }
}
```

Restart Claude Code. Three tools become available:

- `mcp__skillful-bridge__list_skills`
- `mcp__skillful-bridge__get_skill`
- `mcp__skillful-bridge__download_skill`

## What each tool does

- **`list_skills(query?)`** — search by id/name/description. Returns
  one line per skill. Shows `✓` (signature verified) or `⚠`
  (unverified) when the bridge is admin-key authenticated.
- **`get_skill(skill_id, version?)`** — return the SKILL.md body
  inline. Use this to peek before deciding whether to install.
- **`download_skill(skill_id, version?, require_signature?)`** —
  mirror the bundle into `~/.claude/skills/<skill_id>/` so Claude
  Code's native skill discovery picks it up. Pass
  `require_signature=true` to refuse unverified bundles.

## Auth recipe

1. Provision one agent per developer in the catalog with the
   skillsets they should see.
2. Mint a long-lived token:
   ```bash
   curl -X POST $CATALOG/token \
     -H "X-Admin-Key: $ADMIN" -H "Content-Type: application/json" \
     -d '{"agent_id":"alice-laptop","expires_in":2592000}'   # 30d
   ```
3. Store the token in `~/.config/skillful-mcp/token` with `chmod 600`
   and reference it in `~/.claude.json`'s `env` block.

The catalog's existing `resolve_allowed_skill_ids` does the rest —
the developer only sees skills their agent is granted.
