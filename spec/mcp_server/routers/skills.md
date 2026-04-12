# mcp_server/routers/skills.py

Skill CRUD. Reads are JWT-scoped; writes are admin-gated.

## `GET /skills` (JWT)

Returns the latest version of every skill the caller's JWT authorizes
(`authorization.resolve_allowed_skill_ids` + `catalog.list_skills_for_agent`).

No pagination (prototype limitation — productization §3.3).

## `GET /skills/{skill_id}/versions` (JWT)

Returns every version of a skill as `[SkillVersionInfo]`, sorted by
`catalog.get_skill_versions` (ascending semver).

- 403 if the caller's token doesn't authorize this skill id.
- 404 if the skill has no versions at all.

## `GET /skills/{skill_id}?version=X.Y.Z` (JWT)

Returns a specific version, or latest if `version` is omitted.

- 403 if the caller's token doesn't authorize this skill id.
- 404 if the requested version doesn't exist.

## `POST /skills` (admin) → 201

Body: `schemas.SkillCreate`. Returns the created skill.

- 409 if `(id, version)` already exists (maps `catalog.create_skill`'s
  `ValueError`).
- 422 on semver / metadata validation failure (pydantic).

The `skillset_ids` list creates associations in the same call. Every id must
already exist or the whole request fails.

## `PUT /skills/{skill_id}` (admin) → 200

Body: `schemas.SkillUpsertBody`. Creates the `(id, version)` row if absent,
otherwise replaces its `name`, `description`, `metadata` in place.

Does **not** modify skillset associations. Use `PUT /skillsets/{id}/skills/{skill_id}`
for that.

## `DELETE /skills/{skill_id}?version=` (admin) → 204

- With `?version=X.Y.Z`: `catalog.delete_skill_version`. 404 if not found.
- Without: `catalog.delete_skill_all` — removes every version, their bundles, and orphan skillset-association rows. 404 if no versions existed.

## Testing

`tests/test_api_skills.py` covers all routes — classes per endpoint,
~28 tests total. Notable:
- Authorization-scoped reads: unauthorized skill id → 403 on list/get/versions.
- Latest-version-only listing behavior.
- Duplicate-rejection on POST.
- Upsert semantics (create if absent, replace if present; new version rolls `is_latest`).
- Cascading delete.

## Future work

- Pagination (`GET /skills?cursor=&limit=`).
- ETags / conditional reads.
- `PATCH /skills/{id}` for partial updates.
- `scope` enforcement (`read` for GET, `execute` for anything that triggers skill invocation — needs execution endpoint first).
