# mcp_server/schemas.py

Pydantic request/response models. Lives between the HTTP layer and the
ORM layer.

## Validation constants

`VALID_SCOPES: frozenset[str] = frozenset({"read", "execute"})` — closed
set used by `AgentCreate` / `AgentUpdate`. Extending it requires updating:
- this constant,
- any docs that list scopes,
- downstream enforcement (currently none, but productization §3.1 will
  enforce `execute`).

## Skill schemas

### `SkillCreate`

Fields: `id`, `name`, `description=""`, `version`, `metadata={}`, `skillset_ids=[]`.

Validators:
- `version` → must parse as `semver.Version`. `ValueError` → 422.
- `metadata` → must be `dict`. Non-dicts → 422.

### `SkillUpsertBody`

Body for `PUT /skills/{skill_id}`. Same as `SkillCreate` minus `id` (comes
from the path) and `skillset_ids` (upsert doesn't change associations).

### `SkillResponse`

Fields: `id`, `name`, `description`, `version`, `is_latest`, `metadata`,
`created_at`, `updated_at`. Config: `from_attributes=True` so it accepts
SQLAlchemy instances directly.

**Note**: the field is exposed as `metadata` in the response even though
the ORM column is named `metadata_` (to dodge SQLAlchemy's own `metadata`
attribute). Routers translate manually (see `_to_response` helpers).

### `SkillVersionInfo`

Slim record for version listing: `version`, `is_latest`, `created_at`.

## Skill-bundle schemas

### `BundleFileInfoResponse`

`path`, `size`, `sha256`.

### `BundleUploadResponse`

`file_count`, `total_size` (bytes). Returned from `POST /bundle`,
`POST /bundle/copy-from/...`.

## Skillset schemas

### `SkillsetCreate`

`id`, `name`, `description=""`.

### `SkillsetResponse`

`id`, `name`, `description`, `created_at`, `updated_at`. `from_attributes=True`.

## Agent schemas

### `AgentCreate`

`id`, `name`, `skillsets=[]`, `skills=[]`, `scope=[]`.

Validator:
- `scope` values must be a subset of `VALID_SCOPES`. Invalid → 422 with the
  bad values listed.

### `AgentUpdate`

All fields optional (`None` means "don't change"). Same scope validator
applied when `scope` is non-None.

### `AgentResponse`

Everything plus `created_at` / `updated_at`. `from_attributes=True`.

## Token schemas

### `TokenRequest`

`agent_id`, `expires_in: int = 3600`.

### `TokenResponse`

`access_token`, `token_type="bearer"`, `expires_in`.

## Pydantic version

Pydantic v2 semantics throughout (`field_validator`, `model_config`).

## Testing

Shapes are implicitly tested by the `tests/test_api_*` modules — anything
that fails validation returns 422 and tests check that.

## Future work

- Tighter types on `id` fields (regex for kebab-case or UUID).
- `metadata` typed with a schema registry rather than free `dict`.
- `TokenRequest.expires_in` clamped to a server-side max (policy).
- Add `tenant_id` field to every `*Create` (productization).
