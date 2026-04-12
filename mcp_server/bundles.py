"""
Skill-bundle storage and archive handling.

See spec/skill-bundles.md for the full model. This module implements:

- archive format detection by magic bytes,
- safe extraction with path / size / count guards,
- persistence into the `skill_files` table,
- reconstruction of a canonical `.tar.gz` for download.

The storage API is intentionally narrow so the backend can later be swapped
from SQLite BLOBs to an object store without touching the HTTP layer.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
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
class BundleStats:
    file_count: int
    total_size: int


# ---------------------------------------------------------------------------
# Archive format detection
# ---------------------------------------------------------------------------

def detect_format(data: bytes) -> str:
    """Return one of 'zip', 'tar', 'tar.gz', 'tar.bz2', 'tar.xz'. Raises on unknown."""
    if len(data) < 4:
        raise BundleError("archive too small to identify")
    # ZIP: PK\x03\x04 (or \x05\x06 empty / \x07\x08 spanned)
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    # gzip
    if data[:2] == b"\x1f\x8b":
        return "tar.gz"
    # bzip2
    if data[:3] == b"BZh":
        return "tar.bz2"
    # xz
    if data[:6] == b"\xfd7zXZ\x00":
        return "tar.xz"
    # Uncompressed tar: "ustar" magic at offset 257
    if len(data) >= 265 and data[257:262] in (b"ustar",):
        return "tar"
    raise BundleError(
        "unsupported archive format (expected zip, tar, tar.gz, tar.bz2, or tar.xz)"
    )


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _normalize_path(raw: str) -> str | None:
    """Normalize a POSIX-ish path; return None if it should be skipped.

    Rejects absolute paths and `..` traversal.
    """
    if not raw:
        return None
    p = raw.replace("\\", "/")
    if p.startswith("/"):
        raise BundleError(f"absolute path not allowed: {raw!r}")
    # Reject `..` anywhere as a standalone segment. Use raw parts (before
    # filtering) so that "../evil" does not silently collapse to "evil".
    raw_parts = p.split("/")
    if any(seg == ".." for seg in raw_parts):
        raise BundleError(f"path traversal not allowed: {raw!r}")
    parts = [seg for seg in raw_parts if seg and seg != "."]
    if not parts:
        return None
    return "/".join(parts)


def _common_top_prefix(paths: list[str]) -> str:
    """If every path shares the same leading directory AND there are multiple
    such paths, return that prefix. Else ''.

    This is only used when the caller explicitly asks to strip a wrapper dir
    (for example, GitHub tarballs with a leading `repo-main/` component).
    Single-file archives are never stripped, since a lone `scripts/run.py`
    should not turn into `run.py`.
    """
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
                        raise BundleError(
                            f"file {info.filename!r} exceeds size limit"
                        )
                    # Check cumulative uncompressed size before reading
                    running = sum(len(c) for _, c in raw)
                    if running + info.file_size > MAX_BUNDLE_BYTES:
                        raise BundleError("bundle exceeds total size limit")
                    raw.append((info.filename, zf.read(info)))
        except zipfile.BadZipFile as exc:
            raise BundleError(f"bad zip archive: {exc}")
    else:  # tar family
        # tarfile auto-detects compression when mode is "r:*"
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
                for member in tf:
                    if member.issym() or member.islnk():
                        raise BundleError(
                            f"symlink/hardlink not allowed: {member.name!r}"
                        )
                    if not member.isfile():
                        continue
                    if len(raw) >= MAX_FILE_COUNT:
                        raise BundleError(f"too many files (> {MAX_FILE_COUNT})")
                    if member.size > MAX_BUNDLE_BYTES:
                        raise BundleError(
                            f"file {member.name!r} exceeds size limit"
                        )
                    running = sum(len(c) for _, c in raw)
                    if running + member.size > MAX_BUNDLE_BYTES:
                        raise BundleError("bundle exceeds total size limit")
                    fobj = tf.extractfile(member)
                    if fobj is None:
                        continue
                    raw.append((member.name, fobj.read()))
        except tarfile.TarError as exc:
            raise BundleError(f"bad tar archive: {exc}")

    # Normalize + optionally strip shared top-level dir (common for tarballs
    # produced by `git archive` or GitHub's codeload).
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

    # Deduplicate: last entry for a given path wins
    by_path: dict[str, bytes] = {}
    for p, c in normalized:
        by_path[p] = c
    return [BundleFile(path=p, content=c) for p, c in sorted(by_path.items())]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def store_bundle(db: Session, skill_pk: int, files: list[BundleFile]) -> BundleStats:
    """Replace all files for skill_pk with the given list, atomically."""
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


def list_bundle(db: Session, skill_pk: int) -> list[BundleFileInfo]:
    rows = (
        db.query(SkillFile)
        .filter(SkillFile.skill_pk == skill_pk)
        .order_by(SkillFile.path)
        .all()
    )
    return [BundleFileInfo(path=r.path, size=r.size, sha256=r.sha256) for r in rows]


def get_file(db: Session, skill_pk: int, path: str) -> SkillFile | None:
    return (
        db.query(SkillFile)
        .filter(SkillFile.skill_pk == skill_pk, SkillFile.path == path)
        .first()
    )


def delete_bundle(db: Session, skill_pk: int) -> int:
    n = db.query(SkillFile).filter(SkillFile.skill_pk == skill_pk).delete()
    db.commit()
    return n


def copy_bundle(db: Session, src_skill_pk: int, dst_skill_pk: int) -> BundleStats:
    """Replace the bundle at dst with a copy of the bundle at src.

    Used when a new skill version is created from an existing one and the user
    wants to inherit the bundle rather than re-upload it.
    """
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


def build_targz(db: Session, skill_pk: int) -> bytes:
    """Rebuild a canonical .tar.gz for downloading the whole bundle."""
    rows = (
        db.query(SkillFile)
        .filter(SkillFile.skill_pk == skill_pk)
        .order_by(SkillFile.path)
        .all()
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for r in rows:
            info = tarfile.TarInfo(name=r.path)
            info.size = r.size
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(r.content))
    return buf.getvalue()
