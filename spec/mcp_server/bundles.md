# mcp_server/bundles.py

Archive decoding + bundle storage. Sits between the HTTP layer
(`routers/bundles.py`) and the `SkillFile` ORM model. The conceptual design
is in [`../skill-bundles.md`](../skill-bundles.md); this file documents the
code contract.

## Limits

```python
MAX_BUNDLE_BYTES = 100 * 1024 * 1024   # 100 MB total uncompressed
MAX_FILE_COUNT   = 10_000
```

Monkey-patched in tests that want smaller limits.

## Data classes

- `BundleFile(path, content)` — extracted file.
- `BundleFileInfo(path, size, sha256)` — listing row.
- `BundleStats(file_count, total_size)` — return value from upload / copy.
- `BundleError` — `ValueError` subclass raised on any extraction failure;
  router converts to 400 `Bad Request`.

## Archive formats

`detect_format(bytes) -> str` reads magic bytes; returns one of:

| Format  | Magic                                          |
| ------- | ---------------------------------------------- |
| `zip`   | `PK\x03\x04` / `PK\x05\x06` / `PK\x07\x08`     |
| `tar.gz`| `\x1f\x8b`                                     |
| `tar.bz2`| `BZh`                                         |
| `tar.xz`| `\xfd7zXZ\x00`                                 |
| `tar`   | `ustar` at offset 257                          |

Content-type / filename are **ignored** — magic bytes are truth. Unknown →
`BundleError("unsupported archive format …")`.

## `extract_archive(data, *, strip_common_prefix=False) -> list[BundleFile]`

1. `detect_format(data)`.
2. Read entries (zipfile / tarfile branch). For each entry:
   - Skip directories.
   - Reject symlinks / hardlinks in tar (explicit check; `BundleError`).
   - Reject anything that pushes the file count past `MAX_FILE_COUNT`.
   - Reject anything that pushes cumulative uncompressed size past `MAX_BUNDLE_BYTES` — uses `member.file_size` / `member.size` *before* reading bytes, so a zip bomb can't allocate past the cap.
3. `_normalize_path(raw)` on each entry name:
   - Reject absolute paths (`/etc/passwd` → `BundleError`).
   - Reject `..` as a standalone segment (`../evil` → `BundleError`).
   - Drop empty / `.` segments; normalize backslashes to `/`.
4. Optionally strip a shared top-level directory (opt-in via `strip_common_prefix=True`). Safe-guards:
   - Only strips when **≥ 2 entries** share the same top-level segment and each path would survive the strip.
   - Default is **off** — the upload endpoint intentionally does not auto-strip. The importer for `anthropics/skills` assembles per-skill tarballs with pre-stripped paths, so it doesn't need this either.
5. Dedup by path (last write wins), sort, return.

### Why a separate `_normalize_path`

The current rule uses segment-level parsing (split on `/`, reject `..` segments) rather than `str.lstrip("./")`. The latter was tried in an earlier revision and silently collapsed `"../evil"` to `"evil"` because `lstrip` removes *any character in the set*, not the literal prefix. Regression tests cover both cases.

## `store_bundle(db, skill_pk, files) -> BundleStats`

Atomic replace:
1. Bulk-delete existing `SkillFile` rows for `skill_pk`.
2. For each `BundleFile`: compute `sha256`, insert a row.
3. Commit.
4. Return counts.

## `list_bundle(db, skill_pk) -> list[BundleFileInfo]`

All files for this skill version, ordered by path.

## `get_file(db, skill_pk, path) -> SkillFile | None`

Single-row fetch by `(skill_pk, path)`.

## `delete_bundle(db, skill_pk) -> int`

Bulk-delete + commit. Returns row count.

## `copy_bundle(db, src_pk, dst_pk) -> BundleStats`

Used by both "new version from this one" and cross-skill "clone" flows:
1. Delete dst's files.
2. Copy every row from src → dst (same bytes, size, sha256; new `skill_pk`).
3. Commit.

Cross-skill is the default case from the Web UI's clone page; same-skill
is the new-version flow. The endpoint wraps this with its own path shape
— see `routers/bundles.py`.

## `build_targz(db, skill_pk) -> bytes`

Reconstructs a canonical `.tar.gz` from the stored rows. The original
archive is **not** retained — downloads rebuild on demand. This preserves
a deterministic, canonical layout regardless of what format the user
originally uploaded.

## Testing

`tests/test_bundles.py` + `tests/test_api_bundles.py` — 45 tests covering:
- Format detection for every supported format + garbage input.
- Each format roundtrips through extract → store → list.
- Strip-common-prefix opt-in and the no-strip-on-single-file regression.
- Path traversal, absolute path, symlink, empty archive, bad zip, unsupported format — all rejected.
- Size / count limits (via `monkeypatch`).
- `copy_bundle` across same and different skill ids.
- `build_targz` round-trip.

## Future work

- Move bytes out of the DB (object store) — `content` becomes a storage key (productization §3.2).
- Stream uploads instead of reading the whole archive to memory.
- Hash-based dedup: if two files have the same `sha256`, store content once.
- Delta versions: instead of re-copying the whole bundle on each new version, store a parent pointer and delta.
- Sign uploads + verify before accepting (productization §3.1 — bundle content policy).
