from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator
import semver

VALID_SCOPES: frozenset[str] = frozenset({"read", "execute"})

# Wave 8a -> 9.2 — visibility tiers.
#   public  — any authenticated agent; anonymous UI visitors too.
#   account — members of the resource's account + allow list.
#   private — owner + account-admins + (allow list ∩ account members).
# "account" replaces the Wave 8a default; existing `private` rows stay
# private and remain backward-compatible.
VALID_VISIBILITY: frozenset[str] = frozenset({"public", "account", "private"})


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
    # Wave 9.2 — optional account_id. When omitted, the catalog
    # service stamps the row with the id of the `default` account so
    # admin-key CLI callers + existing tests keep working unchanged.
    account_id: str | None = None
    owner_user_id: str | None = None

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
    # Wave 9.2 — see SkillCreate.
    account_id: str | None = None
    owner_user_id: str | None = None

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
    # Wave 9.2 — always populated for rows created post-migration; the
    # 0005 backfill wrote the "default" account id into pre-9.2 rows.
    account_id: str | None = None
    owner_user_id: str | None = None
    owner_email_snapshot: str | None = None
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
    # Wave 9.2 — see SkillCreate.
    account_id: str | None = None
    owner_user_id: str | None = None

    @field_validator("visibility")
    @classmethod
    def _v(cls, v: str) -> str:
        return _validate_visibility(v)


class SkillsetResponse(BaseModel):
    id: str
    name: str
    description: str
    visibility: str
    account_id: str | None = None
    owner_user_id: str | None = None
    owner_email_snapshot: str | None = None
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
    # Wave 9.2 — optional at the wire; defaults to the `default`
    # account in the registry layer.
    account_id: str | None = None
    owner_user_id: str | None = None

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
    account_id: str | None = None
    owner_user_id: str | None = None
    owner_email_snapshot: str | None = None
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


# ---------------------------------------------------------------------------
# Account / membership / pending-invitation schemas (Wave 9.1)
# ---------------------------------------------------------------------------

VALID_MEMBERSHIP_ROLES: frozenset[str] = frozenset(
    {"account-admin", "contributor", "viewer"}
)


def _validate_membership_role(v: str) -> str:
    v = (v or "").strip().lower()
    if v not in VALID_MEMBERSHIP_ROLES:
        raise ValueError(
            f"role must be one of {sorted(VALID_MEMBERSHIP_ROLES)}, got {v!r}"
        )
    return v


class AccountCreateRequest(BaseModel):
    """Body for `POST /admin/accounts`.

    `initial_admin_user_id` must reference an existing users row. The
    endpoint atomically adds that user as the first `account-admin`
    membership.
    """

    name: str
    initial_admin_user_id: str


class AccountResponse(BaseModel):
    id: str
    name: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MembershipInviteRequest(BaseModel):
    """Body for `POST /admin/accounts/{id}/members`.

    If `email` resolves to an existing users row, a real membership is
    created. Otherwise a `pending_memberships` row is inserted and
    consumed atomically when that email later signs up.
    """

    email: str
    role: str

    @field_validator("role")
    @classmethod
    def _r(cls, v: str) -> str:
        return _validate_membership_role(v)


class MembershipRoleUpdateRequest(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def _r(cls, v: str) -> str:
        return _validate_membership_role(v)


class MembershipResponse(BaseModel):
    """Active membership row as returned by the members listing."""

    user_id: str
    account_id: str
    role: str
    email: str
    display_name: str | None = None
    disabled: bool = False
    created_at: datetime
    # Distinguishes active rows from `PendingMembershipResponse` in the
    # combined listing.
    pending: bool = False


class PendingMembershipResponse(BaseModel):
    id: int
    email: str
    account_id: str
    role: str
    invited_by_user_id: str | None = None
    created_at: datetime
    pending: bool = True

    model_config = {"from_attributes": True}


class SignupRequest(BaseModel):
    """Body for `POST /admin/signup` (invoked by the Web UI on behalf
    of the user submitting the public signup form).

    The Web UI is responsible for rate-limiting + invite gating; the
    catalog just enforces the reserved-email guard and consumes
    matching pending invitations atomically.
    """

    email: str
    password: str
    display_name: str | None = None


class SignupResponse(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    # IDs of accounts where a pending invitation was consumed into a
    # real membership as part of this signup. Empty for a bare signup
    # with no outstanding invites.
    consumed_account_ids: list[str] = []


class DisableUserRequest(BaseModel):
    disabled: bool


# ---------------------------------------------------------------------------
# Sharing schemas (Wave 9.4)
# ---------------------------------------------------------------------------

class ShareCreateRequest(BaseModel):
    """Body for POST /skills/{id}/shares + skillset parallel.

    Email normalization + regex validation happens in the service
    layer so the schema accepts raw input; the service surfaces a
    400 with a specific message.
    """

    email: str


class ShareResponse(BaseModel):
    id: int
    email: str
    granted_by_user_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Membership-removal preview (Wave 9.6)
# ---------------------------------------------------------------------------

class RemovalPreviewTarget(BaseModel):
    user_id: str
    email: str
    role: str


class RemovalPreviewResponse(BaseModel):
    owns_skills: int
    owns_skillsets: int
    owns_agents: int
    default_target: RemovalPreviewTarget | None = None
    target_members: list[RemovalPreviewTarget] = []
