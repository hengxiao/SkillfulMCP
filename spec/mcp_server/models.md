# mcp_server/models.py

SQLAlchemy 2.0-style declarative ORM models.

## `Base`

`DeclarativeBase` — single root for all tables in the module. `create_all` on
this Base produces the whole schema.

## `Skill`

Versioned skill rows. Composite uniqueness on `(id, version)`.

| Column       | Type          | Notes                                                  |
| ------------ | ------------- | ------------------------------------------------------ |
| `pk`         | int PK AI     | Synthetic surrogate — lets other tables FK a *specific version* without carrying (id, version) tuples. |
| `id`         | str, indexed  | Logical skill id (shared across versions).             |
| `name`       | str           | Display name. Per-version but in practice stable.      |
| `description`| str           | Free text.                                             |
| `version`    | str           | Semver. Parsed by `semver.Version` in `catalog.py`.    |
| `is_latest`  | bool          | Exactly one row per `id` is marked; maintained by `_refresh_is_latest`. |
| `metadata_`  | JSON          | Stored as column `metadata` (renamed to avoid shadowing SQLA's `metadata`). |
| `created_at` | datetime(tz)  | UTC on insert.                                         |
| `updated_at` | datetime(tz)  | UTC on insert and on update.                           |

## `SkillFile`

Bundle file row. See [`../skill-bundles.md`](../skill-bundles.md) for the
content model. PK is `(skill_pk, path)`; `skill_pk` has
`ForeignKey("skills.pk", ondelete="CASCADE")` — though SQLite needs
`PRAGMA foreign_keys=ON` for that to actually fire, and service code deletes
dependents explicitly to stay correct regardless.

| Column     | Type        | Notes                                        |
| ---------- | ----------- | -------------------------------------------- |
| `skill_pk` | int FK      | → `skills.pk`                                |
| `path`     | str         | Relative POSIX path inside the bundle        |
| `content`  | LargeBinary | Raw bytes (SQLite BLOB column)               |
| `size`     | int         | Byte length                                  |
| `sha256`   | str         | Hex digest for integrity / future dedup      |

## `Skillset`

| Column        | Type         |
| ------------- | ------------ |
| `id`          | str PK       |
| `name`        | str          |
| `description` | str          |
| `created_at`  | datetime(tz) |
| `updated_at`  | datetime(tz) |

Has `skill_links: list[SkillSkillset]` with `cascade="all, delete-orphan"`
so deleting a skillset cascades to its association rows via the ORM (distinct
from the `SkillFile` case, which uses bulk deletes).

## `SkillSkillset`

Many-to-many join between skills and skillsets. Keyed by `skill_id` (the
*logical* id — not the per-version pk) so membership is version-agnostic.

| Column        | Type | Notes                                         |
| ------------- | ---- | --------------------------------------------- |
| `skill_id`    | str  | PK (part of composite)                        |
| `skillset_id` | str  | PK; FK → `skillsets.id` (ondelete=CASCADE)    |

**Design choice**: no FK from `skill_id` → `skills.id` because skills aren't
primary-key'd by `id` (they're keyed by `pk`). Orphan rows are scrubbed
explicitly in `catalog.delete_skill_all`.

## `Agent`

Agent registry. Skill grants are stored as JSON lists on the row.

| Column       | Type         | Notes                              |
| ------------ | ------------ | ---------------------------------- |
| `id`         | str PK       |                                    |
| `name`       | str          | Display name                       |
| `skillsets`  | JSON (list)  | `list[str]` — skillset ids granted |
| `skills`     | JSON (list)  | `list[str]` — explicit skill grants|
| `scope`      | JSON (list)  | `list[str]` — e.g. `["read", "execute"]` |
| `created_at` | datetime(tz) |                                    |
| `updated_at` | datetime(tz) |                                    |

**Trade-off**: JSON columns for list fields keep the prototype simple but
break normalization — you can't query "all agents authorized for skillset X"
efficiently. Fine at prototype scale.

## Invariants maintained by the service layer

- `Skill.is_latest` — exactly one `True` per `id`, recomputed on every
  insert / version delete by `catalog._refresh_is_latest` using semver order.
- `SkillFile.size == len(content)` — enforced by `bundles.store_bundle`.
- `SkillSkillset` rows may exist for a `(skill_id, skillset_id)` pair even
  when no `Skill` rows with that id exist — `catalog.add_skill_to_skillset`
  rejects this on write, but the DB doesn't prevent drift.

## Future work

- Composite unique index on `(Skill.id, Skill.version)` is declared; add an
  index on `SkillFile.skill_pk` for fast list queries as bundles grow.
- Move `content` out of this table (see `skill-bundles.md` §"Storage Layer
  Abstraction").
- Add `tenant_id` to every top-level entity; compound unique constraints
  include it (productization §3.1).
