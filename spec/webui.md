# Web UI Spec

Browser-based management interface for the SkillfulMCP server.

## Goals

- Let operators create, view, and delete skills, skill versions, and skillsets without the CLI.
- Make skill versions **immutable** — edits are expressed as new versions, not in-place updates.
- Make skill **names** immutable within a skill id — renaming requires cloning to a new skill.
- Browse and inspect each skill's bundle files (markdown, code, etc.) inline.
- Stay readable on mobile (hamburger-collapsed navigation).

## Stack

| Layer         | Choice                 | Reason                                               |
| ------------- | ---------------------- | ---------------------------------------------------- |
| Web server    | FastAPI (Python)       | Consistent with the rest of the project              |
| Templating    | Jinja2                 | Server-rendered HTML; no build step                  |
| Interactivity | HTMX 1.9               | Modal partials and inline deletes without a framework|
| Styling       | Bootstrap 5.3 (CDN)    | Fast layout; no build step                           |
| Code viewer   | highlight.js 11 + marked.js 12 | Syntax highlighting + Markdown rendering     |
| HTTP client   | httpx (async)          | Calls the MCP server API                             |

The Web UI is a **standalone FastAPI process** that proxies all data operations to the MCP server API. It does not touch the database directly.

## Configuration

| Variable         | Default                  | Description                                               |
| ---------------- | ------------------------ | --------------------------------------------------------- |
| `MCP_SERVER_URL` | `http://localhost:8000`  | Base URL of the MCP server                                |
| `MCP_ADMIN_KEY`  | _(empty)_                | Admin key forwarded as `X-Admin-Key` on every proxied call|
| `WEBUI_HOST`     | `127.0.0.1`              | Bind host                                                 |
| `WEBUI_PORT`     | `8080`                   | Port                                                      |

Reuses the same `.env` file as the MCP server.

## MCP server additions used by the Web UI

Because the Web UI doesn't hold a per-agent JWT, the MCP server exposes admin-key-protected read endpoints:

| Endpoint                                                              | Purpose                                           |
| --------------------------------------------------------------------- | ------------------------------------------------- |
| `GET /admin/skills`                                                   | Latest version of every skill                     |
| `GET /admin/skills/{skill_id}?version=`                               | A specific version (or latest)                    |
| `GET /admin/skills/{skill_id}/versions`                               | All versions of one skill                         |
| `GET /admin/skillsets/{skillset_id}/skills`                           | Skills in a skillset                              |
| `GET /admin/skills/{skill_id}/versions/{version}/files`               | List files in a bundle                            |
| `GET /admin/skills/{skill_id}/versions/{version}/files/{path}`        | Fetch a bundle file's bytes                       |

Write/mutation endpoints (skills, bundles) go through the existing admin-key-protected catalog APIs.

## Layout

A two-pane shell:

- **Desktop (≥ 768 px):** fixed 240 px dark sidebar on the left with brand, three nav links (Dashboard, Skillsets, Skills), and a version tag in the footer. Main content fills the remainder.
- **Mobile (< 768 px):** sidebar collapses. A dark top bar with a hamburger toggle opens the sidebar as a Bootstrap offcanvas drawer. The sidebar markup lives in a single Jinja macro rendered both on desktop and inside the offcanvas so there is one source of truth.

A shared `#detail-modal` element lives in the base template; any page can open it by issuing `hx-get` for a partial and pointing the swap target at `#detail-modal-content`.

A second `#file-viewer-modal` element (also in the base template) is used for bundle file viewing.

## Pages and flows

### Dashboard `/`

Three stat cards — skillset count, skill count, agent count — each linking to the corresponding list page.

### Skillsets list `/skillsets`

- Table: id, name, description, created, actions (Edit link, Delete).
- Rows are **clickable** (`row-clickable`) and open a quick-view modal (`GET /skillsets/{id}/modal`). The actions cell stops click propagation so Edit/Delete still work.
- Modal shows the skillset's metadata + a list of member skills and has **Open / Edit** and **Close** buttons.
- "New Skillset" card below the table (POST → redirect).

### Skillset detail `/skillsets/{id}`

- Inline edit form for name and description.
- Table of member skills with a Remove button (HTMX `hx-delete`).
- Add-skill selector that lists catalog skills not yet in the skillset.

### Skills list `/skills`

- Toolbar above the table with two controls:
  - **Search input** — substring match (case-insensitive) across id, name, and description. Filtering happens client-side; rows carry `data-search` with pre-lowered text.
  - **Skillset filter pills** — one button per skillset. Multiple can be toggled active; a row passes the filter if any of its skillsets is selected. The header count updates live: `{visible} / {total}`.
- Table columns: id, name, latest version, description, **Skillsets** (badges), updated, actions.
- Rows are clickable → quick-view modal (`GET /skills/{id}/modal`) showing versions, bundle file count, and metadata.
- "New Skill" card below (id, name, version, description, metadata, skillset checkboxes).

Server-side helper: the handler walks the skillsets and calls `list_skillset_skills` for each to assemble a `skill_id → [skillset_ids]` map, attached as `data-skillsets=` on each row. Filtering is then O(1) per row in the browser.

### Skill detail `/skills/{id}?version=X.Y.Z` — **read-only**

The skill detail page is **informational only**. The only mutations available here are version deletion and navigating to the edit flow.

- **Header:** skill name, id, version count. Two action buttons: **New version from v{X}** (primary) and **Clone** (outline).
- **Version selector:** one pill per version (green pill = currently selected; `latest` badge marks the is_latest version). Clicking a pill reloads with `?version=`.
- **Left column (for the selected version):**
  - Details card: name (display), description (display), metadata as a syntax-highlighted JSON block (`<pre><code class="language-json">`).
  - Timestamps card: created / updated.
  - Danger zone: **Delete this version** (HTMX `hx-delete`, confirm, redirects back to `/skills/{id}` after success).
- **Right column:**
  - Bundle card with clickable file table. Each file row opens the file viewer modal. An inline hint points to the new-version page if the user wants to change the bundle.
  - SKILL.md preview card (if `SKILL.md` is in the bundle) with an **Open rendered** button that launches the viewer modal.

### New version page `/skills/{id}/new-version?from=X.Y.Z`

Full-page form for creating a new version of an existing skill.

- **Version number** (required, semver, must not duplicate).
- **Name** — displayed but `readonly`/`disabled`. The POST handler ignores any client-supplied name field and reuses the source version's name (immutability enforced server-side).
- **Description**, **Metadata (JSON)** — prefilled from the source version.
- **Bundle** radio:
  - **Copy from v{source}** (default if the source has a bundle; disabled otherwise).
  - **Upload new bundle** — file input, required only when this option is selected (enforced in page JS).
  - **No bundle** — empty new version.
- On submit:
  1. POST `/skills` to create the new version row (using source's name).
  2. If `copy`: POST `/skills/{id}/versions/{new}/bundle/copy-from/{id}/{src}`.
  3. If `upload`: POST multipart to `/skills/{id}/versions/{new}/bundle`.
  4. Redirect to `/skills/{id}?version={new}` with a flash message.
  - On partial success (version created but bundle step failed), the redirect still lands on the new version with an error flash — partial-state visibility beats silent half-success.

### Clone skill `/skills/{id}/clone?from=X.Y.Z`

Full-page form for creating a **new skill** prefilled from an existing one. Used when the user wants to rename (names are immutable within a skill id).

- **New skill id** (required, pattern-validated, must not collide).
- **Name** — editable here (fixed after creation).
- **Starting version**, **Description**, **Metadata** — prefilled from source.
- **Bundle** radio identical to the new-version page, but **Copy from** dispatches across skill ids via `POST /skills/{new_id}/versions/{version}/bundle/copy-from/{src_id}/{src_version}`.
- On submit: create the new skill row, handle bundle, redirect to `/skills/{new_id}?version={version}`.
- Skillset memberships do **not** carry over; the user assigns them manually afterward.

### Bundle file viewer (modal, available on any page)

A shared `#file-viewer-modal` served from the base template. The page JS function `viewBundleFile(skill_id, version, path)`:

1. Fetches `/skills/{id}/versions/{ver}/files/{path}` (proxied by the Web UI, which calls the admin endpoint).
2. Decodes as UTF-8.
3. **Binary detection:** if the first 1 KB contains a null byte, renders "Binary file — N bytes" with a download button instead of garbled text.
4. **`.md` / `.markdown`:** rendered with `marked.parse(...)`; embedded code fences get `highlight.js` treatment.
5. **Code:** extension-to-language map (`py`, `js/ts/tsx/jsx`, `html/xml/svg`, `css`, `json`, `yaml`, `sh`, `sql`, `rb/go/rs/java/kt/c/cpp`, `toml/ini`, `dockerfile`, …); unknown text extensions fall back to hljs auto-detection.

The header shows the path; a **Download** button downloads the original bytes.

## Interaction patterns

| Operation                         | Mechanism                                              | Post-action                              |
| --------------------------------- | ------------------------------------------------------ | ---------------------------------------- |
| Create (skillset, skill, version) | Standard form POST → HTTP 303 redirect                 | Full page reload with flash              |
| Quick-view row click              | HTMX `hx-get` → swap partial into `#detail-modal-content` + Bootstrap modal opens | Modal appears inline |
| File click                        | Page JS (`viewBundleFile`) fetches + renders in modal  | Viewer modal appears                     |
| Delete row                        | HTMX `hx-delete` + `hx-confirm` + `hx-swap="delete"`   | Row removed from DOM                     |
| Delete version                    | HTMX `hx-delete` + redirect to `/skills/{id}` via `hx-on::after-request` | Back to skill latest view |

Flash messages are encoded as `?msg=&msg_type=` query params. `_redirect()` preserves any existing query string in the redirect path (using `&` as the separator when `?` is already present) — failing to do this produced the classic "Skill not found" bug where `/skills/pdf?version=1.0.1?msg=...` was parsed as `version=1.0.1?msg=...`.

## Error handling

All proxied API calls are wrapped in try/except.
- On `httpx.HTTPStatusError`, the detail field is extracted and shown as an error flash.
- On network error, a generic "Could not reach MCP server" message is shown.
- The detail page redirects to `/skills` with an error flash when the version lookup 404s (e.g., the user pasted an unknown `?version=`).

## Immutability rules enforced by the Web UI

| What is mutable?                           | Where                                    |
| ------------------------------------------ | ---------------------------------------- |
| Skillset name & description                | Inline on skillset detail                |
| Skill description / metadata on *new* version | New-version page                      |
| Skill name for a given id                  | **Not mutable.** Clone to rename.        |
| Bundle for an existing version             | **Not mutable from the UI.** Create a new version and upload or copy. (The underlying MCP endpoint still accepts bundle replaces; the Web UI intentionally does not expose that on the view page.) |

## Running

```bash
# Start the MCP server first
MCP_JWT_SECRET=secret MCP_ADMIN_KEY=admin-key mcp-server

# Then start the web UI
MCP_ADMIN_KEY=admin-key webui-server
# or
MCP_ADMIN_KEY=admin-key uvicorn "webui.main:create_app" --factory --port 8080 --reload
```

## Testing

Located at [`tests/test_webui.py`](../tests/test_webui.py). Uses `AsyncMock` to stand in for the MCPClient so routes can be exercised without a running MCP server. Covers:

- `_redirect` helper — regression test asserting a single `?` in the output when the path already contains a query string.
- New-version flow — bundle `copy` / `upload` / `none` paths, invalid-metadata redirect, `name` form-field ignored (immutability), upload-failure still leaves the version with an error flash.
- Clone flow — cross-skill bundle copy uses `copy_bundle(new_id, ver, src_id, src_ver)`.
- View page — unknown version redirects cleanly; no edit form markup on the page; New version + Clone buttons present.
- Skills list — search input + skillset filter pills + `data-skillsets` attrs rendered.
