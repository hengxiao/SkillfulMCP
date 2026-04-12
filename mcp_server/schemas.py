from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator
import semver

VALID_SCOPES: frozenset[str] = frozenset({"read", "execute"})


# ---------------------------------------------------------------------------
# Skill schemas
# ---------------------------------------------------------------------------

class SkillCreate(BaseModel):
    id: str
    name: str
    description: str = ""
    version: str
    metadata: dict[str, Any] = {}
    skillset_ids: list[str] = []

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


class SkillUpsertBody(BaseModel):
    """Body for PUT /skills/{skill_id} — id comes from the path."""
    name: str
    description: str = ""
    version: str
    metadata: dict[str, Any] = {}

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        try:
            semver.Version.parse(v)
        except ValueError:
            raise ValueError(f"Invalid semver version: {v!r}.")
        return v


class SkillResponse(BaseModel):
    id: str
    name: str
    description: str
    version: str
    is_latest: bool
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SkillVersionInfo(BaseModel):
    version: str
    is_latest: bool
    created_at: datetime


# ---------------------------------------------------------------------------
# Skillset schemas
# ---------------------------------------------------------------------------

class SkillsetCreate(BaseModel):
    id: str
    name: str
    description: str = ""


class SkillsetResponse(BaseModel):
    id: str
    name: str
    description: str
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


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
