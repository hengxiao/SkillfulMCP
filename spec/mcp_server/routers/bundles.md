# mcp_server/routers/bundles.py

Skill-bundle HTTP endpoints. Shares the `/skills` prefix with the skill
router but operates on `(skill_id, version)` pairs. Auth model is documented
in [`../../skill-bundles.md`](../../skill-bundles.md) and restated here.

## Auth model

- **Writes** (upload, delete, copy): `X-Admin-Key` (`require_admin`).
- **Reads** (list, get one, download archive): JWT with the skill id in its
  authorized set. Granting a skill via JWT is all-or-nothing: if an agent can
  see the skill, it can read every file in the version (no per-file ACL).

Helper `_require_skill(db, id, version)` returns the `Skill` row or 404.
`_require_read_access(claims, db, id)` checks
`authorization.resolve_allowed_skill_ids` and 403s on miss.

## `POST /skills/{skill_id}/versions/{version}/bundle` (admin) → 201

Multipart upload. Field name `file`. Accepts zip / tar / tar.gz / tar.bz2 /
tar.xz (see `bundles.py` for detection).

Body size checked twice:
- Request-body size: if `len(data) > MAX_BUNDLE_BYTES` → 413 `Request Entity Too Large`.
- Extraction: per-file and cumulative checks inside `bundles.extract_archive` → 400 `BundleError` on violation.

On success:
- `store_bundle` replaces any prior bundle atomically.
- Returns `BundleUploadResponse(file_count, total_size)`.

## `POST /skills/{skill_id}/versions/{version}/bundle/copy-from/{src_skill_id}/{src_version}` (admin) → 201

Replaces the bundle at `(skill_id, version)` with a copy of the bundle at
`(src_skill_id, src_version)`. Used both for same-skill (new version) and
cross-skill (clone) flows — callers pass the same skill id for both when it
makes sense.

- 404 if either the source or destination skill version doesn't exist.
- Returns the same `BundleUploadResponse` shape as upload.

## `DELETE /skills/{skill_id}/versions/{version}/bundle` (admin) → 204

Removes all files for a version. Idempotent — deleting an empty bundle is
a no-op that still returns 204 (as long as the skill version exists).

## `GET /skills/{skill_id}/versions/{version}/files` (JWT) → list

Returns `list[BundleFileInfoResponse]` sorted by path.

## `GET /skills/{skill_id}/versions/{version}/files/{path:path}` (JWT)

Streams a single file. Response headers:
- `Content-Type`: best-effort from extension via `mimetypes.guess_type`, default `application/octet-stream`.
- `X-Content-SHA256`: hex digest of the file.

404 if the file isn't in the bundle.

## `GET /skills/{skill_id}/versions/{version}/bundle` (JWT)

Streams the whole bundle as a freshly-built `.tar.gz`
(`bundles.build_targz`). Headers:
- `Content-Type: application/gzip`
- `Content-Disposition: attachment; filename="{skill_id}-{version}.tar.gz"`

## Testing

`tests/test_api_bundles.py` covers upload (all formats), replace semantics,
path traversal rejection, size caps, JWT-scoped reads (authorized /
unauthorized / skillset-granted / missing-file 404s), download (file-by-file
and full archive), delete + cascade delete (deleting the skill version
removes its bundle rows), cross-skill copy.

## Future work

- Stream upload (don't buffer the whole archive in memory).
- Range requests on file download.
- Upload content signing (P1 in productization plan).
