# mcp_server/catalog.py

Service-layer CRUD for skills, skill versions, skillsets, and the
membership join. The HTTP layer (`routers/*`) delegates all database work
here.

## Concepts

- **Skill = (id, version)**. Each row is one version of a skill.
- **`is_latest`** is maintained by the service, not the caller. Every mutation that touches versions calls `_refresh_is_latest(skill_id)` which re-resolves based on semver ordering.
- **Skillset membership** is **version-agnostic**. A `SkillSkillset` row pairs `(skill_id, skillset_id)` — every version of that skill id belongs to the skillset.

## Internal helpers

### `_refresh_is_latest(db, skill_id)`

Load every row with this id, pick the max by `semver.Version.parse(row.version)`,
set `is_latest = (row.pk == latest.pk)` on each. `db.flush()` — commit is the
caller's responsibility. Called after inserts, version deletes, and upserts
that introduce a new version.

### `_ensure_link(db, skill_id, skillset_id)`

Idempotent insert into `skill_skillsets`. Used by `create_skill` and
`add_skill_to_skillset`.

## Skill CRUD

### `create_skill(db, data: SkillCreate) -> Skill`

1. Validate every `skillset_id` in `data.skillset_ids` exists → `ValueError` if not.
2. Build `Skill` row with `is_latest=False`, flush.
3. On `IntegrityError` (duplicate `(id, version)`) rollback + raise
   `ValueError` that the router converts to 409.
4. Create `SkillSkillset` association rows for each skillset id. q
5. `_refresh_is_latest(data.id)` → commit → refresh → return.

### `upsert_skill(db, skill_id, name, description, version, metadata) -> Skill`

If `(id, version)` exists, mutate its fields. Otherwise insert a new row.
Either path finishes with `_refresh_is_latest` + commit.

Does **not** update skillset associations — that's a separate concern.

### `get_skill_latest(db, skill_id) -> Skill | None`

`WHERE id = ? AND is_latest = TRUE LIMIT 1`.

### `get_skill_version(db, skill_id, version) -> Skill | None`

`WHERE id = ? AND version = ? LIMIT 1`.

### `get_skill_versions(db, skill_id) -> list[Skill]`

All rows with this id, **sorted ascending by semver**. (The HTTP layer
reverses this when it wants "latest first".)

### `delete_skill_all(db, skill_id) -> int`

Deletes every version of a skill. Explicit two-step to preserve integrity
without relying on SQLite FK enforcement:
1. Capture `pk`s of all rows with this id.
2. Bulk-delete `SkillFile`s by `skill_pk in (...)`.
3. Bulk-delete `Skill`s.
4. Bulk-delete `SkillSkillset` rows (since the join doesn't have a DB FK
   back to `Skill`, orphan rows would otherwise survive).

Returns the count of deleted Skill rows (0 = "not found" → 404 in router).

### `delete_skill_version(db, skill_id, version) -> bool`

Deletes one specific version:
1. Fetch the target row (need `.pk` for the bundle delete).
2. Delete its `SkillFile`s.
3. Delete the skill row.
4. If any siblings remain: `_refresh_is_latest`.
5. Otherwise: remove `SkillSkillset` rows (the skill id is gone).

Returns `True` on hit, `False` on miss (→ 404).

### `list_skills_for_agent(db, allowed_ids: set[str]) -> list[Skill]`

Used by `GET /skills` (JWT-scoped). Returns the latest version of every
skill whose id is in `allowed_ids`. `allowed_ids` comes from
`authorization.resolve_allowed_skill_ids`.

## Skillset CRUD

- `create_skillset(db, data)` — insert; `IntegrityError` → `ValueError` → 409.
- `upsert_skillset(db, id, data)` — mutate if present, else insert.
- `get_skillset(db, id)` — `db.get(Skillset, id)`.
- `list_skillsets(db)` — all rows.
- `delete_skillset(db, id)` — ORM delete; ORM cascade on `skill_links` removes association rows.

## Membership CRUD

- `list_skills_in_skillset(db, skillset_id)` — subquery on `SkillSkillset.skill_id`, return latest versions of each.
- `add_skill_to_skillset(db, skillset_id, skill_id)` — validates both exist, then `_ensure_link`.
- `remove_skill_from_skillset(db, skillset_id, skill_id)` — bulk-delete from join; return `True` on hit.

## Transactional boundaries

Every top-level function commits before returning. The prototype uses
autoflush-off sessions and calls `db.flush()` to surface integrity errors
before `commit`. There's no explicit retry on transient errors — a P1 item
in productization.

## Testing

`tests/test_catalog.py` exercises every function on an in-memory SQLite DB,
independent of FastAPI. 31 tests covering:
- create/update/delete for each entity
- duplicate-rejection semantics
- `is_latest` invariant after arbitrary insert orders
- cascade cleanup (version-delete leaves no orphan `SkillFile` rows)
- skillset membership updates

## Future work

- Replace list-based `skillset_ids` param on create with separate association
  calls, then a single "apply memberships" helper (cleans up the split brain
  between `create_skill` and `upsert_skill`).
- Index `SkillSkillset.skill_id` for the "show all skills in skillsets X, Y"
  scan path.
- Batch `delete_skill_all` into a single `RETURNING` query on Postgres
  (removes the two-step capture-and-delete).
