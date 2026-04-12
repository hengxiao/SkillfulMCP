"""
Skill-bundle HTTP endpoints.

See spec/skill-bundles.md for the endpoint contract and authorization model.

Authorization:
    - Writes require the admin key header (X-Admin-Key).
    - Reads require a valid JWT; the agent must be authorized for the skill id
      via skills or skillsets claims. A JWT that authorizes the skill grants
      full read access to every file in that skill version (no per-file ACL).
"""

from __future__ import annotations

import mimetypes

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..authorization import resolve_allowed_skill_ids
from ..bundles import (
    MAX_BUNDLE_BYTES,
    BundleError,
    build_targz,
    copy_bundle,
    delete_bundle,
    extract_archive,
    get_file,
    list_bundle,
    store_bundle,
)
from ..catalog import get_skill_version
from ..dependencies import get_current_claims, get_db, require_admin
from ..schemas import BundleFileInfoResponse, BundleUploadResponse

router = APIRouter(prefix="/skills", tags=["bundles"])


def _require_skill(db: Session, skill_id: str, version: str):
    skill = get_skill_version(db, skill_id, version)
    if not skill:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill {skill_id!r} version {version!r} not found",
        )
    return skill


def _require_read_access(claims: dict, db: Session, skill_id: str) -> None:
    allowed = resolve_allowed_skill_ids(claims, db)
    if skill_id not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Access denied"
        )


# ---------------------------------------------------------------------------
# Write endpoints (admin)
# ---------------------------------------------------------------------------

@router.post(
    "/{skill_id}/versions/{version}/bundle",
    response_model=BundleUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_bundle(
    skill_id: str,
    version: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """Upload an archive (zip / tar / tar.gz / tar.bz2 / tar.xz).

    Replaces all files for that skill version atomically. 100 MB cap; see
    spec/skill-bundles.md.
    """
    skill = _require_skill(db, skill_id, version)
    data = await file.read()
    if len(data) > MAX_BUNDLE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"archive exceeds {MAX_BUNDLE_BYTES} bytes",
        )
    try:
        files = extract_archive(data)
    except BundleError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    stats = store_bundle(db, skill.pk, files)
    return BundleUploadResponse(file_count=stats.file_count, total_size=stats.total_size)


@router.post(
    "/{skill_id}/versions/{version}/bundle/copy-from/{src_skill_id}/{src_version}",
    response_model=BundleUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
def copy_bundle_endpoint(
    skill_id: str,
    version: str,
    src_skill_id: str,
    src_version: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    """Replace the bundle at `(skill_id, version)` with a copy of the bundle at
    `(src_skill_id, src_version)`. Supports same-skill (new version from an
    existing one) and cross-skill (clone-into-new-skill) flows.
    """
    dst = _require_skill(db, skill_id, version)
    src = _require_skill(db, src_skill_id, src_version)
    stats = copy_bundle(db, src.pk, dst.pk)
    return BundleUploadResponse(file_count=stats.file_count, total_size=stats.total_size)


@router.delete(
    "/{skill_id}/versions/{version}/bundle",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_bundle_endpoint(
    skill_id: str,
    version: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
):
    skill = _require_skill(db, skill_id, version)
    delete_bundle(db, skill.pk)


# ---------------------------------------------------------------------------
# Read endpoints (JWT-authorized)
# ---------------------------------------------------------------------------

@router.get(
    "/{skill_id}/versions/{version}/files",
    response_model=list[BundleFileInfoResponse],
)
def list_files(
    skill_id: str,
    version: str,
    claims: dict = Depends(get_current_claims),
    db: Session = Depends(get_db),
):
    _require_read_access(claims, db, skill_id)
    skill = _require_skill(db, skill_id, version)
    return [
        BundleFileInfoResponse(path=f.path, size=f.size, sha256=f.sha256)
        for f in list_bundle(db, skill.pk)
    ]


@router.get("/{skill_id}/versions/{version}/bundle")
def download_bundle(
    skill_id: str,
    version: str,
    claims: dict = Depends(get_current_claims),
    db: Session = Depends(get_db),
):
    _require_read_access(claims, db, skill_id)
    skill = _require_skill(db, skill_id, version)
    data = build_targz(db, skill.pk)
    return Response(
        content=data,
        media_type="application/gzip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{skill_id}-{version}.tar.gz"'
            )
        },
    )


@router.get("/{skill_id}/versions/{version}/files/{path:path}")
def download_file(
    skill_id: str,
    version: str,
    path: str,
    claims: dict = Depends(get_current_claims),
    db: Session = Depends(get_db),
):
    _require_read_access(claims, db, skill_id)
    skill = _require_skill(db, skill_id, version)
    row = get_file(db, skill.pk, path)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"File {path!r} not found"
        )
    media_type, _encoding = mimetypes.guess_type(path)
    return Response(
        content=row.content,
        media_type=media_type or "application/octet-stream",
        headers={"X-Content-SHA256": row.sha256},
    )
