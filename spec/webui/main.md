# webui/main.py

FastAPI app factory + every route.

## `create_app() -> FastAPI`

No lifespan — the Web UI holds no resources of its own. The `MCPClient` is
a module-level lazy singleton (`_client`) created on first `get_client()`.

## Helpers

### `_render(request, template, ctx) -> HTMLResponse`

Thin wrapper around `Jinja2Templates.TemplateResponse`, using the newer
`TemplateResponse(request, template, ctx)` signature required by
starlette ≥ 0.29.

### `_redirect(path, msg="", msg_type="success") -> RedirectResponse`

Appends `msg=` / `msg_type=` to the path. Uses `&` as the separator when
`path` already contains `?` — this is the regression-tested fix for the
"Skill not found" bug (see `tests/test_webui.py::TestRedirect`).

### `_flash_ctx(msg, msg_type) -> dict`

Helper that packs flash-message context into a dict the base template reads.

### `get_client() -> MCPClient`

Module-level lazy singleton. Tests patch this to inject an `AsyncMock`.

## Routes

### Dashboard

`GET /` — fetches skillsets, skills, agents counts; renders `dashboard.html`.

### Skillsets

| Method  | Path                                           | Purpose                                              |
| ------- | ---------------------------------------------- | ---------------------------------------------------- |
| GET     | `/skillsets`                                   | List page                                            |
| GET     | `/skillsets/{id}/modal`                        | HTMX partial — quick-view modal body                 |
| POST    | `/skillsets`                                   | Create → redirect                                    |
| GET     | `/skillsets/{id}`                              | Detail (edit form + members)                         |
| POST    | `/skillsets/{id}/update`                       | Rename / update description → redirect               |
| DELETE  | `/skillsets/{id}`                              | HTMX row delete                                      |
| POST    | `/skillsets/{id}/skills`                       | Associate a skill (form select) → redirect           |
| DELETE  | `/skillsets/{id}/skills/{skill_id}`            | HTMX row delete (remove association)                 |

### Skills

| Method  | Path                                           | Purpose                                              |
| ------- | ---------------------------------------------- | ---------------------------------------------------- |
| GET     | `/skills`                                      | List page with search + skillset-filter (client-side)|
| GET     | `/skills/{id}/modal`                           | HTMX partial — quick-view modal body                 |
| POST    | `/skills`                                      | Create new skill → redirect                          |
| GET     | `/skills/{id}?version=`                        | Read-only detail page for a specific version         |
| GET     | `/skills/{id}/clone?from=`                     | Clone form (new skill id, inherited bundle)          |
| POST    | `/skills/{id}/clone`                           | Create clone → redirect                              |
| GET     | `/skills/{id}/new-version?from=`               | New-version form                                     |
| POST    | `/skills/{id}/new-version`                     | Create new version + optional bundle handling        |
| DELETE  | `/skills/{id}`                                 | Delete all versions                                  |
| DELETE  | `/skills/{id}/versions/{version:path}`         | Delete one version                                   |

### Bundle file fetch (proxy)

| Method | Path                                                         | Purpose                                     |
| ------ | ------------------------------------------------------------ | ------------------------------------------- |
| GET    | `/skills/{id}/versions/{version}/files/{path:path}`         | Proxies to `MCPClient.get_bundle_file()`; used by the client-side viewer |

No bundle upload route — uploads go through the new-version and clone POST
handlers so users can't mutate an existing version's bundle from the view
page.

## Read-only view vs mutation pages

- `GET /skills/{id}` renders `skill.html` — **read-only**. The only mutation
  available is Delete-this-version.
- Mutations flow through dedicated pages (`/new-version`, `/clone`) which
  handle metadata + bundle together in one submit.

## New-version handler specifics

`POST /skills/{id}/new-version`:
1. Parse `metadata` as JSON; bad JSON → redirect back with error.
2. Fetch the source version (`client.get_skill(id, version=from_version)`)
   to read the **authoritative name** — any `name` field the client sent is
   ignored.
3. `client.create_skill(...)` with the source's name.
4. Based on `bundle_action`:
   - `copy` → `client.copy_bundle(id, new, id, from)`.
   - `upload` → `client.upload_bundle(id, new, filename, bytes)`.
   - `none` → leave the new version bundle-less.
5. Redirect to `/skills/{id}?version={new}` with a success flash. Partial
   failures (version created but bundle step failed) still redirect there
   with an error flash, so the user sees the new version rather than a
   false-negative 500.

## Clone handler specifics

`POST /skills/{id}/clone`:
1. Create a new skill (new id, editable name, initial version).
2. Handle bundle per `bundle_action` — `copy` routes to
   `client.copy_bundle(new_id, version, src_id, src_version)` (cross-skill).
3. Redirect to `/skills/{new_id}?version={version}`.

Skillset memberships are **not** carried over. The user re-associates
manually.

## Testing

`tests/test_webui.py` — 22 tests with `MCPClient` replaced by `AsyncMock`.
Covers `_redirect` edge cases, new-version / clone happy paths and failure
modes, name-immutability enforcement, invalid-metadata handling, the
read-only view page, and the skills-list filter markup.
