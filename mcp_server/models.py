from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Skill(Base):
    __tablename__ = "skills"
    __table_args__ = (
        UniqueConstraint("id", "version", name="uq_skill_id_version"),
    )

    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, default="")
    version: Mapped[str] = mapped_column(String, nullable=False)
    is_latest: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Wave 8a -> 9.2: visibility ∈ {public, account, private}.
    #   public  — any authenticated agent; anonymous UI visitors.
    #   account — members of the owning account + allow list.
    #   private — owner, account-admins, and account-member allow-list
    #             entries only.
    # The column stays free-form TEXT; the Pydantic validator gates
    # the set.
    visibility: Mapped[str] = mapped_column(
        String, nullable=False, default="private", server_default="private"
    )
    # Wave 9.2: tenant boundary + ownership.
    account_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    owner_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    owner_email_snapshot: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class SkillFile(Base):
    """One file within a skill version's bundle.

    See spec/skill-bundles.md for the storage model. The row doubles as the
    metadata index and (for the SQLite prototype) the content store; moving
    `content` to an object store later only affects this table.
    """

    __tablename__ = "skill_files"

    skill_pk: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("skills.pk", ondelete="CASCADE"),
        primary_key=True,
    )
    path: Mapped[str] = mapped_column(String, primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False)


class Skillset(Base):
    __tablename__ = "skillsets"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, default="")
    # Wave 8a -> 9.2: visibility ∈ {public, account, private}. See
    # Skill.visibility for the full semantics.
    visibility: Mapped[str] = mapped_column(
        String, nullable=False, default="private", server_default="private"
    )
    # Wave 9.2: tenant boundary + ownership (parallel to Skill).
    account_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    owner_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    owner_email_snapshot: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    skill_links: Mapped[list["SkillSkillset"]] = relationship(
        "SkillSkillset",
        back_populates="skillset",
        cascade="all, delete-orphan",
    )


class SkillSkillset(Base):
    """Version-agnostic join table: associates a skill id with a skillset."""

    __tablename__ = "skill_skillsets"

    skill_id: Mapped[str] = mapped_column(String, primary_key=True)
    skillset_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("skillsets.id", ondelete="CASCADE"),
        primary_key=True,
    )

    skillset: Mapped["Skillset"] = relationship(
        "Skillset", back_populates="skill_links"
    )


class User(Base):
    """Web UI operator identity.

    Wave 9 shape. The identity row carries no role — authority lives
    on :class:`AccountMembership` rows. The hardcoded superadmin
    (see spec §2.3) is never stored here; id `"0"` is reserved via
    the CHECK constraint below.

    `last_active_account_id` stamps the user's last-used account so
    login can land them back in the same tenant context without a
    picker (spec §3.4).
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("id != '0'", name="ck_users_id_not_reserved"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)  # uuid4 hex
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    disabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    last_active_account_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Account(Base):
    """A tenant container.

    Wave 9. Every account has ≥ 1 `account-admin` membership at all
    times (last-admin guard enforced at the service layer in
    `mcp_server.accounts`). Accounts are flat — there is no
    parent/child hierarchy, by design (spec §2.1).
    """

    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # uuid4 hex
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class AccountMembership(Base):
    """Join row: user × account × role.

    Wave 9. A user can appear in multiple accounts with different
    roles. Composite PK `(user_id, account_id)` prevents the same
    user from holding two roles in the same account (bump the role
    instead).
    """

    __tablename__ = "account_memberships"

    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    account_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("accounts.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    # 'account-admin' | 'contributor' | 'viewer'
    role: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class PendingMembership(Base):
    """Invitation for an email that hasn't signed up yet.

    Wave 9. Consumed atomically during `POST /signup` when a new
    user's normalized email matches. No verification tokens in the
    initial cut — that's a deferred SMTP wave (spec §3.5.2).
    """

    __tablename__ = "pending_memberships"
    __table_args__ = (
        UniqueConstraint(
            "email", "account_id", name="uq_pending_membership_email_account"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    invited_by_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class AuditEvent(Base):
    """Append-only audit trail for forensics (item H).

    Written by `mcp_server.audit.record()`. Every write-class
    superadmin action, membership mutation, catalog move, and
    user-lifecycle event emits one row. Structured `diff` JSON
    carries op-specific details (old/new roles, resource ids, etc.)
    so the Web UI and log aggregators can render the event without
    a separate enrichment step.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    actor_email: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    actor_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Nullable: platform-level events (superadmin login, account
    # create) aren't scoped to a tenant.
    account_id: Mapped[str | None] = mapped_column(String, nullable=True)
    target_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    target_id: Mapped[str | None] = mapped_column(String, nullable=True)
    diff: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class SkillShare(Base):
    """Email-keyed allow-list entry for a skill (Wave 9.4).

    `skill_id` is the logical skill id (shared across versions).
    `email` is normalized (``.strip().lower()``) before insert. No FK
    from `email` to `users.email` on purpose — shares for
    not-yet-registered emails must persist until signup resolves
    them.
    """

    __tablename__ = "skill_shares"
    __table_args__ = (
        UniqueConstraint("skill_id", "email",
                         name="uq_skill_shares_skill_email"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    skill_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)
    granted_by_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class SkillsetShare(Base):
    """Email-keyed allow-list entry for a skillset. Parallel to
    :class:`SkillShare`."""

    __tablename__ = "skillset_shares"
    __table_args__ = (
        UniqueConstraint("skillset_id", "email",
                         name="uq_skillset_shares_skillset_email"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    skillset_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("skillsets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)
    granted_by_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    skillsets: Mapped[list[str]] = mapped_column(JSON, default=list)
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    scope: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Wave 9.2: every agent lives in exactly one account. JWTs
    # minted for the agent carry this id at the authorization layer
    # (wired in Wave 9.5 with the session model).
    account_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    owner_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    owner_email_snapshot: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
