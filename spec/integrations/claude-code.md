# Integrating a deployed SkillfulMCP with Claude Code

After the catalog is reachable on Azure (or wherever you deployed
it — the integration story is the same), there are three viable
ways for **Claude Code** running on a developer machine to consume
the catalog. Pick by what you want Claude Code to do with the
skills.

| Pattern | What Claude Code sees | Best for |
| ------- | --------------------- | -------- |
| **A. Skill sync** | Local `~/.claude/skills/<id>/` directories that Claude Code auto-discovers | Stable, infrequently-changing skill bundles you want active by default |
| **B. MCP bridge** | An MCP server registered with Claude Code that exposes tools like `list_skills`, `get_skill_bundle` | "Pull on demand" — the model decides when to fetch a skill mid-session |
| **C. Slash command** | `/skill <id>` typed at the prompt | One-off ad-hoc skill use, no persistent install |

You can mix them: sync the always-on skills (A), bridge the rest
(B), and keep `/skill add` as the escape hatch (C).

This guide walks each pattern end-to-end against a deployed catalog
running at `https://catalog.skillful-mcp.example.com`.

---

## 0. Prereqs — pick an auth identity

The catalog has three identities:

- **Admin key** (`X-Admin-Key`). Process-wide credential, talks to
  every `/admin/*` endpoint. Use this for batch sync jobs only —
  never on a developer laptop.
- **Operator JWT** issued via `/admin/users/authenticate` and
  scoped via the Wave 9 account model. Right tier for a developer
  who logs in and pulls "skills my account has access to."
- **Agent JWT** issued via `POST /token` for a registered agent.
  Carries the agent's grants; perfect for Path B since the bridge
  is itself an agent the developer represents.

Recommendation: provision **one agent per developer** at deploy
time, mint a long-lived JWT per agent (server-side cap is 24h by
default — flip `MCP_MAX_TOKEN_LIFETIME_SECONDS` higher if you need
weekly rotations). Store the JWT in `~/.config/skillful-mcp/token`
with `chmod 600`.

For Patterns A and B below, the call paths assume an agent JWT.
Substitute your operator JWT / admin key as needed.

---

## A. Skill sync — `claude.skills/` from the catalog

**What you ship**: a `mcp-cli skills sync` subcommand (or a tiny
shell wrapper) that hits the catalog and writes one folder per
skill into `~/.claude/skills/`.

### A.1 Layout Claude Code expects

```
~/.claude/skills/
├── lookup-invoice/
│   ├── SKILL.md              ← Claude Code reads this
│   └── helpers/
│       └── format.py
└── deploy-checklist/
    └── SKILL.md
```

Claude Code auto-discovers anything in `~/.claude/skills/<name>/`
with a `SKILL.md` at the root.

### A.2 Sync command (zsh / bash, ~30 lines)

Save as `~/bin/mcp-skills-sync` and `chmod +x`:

```bash
#!/usr/bin/env bash
set -euo pipefail

CATALOG="${MCP_CATALOG_URL:-https://catalog.skillful-mcp.example.com}"
TOKEN="$(cat ~/.config/skillful-mcp/token)"
DEST="${HOME}/.claude/skills"

mkdir -p "$DEST"

# 1. List every skill the JWT can see (latest version each).
ids=$(curl -fsS "$CATALOG/skills" \
        -H "Authorization: Bearer $TOKEN" \
      | jq -r '.[].id')

for id in $ids; do
  echo "syncing $id"
  # 2. Resolve the latest version.
  ver=$(curl -fsS "$CATALOG/skills/$id" \
          -H "Authorization: Bearer $TOKEN" \
        | jq -r '.version')

  # 3. Pull the file list.
  paths=$(curl -fsS "$CATALOG/skills/$id/versions/$ver/files" \
            -H "Authorization: Bearer $TOKEN" \
          | jq -r '.[].path')

  # 4. Mirror each file into the local skill dir.
  out="$DEST/$id"
  rm -rf "$out"
  mkdir -p "$out"
  for p in $paths; do
    mkdir -p "$out/$(dirname "$p")"
    curl -fsS -o "$out/$p" "$CATALOG/skills/$id/versions/$ver/files/$p" \
      -H "Authorization: Bearer $TOKEN"
  done
done

echo "synced $(echo "$ids" | wc -w) skills into $DEST"
```

Run it once: `mcp-skills-sync`. Open Claude Code; the skills are
now available.

### A.3 Make it automatic

Two options:

**Cron / launchd** — run the sync every hour:

```cron
0 * * * * /usr/local/bin/mcp-skills-sync >> ~/.cache/mcp-sync.log 2>&1
```

**Claude Code session-start hook** — fresh skills on every Claude
Code session. Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [{ "type": "command", "command": "mcp-skills-sync" }]
      }
    ]
  }
}
```

`mcp-skills-sync` runs before the model gets its first turn; if it
takes >2 seconds, run it in the background instead
(`mcp-skills-sync &`).

### A.4 Verify a bundle signature (item J integration)

Skip this if you haven't enabled Wave 9 item J (Ed25519 bundle
signatures). When you have:

```bash
# Inside the loop above, after fetching the bundle but before
# writing it under ~/.claude/skills/:
sig=$(curl -fsS "$CATALOG/admin/skills/$id" -H "X-Admin-Key: $KEY" \
        | jq -r '.bundle_signature // empty')
verified=$(curl -fsS "$CATALOG/admin/skills/$id" -H "X-Admin-Key: $KEY" \
        | jq -r '.verified // false')
if [ "$verified" != "true" ]; then
  echo "REFUSING to install $id — signature did not verify" >&2
  rm -rf "$out"
  continue
fi
```

(Uses the admin-key path because `verified` is computed on
`/admin/skills/{id}` — the agent-JWT GET doesn't include it. A
follow-up wave can surface `verified` on the JWT path too.)

### Pros + cons
- ✅ Zero per-session model overhead.
- ✅ Works completely offline once synced.
- ✅ Familiar mental model — "files in a folder."
- ❌ Stale until the next sync; bad for rapidly-evolving content.
- ❌ Pulls everything the JWT can see; large catalogs become
  expensive to mirror.

---

## B. MCP-protocol bridge — pull on demand

This is the natural fit for catalogs with **lots** of skills where
Claude Code should fetch just the relevant ones per task.

### B.1 What the bridge does

A small Python process that:

1. Connects to your deployed catalog over HTTPS using the agent JWT.
2. Speaks the [Model Context Protocol](https://modelcontextprotocol.io)
   over stdio.
3. Exposes three tools to Claude Code:
   - `list_skills(query?: str)` — search the catalog by id /
     name / description.
   - `get_skill(skill_id, version?)` — return the SKILL.md body
     (so the model can read instructions inline).
   - `download_skill(skill_id, version?, dest_path?)` — dump the
     full bundle into `~/.claude/skills/<id>/` so subsequent
     turns have it available locally.

### B.2 The bridge code (sketch, ~120 lines)

`tools/mcp-bridge/skillful_bridge.py`:

```python
"""MCP server that bridges Claude Code to a SkillfulMCP catalog."""
from __future__ import annotations

import os
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

CATALOG = os.environ["MCP_CATALOG_URL"].rstrip("/")
TOKEN = os.environ["MCP_CATALOG_TOKEN"]
LOCAL_SKILLS = Path.home() / ".claude" / "skills"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

server = Server("skillful-mcp-bridge")


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="list_skills",
            description="Search the SkillfulMCP catalog. "
                        "Pass an optional substring to filter.",
            inputSchema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        ),
        Tool(
            name="get_skill",
            description="Return the SKILL.md body for a skill id "
                        "without writing it to disk.",
            inputSchema={
                "type": "object",
                "required": ["skill_id"],
                "properties": {
                    "skill_id": {"type": "string"},
                    "version": {"type": "string"},
                },
            },
        ),
        Tool(
            name="download_skill",
            description="Mirror the full bundle into "
                        "~/.claude/skills/<id>/ so Claude Code "
                        "can use it natively.",
            inputSchema={
                "type": "object",
                "required": ["skill_id"],
                "properties": {
                    "skill_id": {"type": "string"},
                    "version": {"type": "string"},
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    async with httpx.AsyncClient(headers=HEADERS, timeout=15) as c:
        if name == "list_skills":
            r = await c.get(f"{CATALOG}/skills")
            r.raise_for_status()
            rows = r.json()
            q = (arguments.get("query") or "").lower()
            if q:
                rows = [s for s in rows
                        if q in s["id"].lower() or q in s["name"].lower()
                        or q in (s.get("description") or "").lower()]
            return [TextContent(
                type="text",
                text="\n".join(f"{s['id']} ({s['version']}) — {s['name']}"
                               for s in rows[:50]),
            )]

        if name == "get_skill":
            sid = arguments["skill_id"]
            ver = arguments.get("version")
            url = f"{CATALOG}/skills/{sid}"
            if ver:
                url += f"?version={ver}"
            meta = (await c.get(url)).json()
            ver = meta["version"]
            r = await c.get(
                f"{CATALOG}/skills/{sid}/versions/{ver}/files/SKILL.md")
            r.raise_for_status()
            return [TextContent(type="text", text=r.text)]

        if name == "download_skill":
            sid = arguments["skill_id"]
            ver = arguments.get("version")
            url = f"{CATALOG}/skills/{sid}"
            if ver:
                url += f"?version={ver}"
            ver = (await c.get(url)).json()["version"]
            files = (await c.get(
                f"{CATALOG}/skills/{sid}/versions/{ver}/files")).json()
            dest = LOCAL_SKILLS / sid
            dest.mkdir(parents=True, exist_ok=True)
            for f in files:
                p = dest / f["path"]
                p.parent.mkdir(parents=True, exist_ok=True)
                r = await c.get(
                    f"{CATALOG}/skills/{sid}/versions/{ver}/files/{f['path']}")
                p.write_bytes(r.content)
            return [TextContent(
                type="text",
                text=f"installed {sid} {ver} → {dest} ({len(files)} files)",
            )]

        raise ValueError(f"unknown tool: {name}")


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

`requirements.txt`:

```
mcp>=1.0
httpx
```

### B.3 Register with Claude Code

```bash
claude mcp add skillful-bridge \
  -- python /path/to/tools/mcp-bridge/skillful_bridge.py \
  --env MCP_CATALOG_URL=https://catalog.skillful-mcp.example.com \
  --env MCP_CATALOG_TOKEN="$(cat ~/.config/skillful-mcp/token)"
```

Or hand-edit `~/.claude.json` (the equivalent settings entry):

```json
{
  "mcpServers": {
    "skillful-bridge": {
      "command": "python",
      "args": ["/Users/you/tools/mcp-bridge/skillful_bridge.py"],
      "env": {
        "MCP_CATALOG_URL": "https://catalog.skillful-mcp.example.com",
        "MCP_CATALOG_TOKEN": "<your agent JWT>"
      }
    }
  }
}
```

Restart Claude Code. The model now has `mcp__skillful-bridge__list_skills`,
`get_skill`, `download_skill` available as tools.

### Pros + cons
- ✅ Always fresh — no sync window.
- ✅ Pulls only what the model actually decides to use.
- ✅ Works for huge catalogs without local disk pressure.
- ❌ Adds tool-call latency (model has to decide → call → wait).
- ❌ Requires the bridge process to be running each session
  (Claude Code spawns it via `command:` automatically; that's fine
  but the env vars must be available there).

---

## C. Slash command — ad-hoc skill installs

Useful as a manual escape hatch even if you also run A or B.

`~/.claude/commands/skill.md`:

```markdown
---
description: Install a skill from the deployed SkillfulMCP catalog.
allowed-tools: [Bash]
---

Install skill `$1` (and optional version `$2`) from the deployed
SkillfulMCP catalog into ~/.claude/skills/. After the install,
remind me of the SKILL.md instructions so I can use it
immediately.

Run:

```
mcp-skills-sync-one $1 ${2:-latest}
```

Then read the resulting SKILL.md and summarize how to use the
skill in 2-3 sentences.
```

Where `mcp-skills-sync-one` is the same shell as in §A.2 but
parameterized for a single skill id. Now `/skill lookup-invoice`
inside Claude Code installs + summarizes in one shot.

---

## 1. Recommended starting setup

For a single developer:

1. **Phase 1**: bootstrap with §A. Run `mcp-skills-sync` once;
   verify Claude Code sees the skills.
2. **Phase 2**: add the §C slash command for ad-hoc cases.
3. **Phase 3**: deploy §B once the catalog is large enough that
   syncing everything starts hurting (>50 skills or >100 MB).

For a team:

- Bake `mcp-skills-sync` into the team's onboarding script.
- Provision the agent JWT centrally (each developer has their
  own agent record so audits and revocations work per-person).
- Pin the bridge version + auth env to the team's dotfiles repo.

---

## 2. Auth + secret hygiene

- Store the JWT in `~/.config/skillful-mcp/token` with `0600`
  permissions. **Don't** put it in `~/.claude.json` — that file
  often syncs across machines.
- Use the `--env` flag (or the env block in `mcpServers`) to
  inject the token into the bridge process; doesn't end up in
  the model's view.
- Rotate the agent JWT on a schedule:
  `mcp-cli token issue --agent-id <yours> --expires-in 86400`.
- For an org-wide rollout, pair this guide with §4 of
  [`deployment-azure.md`](../deployment-azure.md) — Microsoft Entra
  OIDC for the Web UI, agent JWTs for Claude Code.

---

## 3. Operational notes

- **Offline use**: Pattern A is the only one that survives
  losing network access. Pattern B silently fails the tool calls;
  Pattern C errors out at install time.
- **Cache invalidation**: Pattern B doesn't cache by default. If
  the model calls `get_skill` repeatedly in one session, you're
  paying the round-trip each time. Adding an in-process LRU is
  ~10 lines.
- **Bundle signatures (item J)**: extend the §A.4 verify check to
  Pattern B before calling `download_skill` — refuse to install an
  unverified bundle.
- **Permissions**: the agent JWT scopes which skills the catalog
  returns. To restrict a developer to "billing skills only", give
  their agent a `skillsets=['billing']` grant; the existing
  `resolve_allowed_skill_ids` does the rest.
- **Audit**: every `download_skill` hits the catalog and is
  audited (Wave 9 item H). Operators get a queryable trail of
  "who installed what when."

---

## 4. What's NOT in this guide (yet)

- **Pushing skills back from Claude Code**: today this is one-way
  (catalog → Claude Code). A reverse path would mean letting Claude
  Code create skills via the catalog. Doable with a `publish_skill`
  tool on the bridge but introduces an upload-auth question worth
  its own design pass.
- **Multi-catalog setups**: the bridge as written points at one
  catalog. For a developer with skills across multiple deployments,
  register the bridge twice with different env vars; Claude Code
  scopes them with different MCP server names.
- **Streaming / large bundles**: bundles over ~100 MB don't fit
  the `--bundle` upload cap and shouldn't really be skills. If you
  need bigger artifacts, point Claude Code at the underlying object
  store directly via a separate MCP server.
