# Integrating a deployed SkillfulMCP with Claude Code

After the catalog is reachable on Azure (or wherever you deployed
it — the integration story is the same), the recommended way to
plug it into Claude Code is the **MCP-protocol bridge**: a tiny
stdio server that exposes the catalog as native tools the model
can call on demand. It's the natural fit because Claude Code
already speaks MCP for everything else.

The bridge is shipped as working code in
[`tools/mcp-bridge/`](../../tools/mcp-bridge/). This guide walks
through using it, plus two simpler alternatives for cases the
bridge over-serves.

| Pattern | What Claude Code sees | Best for |
| ------- | --------------------- | -------- |
| **B. MCP bridge** *(recommended)* | Tools `list_skills` / `get_skill` / `download_skill` exposed via the Model Context Protocol | Default. Pull on demand; the model decides what to fetch. |
| **A. Skill sync** | Pre-mirrored `~/.claude/skills/<id>/` directories that Claude Code auto-discovers | Always-on skills + offline use. |
| **C. Slash command** | `/skill <id>` typed at the prompt | Ad-hoc one-off, no persistent install. |

You can mix them. The bridge handles 90% of cases; sync the few
skills you want active by default; keep the slash command as the
escape hatch for ad-hoc installs.

---

## 0. Prereqs — auth identity

The catalog has three identities:

- **Admin key** (`X-Admin-Key`). Process-wide. Use only for batch
  jobs — never on a developer laptop.
- **Operator JWT** issued via `/admin/users/authenticate`. Right
  tier when a developer is logging in.
- **Agent JWT** issued via `POST /token`. Carries the agent's
  grants. **This is what the bridge wants.**

Recommendation: provision **one agent per developer** at deploy
time, mint a long-lived JWT (server caps at
`MCP_MAX_TOKEN_LIFETIME_SECONDS`, default 24h — flip higher for
weekly rotations), store in `~/.config/skillful-mcp/token` with
`chmod 600`.

```bash
curl -X POST $CATALOG/agents \
  -H "X-Admin-Key: $ADMIN" -H 'Content-Type: application/json' \
  -d '{"id":"alice-laptop","name":"Alice laptop",
       "skillsets":["billing","sre"],"scope":["read","execute"]}'

curl -X POST $CATALOG/token \
  -H "X-Admin-Key: $ADMIN" -H 'Content-Type: application/json' \
  -d '{"agent_id":"alice-laptop","expires_in":2592000}'  \
  | jq -r '.access_token' > ~/.config/skillful-mcp/token

chmod 600 ~/.config/skillful-mcp/token
```

---

## B. MCP-protocol bridge — recommended

### B.1 Why it's the right default

- **Always fresh.** No sync window — the model fetches from the
  catalog at the moment the skill is needed.
- **Pull-on-demand.** Big catalogs don't pre-occupy local disk.
- **Per-skill auth flows naturally.** The agent JWT controls what
  the model can see; the catalog already enforces this server-side.
- **Audit out of the box.** Every `download_skill` produces an
  `audit_events` row (Wave 9 item H) — operators get a queryable
  trail of "who installed what when."
- **Discovery is dynamic.** Adding a skill in the catalog makes it
  available to Claude Code immediately, without a re-sync.

### B.2 Install

The bridge lives in [`tools/mcp-bridge/`](../../tools/mcp-bridge/):

```bash
cd tools/mcp-bridge
pip install -r requirements.txt
```

It's ~250 lines of Python plus tests in
[`tests/test_mcp_bridge.py`](../../tests/test_mcp_bridge.py).

### B.3 Register with Claude Code

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
      "args": ["/abs/path/to/tools/mcp-bridge/skillful_bridge.py"],
      "env": {
        "MCP_CATALOG_URL": "https://catalog.skillful-mcp.example.com",
        "MCP_CATALOG_TOKEN": "<paste agent JWT>"
      }
    }
  }
}
```

Restart Claude Code. Three tools become available:

- **`mcp__skillful-bridge__list_skills(query?)`** — search by
  id/name/description. Returns one line per skill.
- **`mcp__skillful-bridge__get_skill(skill_id, version?)`** —
  return SKILL.md inline so the model can read instructions
  without writing anything to disk.
- **`mcp__skillful-bridge__download_skill(skill_id, version?,
  require_signature?)`** — mirror the bundle into
  `~/.claude/skills/<id>/` so Claude Code's native skill discovery
  picks it up. Pass `require_signature=true` to refuse unverified
  bundles (Wave 9 item J).

### B.4 Typical session

The model does roughly:

```
USER: "find me a skill that helps with billing reconciliation"
MODEL → list_skills(query="billing")  →  3 skills
MODEL → get_skill("reconcile-charges") → reads SKILL.md
MODEL → download_skill("reconcile-charges") → installs locally
MODEL → (proceeds with the freshly-installed skill loaded)
```

Each step is one tool call. The user sees only the final answer.

### B.5 Bundle signatures

If your catalog has Wave 9 item J wired (Ed25519 signed bundles),
authenticate the bridge with the admin key instead of an agent JWT
so the `verified` flag is included on detail responses:

```json
"env": {
  "MCP_CATALOG_URL": "https://catalog.skillful-mcp.example.com",
  "MCP_CATALOG_ADMIN_KEY": "<admin key>"
}
```

Then call `download_skill` with `require_signature=true`. The
bridge refuses to install bundles whose signatures didn't verify.

The admin-key path also enables the `✓` / `⚠` markers on
`list_skills` output so the model can avoid recommending an
unverified skill in the first place.

### B.6 Troubleshooting

| Symptom | Likely cause |
| ------- | ------------ |
| Tools don't appear in Claude Code | Bridge process crashed at startup. Run it directly: `MCP_CATALOG_URL=... MCP_CATALOG_TOKEN=... python skillful_bridge.py` and check stderr. |
| `catalog returned 401` from a tool | Token expired or wrong. Mint a fresh one. |
| `catalog returned 403` | Agent grants don't cover that skill. Re-grant via `/admin/agents`. |
| `download_skill` with `require_signature=true` errors `MCP_CATALOG_ADMIN_KEY needed` | `verified` isn't on the JWT path. Switch to admin-key auth as in §B.5. |

---

## A. Skill sync — alternative for always-on skills

Best for a small set of stable skills you want active by default
and able to use offline.

### A.1 Sync command (~30 lines, bash)

Save as `~/bin/mcp-skills-sync` and `chmod +x`:

```bash
#!/usr/bin/env bash
set -euo pipefail
CATALOG="${MCP_CATALOG_URL:-https://catalog.skillful-mcp.example.com}"
TOKEN="$(cat ~/.config/skillful-mcp/token)"
DEST="${HOME}/.claude/skills"
mkdir -p "$DEST"

for id in $(curl -fsS "$CATALOG/skills" \
              -H "Authorization: Bearer $TOKEN" | jq -r '.[].id'); do
  ver=$(curl -fsS "$CATALOG/skills/$id" \
          -H "Authorization: Bearer $TOKEN" | jq -r '.version')
  out="$DEST/$id"
  rm -rf "$out" && mkdir -p "$out"
  for p in $(curl -fsS "$CATALOG/skills/$id/versions/$ver/files" \
               -H "Authorization: Bearer $TOKEN" | jq -r '.[].path'); do
    mkdir -p "$out/$(dirname "$p")"
    curl -fsS -o "$out/$p" \
      "$CATALOG/skills/$id/versions/$ver/files/$p" \
      -H "Authorization: Bearer $TOKEN"
  done
done
```

Run once. Open Claude Code. Skills appear under `~/.claude/skills/`.

### A.2 Auto-refresh

Either cron (`0 * * * * /usr/local/bin/mcp-skills-sync`) or a
Claude Code `SessionStart` hook in `~/.claude/settings.json`:

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

### When sync wins over the bridge

- Need offline access (network drops, air-gapped envs).
- Skills are tiny + few + change rarely.
- You don't trust the bridge process to stay up across sessions.

### When the bridge wins over sync

- Catalog has more skills than you'd want to mirror locally.
- Skills change frequently — re-syncing every hour is wasteful.
- Per-developer access controls are non-trivial — the bridge
  enforces them at fetch time.

---

## C. Slash command — alternative for ad-hoc

Useful as a manual escape hatch even with B running.

`~/.claude/commands/skill.md`:

```markdown
---
description: Install a skill from the deployed SkillfulMCP catalog.
allowed-tools: [Bash]
---

Install skill `$1` (and optional version `$2`). Run:

```
mcp-skills-sync-one $1 ${2:-latest}
```

Then read the resulting SKILL.md and summarize how to use it in
2-3 sentences.
```

Where `mcp-skills-sync-one` is §A.1 parameterized for a single
id. `/skill lookup-invoice` then installs + summarizes in one shot.

---

## Recommended setup

**Solo dev**: just B. Add A or C if and when you hit a case the
bridge over-serves.

**Team rollout**: bake the bridge install + agent JWT provisioning
into the team onboarding script. Use one agent record per person
so audit + revocation work per-developer. Add A as a fallback for
the offline / air-gapped case.

---

## Auth + secret hygiene

- Token in `~/.config/skillful-mcp/token` with `0600`. **Don't**
  put the bare JWT in `~/.claude.json` if that file syncs across
  machines — reference it via the `env` block which can interpolate
  `${HOME}` paths via shell wrapping if needed.
- Rotate tokens on a schedule (`mcp-cli token issue --agent-id <yours>`).
- For an Azure deployment, see
  [`deployment-azure.md`](../deployment-azure.md) §4 — Microsoft
  Entra OIDC for the Web UI, agent JWTs for the bridge.

---

## Operational notes

- **Offline use**: only Pattern A survives losing network access.
  Pattern B's tools fail with a clean error message; Pattern C's
  install errors out at command time.
- **Cache invalidation in B**: the bridge doesn't cache. Repeated
  `get_skill` calls in a session re-fetch each time. Adding an
  in-process LRU is ~10 lines if it matters.
- **Bundle signatures**: see §B.5; require admin-key auth when you
  need the guarantee.
- **Audit**: every `download_skill` hits the catalog and is
  audited via the `audit_events` table — operators see who
  installed what when.

---

## What's NOT in this guide

- **Pushing skills back from Claude Code**: today this is one-way
  (catalog → Claude Code). A reverse path would mean letting
  Claude Code create skills via the catalog. Doable with a
  `publish_skill` tool on the bridge but introduces upload-auth
  questions worth a separate design pass.
- **Multi-catalog setups**: register the bridge twice with
  different env vars to point at multiple catalogs; Claude Code
  scopes them with different MCP server names.
- **Streaming / huge bundles**: bundles over ~100 MB don't fit the
  catalog upload cap. If you need bigger artifacts, point Claude
  Code at the underlying object store directly via a different
  MCP server.
