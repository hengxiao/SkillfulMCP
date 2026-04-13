# Skill Bundles Spec

This document describes the addition of **bundle storage** to the MCP server,
so that each skill version can carry its full content (SKILL.md plus any
supporting files — scripts, references, licenses, etc.) instead of metadata
only.

## Motivation

The initial catalog stored only skill metadata. Real-world skills (for example
those published at https://github.com/anthropics/skills) are multi-file
packages: a `SKILL.md` plus `scripts/`, `reference/`, `LICENSE.txt`, and so on.
Agents need the actual bundle content to use a skill, not just its description.

## Scope

- Store the bytes of every file that makes up a skill version.
- Retrieve individual files, list the tree, or download the full bundle.
- Accept uploads in common archive formats.
- Keep the existing metadata model and API surface backwards-compatible.
- Remain swappable to an external object store later without reshaping the API.

## Storage Model

Bundles are stored in SQLite as BLOBs for the prototype. The database row
doubles as the metadata index and the content store. A future change can move
the `content` column to an object store (S3, MinIO, filesystem) without altering
the public API — the `SkillFile` row remains the authoritative index.

### `SkillFile` table

| Column      | Type        | Notes                                                |
| ----------- | ----------- | ---------------------------------------------------- |
| `skill_pk`  | int (FK)    | References `skills.pk`. Cascade delete.              |
| `path`      | string      | Relative POSIX path inside the bundle (no `..`).     |
| `content`   | BLOB        | Raw file bytes.                                      |
| `size`      | int         | Length of `content` in bytes.                        |
| `sha256`    | string      | Hex digest of `content`; used for integrity + dedup. |

Primary key: `(skill_pk, path)`. A skill version owns its files; deleting the
skill version cascades to its files.

### Path rules

- Paths are normalized to POSIX, no leading `/`, no `..` traversal.
- Symlinks in the uploaded archive are rejected (security).
- Directory entries are skipped (only file content is stored).
- Filenames must decode as UTF-8.

## Supported Upload Formats

The server detects the format from the file's magic bytes (not the client's
content-type or filename), then unpacks it.

| Format  | Extension(s)                    | Notes                               |
| ------- | ------------------------------- | ----------------------------------- |
| ZIP     | `.zip`                          | stdlib `zipfile`                    |
| tar     | `.tar`                          | stdlib `tarfile`                    |
| gzip    | `.tar.gz`, `.tgz`               | stdlib `tarfile` w/ gzip            |
| bzip2   | `.tar.bz2`, `.tbz2`             | stdlib `tarfile` w/ bz2             |
| xz      | `.tar.xz`, `.txz`               | stdlib `tarfile` w/ lzma            |

Uploads that cannot be identified as one of the above are rejected with
`400 Bad Request`.

## Size Limits

| Limit                      | Value     | Rationale                                  |
| -------------------------- | --------- | ------------------------------------------ |
| Max uploaded archive size  | 100 MB    | Hard cap on the request body.              |
| Max total uncompressed size| 100 MB    | Guards against zip bombs and decompression amplification. |
| Max number of files        | 10,000    | Simple guard against pathological archives. |

If any limit is exceeded the whole upload is rejected atomically; no partial
state is written.

## API

All write endpoints require the admin key header (`X-Admin-Key`). Read
endpoints are authorized by the agent's JWT using the same rule as the existing
`GET /skills/*` routes: **if the JWT grants access to the skill id (directly or
via a skillset), the agent has full read access to every file in that skill
version.** No per-file ACLs.

Endpoints:

- `POST /skills/{skill_id}/versions/{version}/bundle`
  - Multipart upload, field name `file`. Replaces all files for that version
    atomically. Returns `{ "file_count": N, "total_size": bytes }`.
- `GET /skills/{skill_id}/versions/{version}/files`
  - Returns `[{ "path": str, "size": int, "sha256": str }, ...]`.
- `GET /skills/{skill_id}/versions/{version}/files/{path:path}`
  - Streams a single file's bytes. `Content-Type` best-effort from extension,
    default `application/octet-stream`.
- `GET /skills/{skill_id}/versions/{version}/bundle`
  - Streams the whole bundle as a freshly built `.tar.gz`
    (`Content-Type: application/gzip`). The server does not retain the original
    archive — it is normalized into `skill_files` rows on upload, so downloads
    rebuild a canonical archive from rows.
- `DELETE /skills/{skill_id}/versions/{version}/bundle`
  - Removes all files for the version (admin).

Version handling: in all endpoints above, `{version}` is a concrete semver
string. Clients that only know about the latest version must first call
`GET /skills/{skill_id}` (returns `version`) before requesting files.

## Storage Layer Abstraction

Shipped in Wave 5. The real interface (see
[`mcp_server/bundles.md`](mcp_server/bundles.md) for the full spec):

```python
class BundleStore(ABC):
    backend_name: str
    def put_files(db, skill_pk, files) -> BundleStats: ...
    def list_files(db, skill_pk) -> list[BundleFileInfo]: ...
    def read_file(db, skill_pk, path) -> BundleFileContent | None: ...
    def delete_all(db, skill_pk) -> int: ...
    def copy_all(db, src_pk, dst_pk) -> BundleStats: ...
    def build_targz(db, skill_pk) -> bytes: ...
```

Two implementations:
- `InlineBundleStore` (default) — bytes in `SkillFile.content`.
- `S3BundleStore` — bytes in an S3-compatible object store at
  `{prefix}/pk{skill_pk}/{path}`. `SkillFile.content` is a `b""`
  placeholder so the NOT-NULL schema is preserved.

Active backend is chosen per deployment via `MCP_BUNDLE_STORE=inline|s3`.
The `SkillFile` row is the authoritative index in both backends.

## Web UI

The skill-detail page gains:

- a **Files** panel: tree of paths with sizes and download links,
- a **Bundle upload** form (admin only): file picker + submit,
- an inline render of `SKILL.md` (if present) using a markdown renderer.

## Migration

The prototype has no Alembic setup. The new `skill_files` table is created by
`Base.metadata.create_all()` on startup. Existing skill rows are unaffected; a
skill without a bundle simply has no `skill_files` rows.

## Open Questions / Future Work

- Checksum verification on download. Clients may want the `sha256` of the
  rebuilt archive — currently only per-file digests are exposed.
- Signed bundle support: verify a detached signature at upload time.
- Per-file ACL (e.g. hide `*.key` from certain scopes). Explicitly out of scope
  for this iteration.
- Move `content` to object storage when bundles grow past a few hundred MB
  aggregate. The table row already has `size` and `sha256`, so it can carry an
  object key instead without API changes.
