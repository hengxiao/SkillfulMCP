# mcp_server/routers/skillsets.py

Skillset CRUD + membership management.

## Endpoints

| Method | Path                                 | Auth       | Returns                   |
| ------ | ------------------------------------ | ---------- | ------------------------- |
| GET    | `/skillsets`                         | admin      | `list[SkillsetResponse]`  |
| GET    | `/skillsets/{id}`                    | admin      | `SkillsetResponse` / 404  |
| POST   | `/skillsets`                         | admin      | `SkillsetResponse`, 201 (409 on dup) |
| PUT    | `/skillsets/{id}`                    | admin      | `SkillsetResponse` (upsert) |
| DELETE | `/skillsets/{id}`                    | admin      | 204 / 404                 |
| GET    | `/skillsets/{id}/skills`             | **JWT**    | `list[SkillResponse]`, filtered by the agent's authorized skill set |
| PUT    | `/skillsets/{id}/skills/{skill_id}`  | admin      | 204 (add association; 404 if either id missing) |
| DELETE | `/skillsets/{id}/skills/{skill_id}`  | admin      | 204 (404 if association doesn't exist) |

## `GET /skillsets/{id}/skills` authorization

Returns `{skills in the skillset} ∩ {skills the JWT authorizes}`. A skillset
may contain skills the caller isn't authorized for; those are omitted from
the response. There is no "you can't see this skillset" gate — the endpoint
just returns an empty list if the caller has nothing in common with the
skillset.

## Membership semantics

- Associations are **version-agnostic** — `SkillSkillset` keys on `skill_id`, not `(skill_id, version)`.
- `PUT .../skills/{skill_id}` is idempotent (re-adding an existing link is a no-op).
- `DELETE .../skills/{skill_id}` removes the association row but leaves both the skill and the skillset alone.
- Deleting a **skillset** cascades to its associations via the ORM (`skill_links` relationship has `cascade="all, delete-orphan"`).
- Deleting a **skill** (all versions) is the service-layer's responsibility, which removes association rows explicitly (`catalog.delete_skill_all`).

## Testing

`tests/test_api_skillsets.py` — 19 tests covering CRUD, associations,
JWT-scoped filtering of the member-listing endpoint.

## Future work

- Admin endpoint to list memberships for a specific skill id (answers "which skillsets contain this skill" without scanning).
- Skillset-level tags (categories, audience labels) to support the Web UI's filter pills with more than just ids.
