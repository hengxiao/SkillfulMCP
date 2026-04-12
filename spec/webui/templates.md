# webui/templates/

Jinja templates. Each page template extends `base.html`. Modal bodies and
small partials start with `_` by convention and are returned directly from
HTMX handlers without the page shell.

## Template inventory

| File                      | Kind     | Rendered by                         |
| ------------------------- | -------- | ----------------------------------- |
| `base.html`               | shell    | every page                          |
| `dashboard.html`          | page     | `GET /`                             |
| `skillsets.html`          | page     | `GET /skillsets`                    |
| `skillset.html`           | page     | `GET /skillsets/{id}`               |
| `_skillset_modal.html`    | partial  | `GET /skillsets/{id}/modal`         |
| `skills.html`             | page     | `GET /skills`                       |
| `skill.html`              | page     | `GET /skills/{id}?version=`         |
| `skill_new_version.html`  | page     | `GET /skills/{id}/new-version?from=`|
| `skill_clone.html`        | page     | `GET /skills/{id}/clone?from=`      |
| `_skill_modal.html`       | partial  | `GET /skills/{id}/modal`            |

## `base.html` — shell

- **Layout:** fixed dark sidebar (≥ 768 px) + main column, with a
  hamburger-toggled offcanvas drawer on mobile. Sidebar markup lives in
  a Jinja macro rendered twice (one source of truth).
- **Global modals:**
  - `#detail-modal` — shared quick-view for skillsets/skills; populated via HTMX `hx-get` into `#detail-modal-content`; reset to a spinner on close.
  - `#file-viewer-modal` — bundle file viewer; populated by the `viewBundleFile(id, version, path)` JS helper.
- **CDN includes:** Bootstrap 5.3, Bootstrap Icons 1.11, HTMX 1.9, marked.js 12, highlight.js 11 (loaded from cdnjs — the jsdelivr `lib/` paths ship ES modules, not a browser bundle).
- **Flash messages:** `?msg=&msg_type=` are read at the top of `{% block content %}` and rendered as a Bootstrap alert.
- **Custom CSS:** sidebar palette, `.row-clickable`, file-viewer modal sizing, `.md-rendered` typography for rendered markdown.

## `viewBundleFile(id, version, path)` JS

Lives inline at the bottom of `base.html`. Fetches the file, detects binary
content by null-byte presence in the first 1 KB, and dispatches:

- **Markdown** (`.md`, `.markdown`) → `marked.parse()`, then
  `hljs.highlightElement` on any `<pre><code>` blocks in the rendered HTML.
- **Code** — `EXT_TO_LANG` maps a dozen common extensions; falls back to
  hljs auto-detection for unknown text.
- **Binary** — render a download button instead of garbled text.

Page-load hook calls `hljs.highlightAll()` on any server-rendered
`<pre><code class="language-*">` blocks (e.g. the read-only metadata JSON
on the skill view page).

## Page templates

### `dashboard.html`

Three stat cards linking to the respective list pages.

### `skillsets.html`

- Table with click-to-preview rows (`row-clickable` + `hx-get` loading the modal partial, with `onclick="event.stopPropagation()"` on the actions cell).
- Create form below the table.

### `skillset.html`

- Edit form for name / description.
- Skill membership table with HTMX-powered remove-association rows.
- Add-skill `<select>` populated from catalog skills not already in the skillset.

### `_skillset_modal.html`

Modal body showing the skillset's metadata and member skills.

### `skills.html`

- Search + skillset filter toolbar. Search uses `data-search` pre-lowered text on each row; filter pills toggle `data-skillset-id` membership. Both are entirely client-side.
- Visible count badge updates live; empty-result hint appears when filters match nothing.
- Skill rows carry `data-skillsets="..."` computed server-side and render as clickable quick-view modals.

### `skill.html` — read-only

- Header with **New version from v{X}** and **Clone** buttons (the only mutation paths).
- Version selector pills.
- Left column: read-only details (name, description, syntax-highlighted metadata JSON), timestamps, danger-zone delete.
- Right column: bundle file list (clickable viewer + download per row), SKILL.md preview with "Open rendered" that re-opens the file via the viewer modal.

### `skill_new_version.html`

- Form prefilled from the source version.
- Name field is disabled/readonly with a rename hint pointing at the Clone page.
- Bundle radio: copy / upload / none. JS makes the file input required only when "upload" is selected.

### `skill_clone.html`

Same shape as new-version, except the skill id and name are editable.

### `_skill_modal.html`

Quick-view modal: version list, bundle file count, metadata. Files in the
bundle list are clickable → file viewer modal (stacked on top of the
quick-view modal).

## Testing

`tests/test_webui.py` covers markup presence:
- Name is `readonly` on the new-version page.
- Search input + filter pills + `data-skillsets` attrs are rendered.
- The read-only skill page has no `/skills/pdf/update` markup (ensures the
  old edit form didn't leak back in) and the New version / Clone buttons
  are present.

## Future work

- Extract shared styling into a compiled CSS asset so we're not inlining
  everything in `base.html`.
- Move the file-viewer JS into an external module.
- Dark mode.
