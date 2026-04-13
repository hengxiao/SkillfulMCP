from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator
import semver

VALID_SCOPES: frozenset[str] = frozenset({"read", "execute"})

# Wave 8a — visibility flag values
VALID_VISIBILITY: frozenset[str] = frozenset({"public", "private"})


# ---------------------------------------------------------------------------
# Skill schemas
# ---------------------------------------------------------------------------

def _validate_visibility(v: str) -> str:
    if v not in VALID_VISIBILITY:
        raise ValueError(
            f"visibility must be one of {sorted(VALID_VISIBILITY)}, got {v!r}"
        )
    return v


class SkillCreate(BaseModel):
    id: str
    name: str
    description: str = ""
    version: str
    metadata: dict[str, Any] = {}
    skillset_ids: list[str] = []
    # Wave 8a — defaults to private so callers that don't know about
    # this field continue to behave as before.
    visibility: str = "private"

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        try:
            semver.Version.parse(v)
        except ValueError:
            raise ValueError(f"Invalid semver version: {v!r}. Must follow MAJOR.MINOR.PATCH.")
        return v

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, v: Any) -> dict:
        if not isinstance(v, dict):
            raise ValueError("metadata must be a JSON object (dict)")
        return v

    @field_validator("visibility")
    @classmethod
    def _v(cls, v: str) -> str:
        return _validate_visibility(v)


class SkillUpsertBody(BaseModel):
    """Body for PUT /skills/{skill_id} — id comes from the path."""
    name: str
    description: str = ""
    version: str
    metadata: dict[str, Any] = {}
    visibility: str = "private"

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        try:
            semver.Version.parse(v)
        except ValueError:
            raise ValueError(f"Invalid semver version: {v!r}.")
        return v

    @field_validator("visibility")
    @classmethod
    def _v(cls, v: str) -> str:
        return _validate_visibility(v)


class SkillResponse(BaseModel):
    id: str
    name: str
    description: str
    version: str
    is_latest: bool
    metadata: dict[str, Any]
    visibility: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SkillVersionInfo(BaseModel):
    version: str
    is_latest: bool
    created_at: datetime


# ---------------------------------------------------------------------------
# Skill bundle schemas
# ---------------------------------------------------------------------------

class BundleFileInfoResponse(BaseModel):
    path: str
    size: int
    sha256: str


class BundleUploadResponse(BaseModel):
    file_count: int
    total_size: int


# ---------------------------------------------------------------------------
# Skillset schemas
# ---------------------------------------------------------------------------

class SkillsetCreate(BaseModel):
    id: str
    name: str
    description: str = ""
    visibility: str = "private"

    @field_validator("visibility")
    @classmethod
    def _v(cls, v: str) -> str:
        return _validate_visibility(v)


class SkillsetResponse(BaseModel):
    id: str
    name: str
    description: str
    visibility: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Agent schemas
# ---------------------------------------------------------------------------

class AgentCreate(BaseModel):
    id: str
    name: str
    skillsets: list[str] = []
    skills: list[str] = []
    scope: list[str] = []

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, v: list[str]) -> list[str]:
        invalid = set(v) - VALID_SCOPES
        if invalid:
            raise ValueError(
                f"Invalid scope value(s): {invalid}. Valid values: {sorted(VALID_SCOPES)}"
            )
        return v


class AgentUpdate(BaseModel):
    name: str | None = None
    skillsets: list[str] | None = None
    skills: list[str] | None = None
    scope: list[str] | None = None

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            invalid = set(v) - VALID_SCOPES
            if invalid:
                raise ValueError(
                    f"Invalid scope value(s): {invalid}. Valid values: {sorted(VALID_SCOPES)}"
                )
        return v


class AgentResponse(BaseModel):
    id: str
    name: str
    skillsets: list[str]
    skills: list[str]
    scope: list[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Token schemas
# ---------------------------------------------------------------------------

class TokenRequest(BaseModel):
    agent_id: str
    expires_in: int = 3600
    # Wave 8c: optional narrowing. When supplied, the issued token's
    # `skills` / `skillsets` / `scope` claims use these values instead of
    # the agent's full grants. Each list MUST be a subset of the agent's
    # own list, otherwise the server 400s — a compromised admin key still
    # can't mint broader tokens than the registered agent allows.
    skills: list[str] | None = None
    skillsets: list[str] | None = None
    scope: list[str] | None = None

    @field_validator("scope")
    @classmethod
    def _scope(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            invalid = set(v) - VALID_SCOPES
            if invalid:
                raise ValueError(
                    f"Invalid scope value(s): {invalid}. Valid: {sorted(VALID_SCOPES)}"
                )
        return v


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


# ---------------------------------------------------------------------------
# User schemas (Wave 9 — `role` is no longer a column on `users`.
# Account-scoped roles live on AccountMembership rows. The identity
# schemas below carry no role field.)
# ---------------------------------------------------------------------------

class UserCreate(BaseModel):
    email: str
    password: str
    display_name: str | None = None


class UserUpdate(BaseModel):
    display_name: str | None = None
    disabled: bool | None = None
    password: str | None = None


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str | None
    disabled: bool
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None

    model_config = {"from_attributes": True}


class AuthenticateResponse(BaseModel):
    """Response shape for POST /admin/users/authenticate.

    Extends the regular user response with `is_superadmin` so the Web UI
    can tell platform-admin sessions apart from regular users. For the
    hardcoded superadmin identity (§2.3), `id` is the reserved string
    `"0"` and the DB is never touched.
    """

    id: str
    email: str
    display_name: str | None = None
    disabled: bool = False
    is_superadmin: bool = False


class UserAuthenticateRequest(BaseModel):
    email: str
    password: str
