# spec/webui — Web UI Submodule Specs

Per-file specifications for the `webui/` FastAPI application.
High-level design is in [`../webui.md`](../webui.md); this directory covers
implementation details.

## Module map

| File                         | Spec                                       | Purpose                                           |
| ---------------------------- | ------------------------------------------ | ------------------------------------------------- |
| `main.py`                    | [main.md](main.md)                         | App factory, all routes, redirect helper          |
| `client.py`                  | [client.md](client.md)                     | Async httpx wrapper around the MCP catalog API    |
| `config.py`                  | [config.md](config.md)                     | Settings from env                                 |
| `auth.py` + `middleware.py`  | [auth.md](auth.md)                         | Operator auth, sessions, CSRF (Wave 6a)           |
| `templates/`                 | [templates.md](templates.md)               | Jinja templates (pages + partials)                |

## Conventions

- The Web UI is **stateless**. It stores nothing locally — all data goes
  through the MCP server.
- All outgoing HTTP calls carry the shared `X-Admin-Key` header. The Web UI
  is effectively a thin admin console.
- Responses are Jinja-rendered HTML for page views, small HTML partials for
  modal bodies and HTMX-driven row deletes, and plain `Response(bytes)` for
  bundle file fetches (pass-through from the catalog).
- Flash messages travel as `?msg=&msg_type=` query params via `_redirect()`.
  That helper preserves any existing query string (`&msg=` instead of
  `?msg=` if the path already has a `?`) to avoid the double-`?` bug that
  broke the new-version flow early on.
