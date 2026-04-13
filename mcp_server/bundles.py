"""
Skill-bundle storage + archive handling.

Two concerns live here:

1. **Archive extraction** — format detection, size/path-traversal guards,
   content normalization. Pure functions, no I/O beyond the archive bytes.
2. **BundleStore** — abstract interface for persisting / retrieving per-file
   bytes + the SkillFile index rows. Two implementations:
     - `InlineBundleStore` — bytes stored in `skill_files.content`.
     - `S3BundleStore`     — bytes stored in an S3-compatible object
                             store; `skill_files` row still written with
                             `content=b""` as a placeholder.
   The choice is a process-wide deployment decision (`MCP_BUNDLE_STORE`).
   Mixing backends within a catalog is not supported in this wave; a
   future migration script handles inline-to-S3 data moves.

Module-level functions (`store_bundle`, `list_bundle`, …) remain as
back-compat shims over a lazily-built default store. New callers can get
the explicit store from `app.state.bundle_store`.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .models import SkillFile


# ---------------------------------------------------------------------------
# Limits (see spec/skill-bundles.md §"Size Limits")
# ---------------------------------------------------------------------------

MAX_BUNDLE_BYTES = 100 * 1024 * 1024  # 100 MB total uncompressed
MAX_FILE_COUNT = 10_000


class BundleError(ValueError):
    """Raised when an uploaded archive is rejected."""


@dataclass(frozen=True)
class BundleFile:
    path: str
    content: bytes


@dataclass(frozen=True)
class BundleFileInfo:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class BundleFileContent:
    """Return type of `get_file`. Matches the shape routers already use
    (`.path`, `.size`, `.sha256`, `.content`) but is a plain dataclass so
    S3-backed reads don't need to fake a SkillFile ORM row."""
    path: str
    size: int
    sha256: str
    content: bytes


@dataclass(frozen=True)
class BundleStats:
    file_count: int
    total_size: int


# ---------------------------------------------------------------------------
# Archive format detection (unchanged from prior wave)
# ---------------------------------------------------------------------------

def detect_format(data: bytes) -> str:
    """Return one of 'zip', 'tar', 'tar.gz', 'tar.bz2', 'tar.xz'. Raises on unknown."""
    if len(data) < 4:
        raise BundleError("archive too small to identify")
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if data[:2] == b"\x1f\x8b":
        return "tar.gz"
    if data[:3] == b"BZh":
        return "tar.bz2"
    if data[:6] == b"\xfd7zXZ\x00":
        return "tar.xz"
    if len(data) >= 265 and data[257:262] in (b"ustar",):
        return "tar"
    raise BundleError(
        "unsupported archive format (expected zip, tar, tar.gz, tar.bz2, or tar.xz)"
    )


def _normalize_path(raw: str) -> str | None:
    if not raw:
        return None
    p = raw.replace("\\", "/")
    if p.startswith("/"):
        raise BundleError(f"absolute path not allowed: {raw!r}")
    raw_parts = p.split("/")
    if any(seg == ".." for seg in raw_parts):
        raise BundleError(f"path traversal not allowed: {raw!r}")
    parts = [seg for seg in raw_parts if seg and seg != "."]
    if not parts:
        return None
    return "/".join(parts)


def _common_top_prefix(paths: list[str]) -> str:
    if len(paths) < 2:
        return ""
    first = paths[0].split("/", 1)
    if len(first) < 2:
        return ""
    prefix = first[0] + "/"
    if all(p.startswith(prefix) and len(p) > len(prefix) for p in paths):
        return prefix
    return ""


def extract_archive(data: bytes, *, strip_common_prefix: bool = False) -> list[BundleFile]:
    """Extract bytes → list of BundleFile. Raises BundleError on any violation."""
    if len(data) == 0:
        raise BundleError("archive is empty")
    fmt = detect_format(data)
    raw: list[tuple[str, bytes]] = []

    if fmt == "zip":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    if len(raw) >= MAX_FILE_COUNT:
                        raise BundleError(f"too many files (> {MAX_FILE_COUNT})")
                    if info.file_size > MAX_BUNDLE_BYTES:
                        raise BundleError(f"file {info.filename!r} exceeds size limit")
                    running = sum(len(c) for _, c in raw)
                    if running + info.file_size > MAX_BUNDLE_BYTES:
                        raise BundleError("bundle exceeds total size limit")
                    raw.append((info.filename, zf.read(info)))
        except zipfile.BadZipFile as exc:
            raise BundleError(f"bad zip archive: {exc}")
    else:
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
                for member in tf:
                    if member.issym() or member.islnk():
                        raise BundleError(f"symlink/hardlink not allowed: {member.name!r}")
                    if not member.isfile():
                        continue
                    if len(raw) >= MAX_FILE_COUNT:
                        raise BundleError(f"too many files (> {MAX_FILE_COUNT})")
                    if member.size > MAX_BUNDLE_BYTES:
                        raise BundleError(f"file {member.name!r} exceeds size limit")
                    running = sum(len(c) for _, c in raw)
                    if running + member.size > MAX_BUNDLE_BYTES:
                        raise BundleError("bundle exceeds total size limit")
                    fobj = tf.extractfile(member)
                    if fobj is None:
                        continue
                    raw.append((member.name, fobj.read()))
        except tarfile.TarError as exc:
            raise BundleError(f"bad tar archive: {exc}")

    normalized: list[tuple[str, bytes]] = []
    for name, content in raw:
        p = _normalize_path(name)
        if p is not None:
            normalized.append((p, content))
    if not normalized:
        raise BundleError("archive contains no files")
    if strip_common_prefix:
        prefix = _common_top_prefix([p for p, _ in normalized])
        if prefix:
            normalized = [(p[len(prefix):], c) for p, c in normalized]
            normalized = [(p, c) for p, c in normalized if p]

    by_path: dict[str, bytes] = {}
    for p, c in normalized:
        by_path[p] = c
    return [BundleFile(path=p, content=c) for p, c in sorted(by_path.items())]


# ---------------------------------------------------------------------------
# BundleStore interface
# ---------------------------------------------------------------------------

class BundleStore(ABC):
    """Where bundle bytes physically live. The `SkillFile` row is the
    shared index in both backends — list_files reads rows, size and sha256
    come from rows, path ordering is by row. Only byte residency differs.
    """

    backend_name: str = "unknown"

    @abstractmethod
    def put_files(
        self, db: Session, skill_pk: int, files: list[BundleFile]
    ) -> BundleStats: ...

    @abstractmethod
    def read_file(
        self, db: Session, skill_pk: int, path: str
    ) -> BundleFileContent | None: ...

    @abstractmethod
    def delete_all(self, db: Session, skill_pk: int) -> int: ...

    @abstractmethod
    def copy_all(
        self, db: Session, src_skill_pk: int, dst_skill_pk: int
    ) -> BundleStats: ...

    # Index-only operation — same in every backend. Can be shared.
    def list_files(self, db: Session, skill_pk: int) -> list[BundleFileInfo]:
        rows = (
            db.query(SkillFile)
            .filter(SkillFile.skill_pk == skill_pk)
            .order_by(SkillFile.path)
            .all()
        )
        return [BundleFileInfo(path=r.path, size=r.size, sha256=r.sha256) for r in rows]

    def build_targz(self, db: Session, skill_pk: int) -> bytes:
        """Rebuild the bundle as a canonical .tar.gz for download.

        Default implementation calls `read_file` per path. Backends can
        override for a streamed implementation if they want to avoid
        materializing the whole tarball in memory.
        """
        listing = self.list_files(db, skill_pk)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for info in listing:
                content = self.read_file(db, skill_pk, info.path)
                if content is None:
                    continue
                t = tarfile.TarInfo(name=info.path)
                t.size = info.size
                t.mode = 0o644
                tf.addfile(t, io.BytesIO(content.content))
        return buf.getvalue()


# ---------------------------------------------------------------------------
# Inline backend — bytes in the skill_files.content column
# ---------------------------------------------------------------------------

class InlineBundleStore(BundleStore):
    backend_name = "inline"

    def put_files(
        self, db: Session, skill_pk: int, files: list[BundleFile]
    ) -> BundleStats:
        db.query(SkillFile).filter(SkillFile.skill_pk == skill_pk).delete()
        total = 0
        for f in files:
            digest = hashlib.sha256(f.content).hexdigest()
            db.add(
                SkillFile(
                    skill_pk=skill_pk,
                    path=f.path,
                    content=f.content,
                    size=len(f.content),
                    sha256=digest,
                )
            )
            total += len(f.content)
        db.commit()
        return BundleStats(file_count=len(files), total_size=total)

    def read_file(
        self, db: Session, skill_pk: int, path: str
    ) -> BundleFileContent | None:
        row = (
            db.query(SkillFile)
            .filter(SkillFile.skill_pk == skill_pk, SkillFile.path == path)
            .first()
        )
        if row is None:
            return None
        return BundleFileContent(
            path=row.path, size=row.size, sha256=row.sha256, content=row.content
        )

    def delete_all(self, db: Session, skill_pk: int) -> int:
        n = db.query(SkillFile).filter(SkillFile.skill_pk == skill_pk).delete()
        db.commit()
        return n

    def copy_all(
        self, db: Session, src_skill_pk: int, dst_skill_pk: int
    ) -> BundleStats:
        db.query(SkillFile).filter(SkillFile.skill_pk == dst_skill_pk).delete()
        rows = db.query(SkillFile).filter(SkillFile.skill_pk == src_skill_pk).all()
        total = 0
        for r in rows:
            db.add(
                SkillFile(
                    skill_pk=dst_skill_pk,
                    path=r.path,
                    content=r.content,
                    size=r.size,
                    sha256=r.sha256,
                )
            )
            total += r.size
        db.commit()
        return BundleStats(file_count=len(rows), total_size=total)


# ---------------------------------------------------------------------------
# S3 backend — bytes in an S3-compatible object store
# ---------------------------------------------------------------------------

class S3BundleStore(BundleStore):
    """S3-backed bundle store.

    Object key scheme: `{prefix}/pk{skill_pk}/{path}`.

    The `skill_files` row is still written for every file and carries the
    authoritative size + sha256; `content` is set to `b""` (empty blob) so
    the NOT NULL column stays satisfied without wasting significant space.

    Replace semantics (`put_files`, `copy_all` dst, `delete_all`) delete
    all existing S3 objects under the skill's prefix before writing new
    ones, so there are no orphans after an overwrite.
    """

    backend_name = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "bundles",
        client=None,
        region: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        if not bucket:
            raise RuntimeError("S3BundleStore requires a bucket name")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is not None:
            self.client = client
        else:
            try:
                import boto3  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "S3BundleStore requires boto3. Install with: "
                    "pip install -e '.[s3]'"
                ) from exc
            kwargs: dict = {}
            if region:
                kwargs["region_name"] = region
            if endpoint_url:
                kwargs["endpoint_url"] = endpoint_url
            self.client = boto3.client("s3", **kwargs)

    # Key layout -----------------------------------------------------------

    def _skill_prefix(self, skill_pk: int) -> str:
        return f"{self.prefix}/pk{skill_pk}/".lstrip("/")

    def _key(self, skill_pk: int, path: str) -> str:
        return self._skill_prefix(skill_pk) + path

    # S3 helpers -----------------------------------------------------------

    def _delete_objects_under(self, skill_pk: int) -> None:
        prefix = self._skill_prefix(skill_pk)
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            contents = page.get("Contents", [])
            if not contents:
                continue
            self.client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": obj["Key"]} for obj in contents]},
            )

    # BundleStore interface ------------------------------------------------

    def put_files(
        self, db: Session, skill_pk: int, files: list[BundleFile]
    ) -> BundleStats:
        # 1. Wipe existing S3 objects + rows.
        self._delete_objects_under(skill_pk)
        db.query(SkillFile).filter(SkillFile.skill_pk == skill_pk).delete()

        # 2. Upload each file and write its index row.
        total = 0
        for f in files:
            digest = hashlib.sha256(f.content).hexdigest()
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._key(skill_pk, f.path),
                Body=f.content,
            )
            db.add(
                SkillFile(
                    skill_pk=skill_pk,
                    path=f.path,
                    content=b"",  # placeholder; real bytes live in S3
                    size=len(f.content),
                    sha256=digest,
                )
            )
            total += len(f.content)
        db.commit()
        return BundleStats(file_count=len(files), total_size=total)

    def read_file(
        self, db: Session, skill_pk: int, path: str
    ) -> BundleFileContent | None:
        row = (
            db.query(SkillFile)
            .filter(SkillFile.skill_pk == skill_pk, SkillFile.path == path)
            .first()
        )
        if row is None:
            return None
        try:
            obj = self.client.get_object(
                Bucket=self.bucket, Key=self._key(skill_pk, path)
            )
        except self.client.exceptions.NoSuchKey:
            # Row present but object missing — catalog / storage drift.
            return None
        body = obj["Body"].read()
        return BundleFileContent(
            path=row.path, size=row.size, sha256=row.sha256, content=body
        )

    def delete_all(self, db: Session, skill_pk: int) -> int:
        self._delete_objects_under(skill_pk)
        n = db.query(SkillFile).filter(SkillFile.skill_pk == skill_pk).delete()
        db.commit()
        return n

    def copy_all(
        self, db: Session, src_skill_pk: int, dst_skill_pk: int
    ) -> BundleStats:
        # Clear destination first.
        self._delete_objects_under(dst_skill_pk)
        db.query(SkillFile).filter(SkillFile.skill_pk == dst_skill_pk).delete()

        src_rows = db.query(SkillFile).filter(SkillFile.skill_pk == src_skill_pk).all()
        total = 0
        for r in src_rows:
            # S3 server-side copy. Avoids round-tripping bytes through the
            # catalog process.
            self.client.copy_object(
                Bucket=self.bucket,
                Key=self._key(dst_skill_pk, r.path),
                CopySource={
                    "Bucket": self.bucket,
                    "Key": self._key(src_skill_pk, r.path),
                },
            )
            db.add(
                SkillFile(
                    skill_pk=dst_skill_pk,
                    path=r.path,
                    content=b"",
                    size=r.size,
                    sha256=r.sha256,
                )
            )
            total += r.size
        db.commit()
        return BundleStats(file_count=len(src_rows), total_size=total)


# ---------------------------------------------------------------------------
# Factory + module-level default store (backwards compat for prior API)
# ---------------------------------------------------------------------------

def build_store_from_settings(settings) -> BundleStore:
    backend = (settings.bundle_store or "inline").lower()
    if backend == "inline":
        return InlineBundleStore()
    if backend == "s3":
        return S3BundleStore(
            bucket=settings.bundle_s3_bucket,
            prefix=settings.bundle_s3_prefix or "bundles",
            region=settings.bundle_s3_region or None,
            endpoint_url=settings.bundle_s3_endpoint_url or None,
        )
    raise RuntimeError(
        f"Unknown MCP_BUNDLE_STORE={backend!r}. Expected 'inline' or 's3'."
    )


_default_store: BundleStore | None = None


def get_default_store() -> BundleStore:
    """Return the module-level default BundleStore (built lazily)."""
    global _default_store
    if _default_store is None:
        from .config import get_settings
        _default_store = build_store_from_settings(get_settings())
    return _default_store


def set_default_store(store: BundleStore | None) -> None:
    """Explicitly install (or reset to None) the module-level default."""
    global _default_store
    _default_store = store


def reset_default_store() -> None:
    """Test helper: clear the lazy default so the next call rebuilds from settings."""
    set_default_store(None)


# ---------------------------------------------------------------------------
# Module-level shims — preserve the pre-Wave-5 API
# ---------------------------------------------------------------------------

def store_bundle(db: Session, skill_pk: int, files: list[BundleFile]) -> BundleStats:
    return get_default_store().put_files(db, skill_pk, files)


def list_bundle(db: Session, skill_pk: int) -> list[BundleFileInfo]:
    return get_default_store().list_files(db, skill_pk)


def get_file(db: Session, skill_pk: int, path: str) -> BundleFileContent | None:
    return get_default_store().read_file(db, skill_pk, path)


def delete_bundle(db: Session, skill_pk: int) -> int:
    return get_default_store().delete_all(db, skill_pk)


def copy_bundle(db: Session, src_skill_pk: int, dst_skill_pk: int) -> BundleStats:
    return get_default_store().copy_all(db, src_skill_pk, dst_skill_pk)


def build_targz(db: Session, skill_pk: int) -> bytes:
    return get_default_store().build_targz(db, skill_pk)
