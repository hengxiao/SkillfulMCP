# mcp_server/bundles.py

Archive extraction + bundle storage. Conceptual design in
[`../skill-bundles.md`](../skill-bundles.md); this spec documents the
module contract.

## Shape

Two concerns live here:

1. **Archive extraction** — format detection, safe path handling, size
   guards. Pure functions, no I/O beyond the archive bytes.
2. **`BundleStore`** — abstract interface for persisting / retrieving
   per-file bytes alongside the `skill_files` index rows. Two concrete
   implementations:
   - `InlineBundleStore` — bytes live in `skill_files.content`. Default
     for dev and single-node deployments.
   - `S3BundleStore` — bytes live in an S3-compatible object store;
     `skill_files.content` is a `b""` placeholder so the existing
     NOT-NULL schema stays intact. Requires `pip install -e ".[s3]"`.

The choice is a **process-wide deployment decision**. Mixing inline and
S3 rows within the same catalog is not supported in this wave; a
migration script (future work) can move data between the backends.

## Limits (spec/skill-bundles.md §"Size Limits")

```python
MAX_BUNDLE_BYTES = 100 * 1024 * 1024   # 100 MB total uncompressed
MAX_FILE_COUNT   = 10_000
```

Monkey-patched in tests that need smaller limits.

## Data classes

| Class                 | Purpose                                                 |
| --------------------- | ------------------------------------------------------- |
| `BundleFile`          | Extracted file (path + content bytes)                   |
| `BundleFileInfo`      | Index row shape (path + size + sha256)                  |
| `BundleFileContent`   | `read_file` return type (index fields + content bytes)  |
| `BundleStats`         | `put_files` / `copy_all` result (file_count, total_size) |
| `BundleError`         | `ValueError` subclass; router converts to 400           |

## Archive extraction

`detect_format(bytes) -> str` — magic-byte sniff. Returns `"zip"`,
`"tar"`, `"tar.gz"`, `"tar.bz2"`, or `"tar.xz"`; raises `BundleError`
for anything else. Filename and content-type are ignored — magic bytes
are the truth.

`extract_archive(data, *, strip_common_prefix=False) -> list[BundleFile]`:
1. Sniff format.
2. Walk entries, rejecting directories, symlinks, hardlinks, and anything
   that would push cumulative uncompressed size past `MAX_BUNDLE_BYTES`
   or file count past `MAX_FILE_COUNT`.
3. `_normalize_path` on each name — absolute paths and `..` segments are
   rejected with `BundleError`. Segment-level parsing (not `str.lstrip`)
   so `"../evil"` does NOT silently collapse to `"evil"` (regression-
   tested).
4. Optionally strip a shared top-level directory when the caller sets
   `strip_common_prefix=True`. Safeguards: only strips when ≥ 2 entries
   share the prefix and every path would survive the strip.
5. Deduplicate by path (last wins), sort, return.

## `BundleStore` interface

```python
class BundleStore(ABC):
    backend_name: str

    def put_files(db, skill_pk, files) -> BundleStats: ...
    def list_files(db, skill_pk) -> list[BundleFileInfo]: ...   # shared impl
    def read_file(db, skill_pk, path) -> BundleFileContent | None: ...
    def delete_all(db, skill_pk) -> int: ...
    def copy_all(db, src_skill_pk, dst_skill_pk) -> BundleStats: ...
    def build_targz(db, skill_pk) -> bytes: ...                 # shared impl
```

- `put_files` must be a **replace** — delete any prior state for this
  `skill_pk` before writing the new files, so there are no orphans.
- `list_files` and `build_targz` have shared default implementations on
  the base class that read the DB index; backends generally don't
  override these.
- `read_file` returns `BundleFileContent | None`. Routers access
  `.content`, `.size`, `.sha256`, `.path` — attribute-compatible with the
  old pre-refactor `SkillFile` return.

## `InlineBundleStore`

Stores bytes directly in `SkillFile.content`. Every method is a small
SQL + ORM operation.

## `S3BundleStore`

Key layout: `{prefix}/pk{skill_pk}/{path}`.

- `put_files` — wipe `skill_files` rows + S3 objects under
  `{prefix}/pk{skill_pk}/`, then upload each file and insert a row with
  `content=b""` + size + sha256.
- `read_file` — DB fetch for the index row, S3 GET for the bytes. If the
  S3 object is missing while the row exists (storage drift), returns
  `None` rather than raising.
- `copy_all` — uses `CopyObject` (server-side) to avoid round-tripping
  bytes through the catalog process.
- `delete_all` — list + bulk delete S3 objects, then bulk delete rows.

Dev-friendly knobs:
- `MCP_BUNDLE_S3_ENDPOINT_URL` — plug in MinIO / LocalStack.
- `MCP_BUNDLE_S3_REGION` — explicit region (else boto3 auto-resolves).

## Module-level shims

`store_bundle`, `list_bundle`, `get_file`, `delete_bundle`, `copy_bundle`,
`build_targz` keep the pre-Wave-5 signatures and delegate to the module-
level default store. `get_default_store()` builds the default lazily from
`get_settings()`; `set_default_store(store)` installs a custom one (used
by `main.create_app` so every app owns its store, and by tests that need
to install a fake). `reset_default_store()` is the test helper.

## Config

| Env var                           | Default   | Purpose                                             |
| --------------------------------- | --------- | --------------------------------------------------- |
| `MCP_BUNDLE_STORE`                | `inline`  | `inline` or `s3`                                    |
| `MCP_BUNDLE_S3_BUCKET`            | —         | Required when `MCP_BUNDLE_STORE=s3`                 |
| `MCP_BUNDLE_S3_PREFIX`            | `bundles` | Key prefix; keys become `{prefix}/pk{skill_pk}/…`   |
| `MCP_BUNDLE_S3_REGION`            | —         | Optional explicit region                            |
| `MCP_BUNDLE_S3_ENDPOINT_URL`      | —         | Optional; for MinIO / LocalStack / VPC endpoints    |

## Testing

- `tests/test_bundles.py` + `tests/test_api_bundles.py` — existing
  extraction + HTTP tests, now exercising the `InlineBundleStore` path
  via the module shims.
- `tests/test_bundle_store_s3.py` — 12 tests against moto-mocked S3:
  factory behavior, put/read/list/delete/copy round-trips, replace
  semantics (no orphan S3 objects), `build_targz` rebuild, missing-
  object-with-row drift, and `set_default_store` wiring.

## Future work

- **Migration script** — read every inline row, upload bytes to S3, set
  `content=b""`. Required before flipping `MCP_BUNDLE_STORE=s3` on an
  existing deployment.
- **Streaming uploads / downloads** — current implementation loads the
  whole archive into memory on upload and materializes the reassembled
  `.tar.gz` in memory on download. Streaming would reduce peak RSS for
  large bundles.
- **Hash-based dedup** — if two files across two skills share a
  `sha256`, store the content once in S3 and point both rows at it.
- **Signed URLs** — for large file downloads, return a presigned S3 URL
  in `GET /skills/…/bundle` instead of streaming through the catalog.
- **Tiered storage** — auto-tier cold bundles to Glacier / Archive
  (productization §3.2 P2).
