# spec/mcp_server/routers — HTTP Route Specs

Each file in `mcp_server/routers/` is a thin FastAPI router that maps HTTP
requests to service-layer functions. These specs document endpoint contracts,
auth requirements, status codes, and behaviors at the HTTP edge.

| Router module | Prefix       | Spec                           | Purpose                                                 |
| ------------- | ------------ | ------------------------------ | ------------------------------------------------------- |
| `health.py`   | (none)       | [health.md](health.md)         | Liveness probe                                          |
| `token.py`    | (none)       | [token.md](token.md)           | JWT minting for registered agents                       |
| `skills.py`   | `/skills`    | [skills.md](skills.md)         | JWT-scoped skill reads; admin-gated writes              |
| `bundles.py`  | `/skills`    | [bundles.md](bundles.md)       | Bundle upload / download / copy / delete                |
| `agents.py`   | `/agents`    | [agents.md](agents.md)         | Agent CRUD (admin)                                      |
| `skillsets.py`| `/skillsets` | [skillsets.md](skillsets.md)   | Skillset CRUD + membership                              |
| `admin.py`    | `/admin`     | [admin.md](admin.md)           | Admin-key alternatives of JWT-scoped reads (Web UI)     |

## Conventions

- Write endpoints require `X-Admin-Key` via `Depends(require_admin)`.
- Skill-delivery reads require `Authorization: Bearer <JWT>` via
  `Depends(get_current_claims)`.
- `/admin/*` endpoints are admin-key-gated read-only; the Web UI uses them
  instead of issuing JWTs for itself.
- Every router receives its `Session` via `Depends(get_db)`.
- Status codes:
  - `200` — successful read, update.
  - `201` — successful create.
  - `204` — successful delete / assocation update.
  - `401` — missing / invalid JWT.
  - `403` — missing / invalid admin key.
  - `404` — resource not found.
  - `409` — uniqueness conflict.
  - `413` — bundle upload too large.
  - `422` — pydantic validation failure.
