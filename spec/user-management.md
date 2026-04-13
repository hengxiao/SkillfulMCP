# User Management — Accounts, Memberships, Roles, and Allow Lists

Status: **proposed** (Wave 9). Builds on:

- [Wave 8b](visibility-and-accounts.md#wave-8b-users--roles--shipped) — DB-backed users with `admin` / `viewer` roles.
- [Wave 8a](visibility-and-accounts.md#1-visibility-model) — `public` / `private` flag on skills + skillsets.

Wave 9 introduces **accounts** as the tenant boundary, and decouples
users from any single account: a user has an **identity** (email,
password, display name) and zero or more **memberships**, each
carrying a role in a specific account. The same user can be an
`account-admin` in one org and a `viewer` in another. Agents and
their tokens, catalog content, and sharing all live inside an
account; an email-based allow list is the escape hatch for targeted
cross-account sharing.

---

## 1. Motivation

Wave 8b's flat operator pool doesn't scale beyond a single ops team:

- Every admin sees every user; no isolation between teams or
  customers sharing the deployment.
- Every viewer sees every skill; no way to say "only the billing
  team sees the billing skills."
- Sharing outside the operator pool requires flipping a skill
  `public`, which exposes it to *all* authenticated agents.

Wave 9 fixes this with three concepts:

- **Accounts** — the tenant boundary. Skills, skillsets, agents,
  and memberships all belong to an account.
- **Memberships** — the join between users and accounts, carrying a
  role. Users are independent of any single account; their role is
  per-account.
- **Email-based allow lists** — targeted cross-account sharing
  without granting account membership.

---

## 2. Accounts, users, and roles

### 2.1 Shape

```
                 superadmin  (platform-level, not in any account, singleton)
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
    Account A    Account B    Account C

    (memberships — user × account × role)

    alice   → Account A as account-admin
           → Account B as contributor
    bob     → Account A as contributor
    carol   → Account B as account-admin
           → Account C as account-admin
```

An **account** is a container for users (via memberships), skills,
skillsets, and agents. Users carry identity (email, password hash);
their **authority** in any account comes from a membership row.

Roles, per membership:

| Role | Who assigns it | Authority within the account |
| ---- | -------------- | ----------------------------- |
| `account-admin` | superadmin, or another account-admin of the same account | Manage memberships in this account. Create / edit / delete accounts' catalog rows + agents. Mint tokens for agents in this account. Multiple account-admins are allowed. |
| `contributor` | any account-admin of this account | CRUD on resources they own **within this account**. Read on account-scoped resources + shared-with-them + public. |
| `viewer` | any account-admin of this account | Read-only in this account. Browse account-scoped + shared-with-them + public. No create. |

Plus one platform-level role:

| Role | Notes |
| ---- | ----- |
| `superadmin` | Platform owner. No membership table involvement. Creates accounts, mints the first account-admin in each, sees and overrides everything across all accounts. **Singleton**, cannot be deleted. |

### 2.2 Multiple account-admins per account

Unlike the earlier draft, Wave 9 **allows any number of account-admins
in an account**. Matches the GitHub org / Google Workspace model —
real orgs want redundancy. Any existing account-admin can promote
another member of their account to `account-admin` via a plain role
update; this matches familiar SaaS RBAC (GitHub org owner, Google
Workspace super-admin groups) and is intentional. A compromised
admin session can therefore mint more admins — the mitigation lives
at the audit-log / SSO layer, not the RBAC layer.

**Last-admin guard.** An account must have at least one non-disabled
`account-admin` membership at all times. Enforced inside a
transaction to prevent the "two admins delete themselves
concurrently" race:

```python
with db.begin():
    # Lock the target membership row so a second concurrent delete
    # can't see the same count.
    victim = db.execute(
        select(AccountMembership)
        .where(AccountMembership.user_id == user_id,
               AccountMembership.account_id == account_id)
        .with_for_update()
    ).scalar_one()

    if victim.role == "account-admin":
        # Count *other* active account-admins inside the same lock.
        remaining = db.scalar(
            select(func.count()).select_from(AccountMembership)
            .where(AccountMembership.account_id == account_id,
                   AccountMembership.role == "account-admin",
                   AccountMembership.user_id != user_id)
            # inner join to users to filter disabled=False
        )
        if remaining == 0:
            raise LastAdminError(
                "Cannot remove the last account-admin of this account. "
                "Promote another member to admin first, or delete the account."
            )
    db.delete(victim)
```

Returns 409 at the HTTP layer with the hint above. No transfer flow
is needed — promoting a contributor to `account-admin` is a plain
role update since there's no uniqueness to preserve.

### 2.3 Superadmin — hardcoded, env-configured, not in the database

Superadmin is **not a `users` row**. It's a fixed identity with a
reserved pseudo-email:

- **Email:** `superadmin@skillfulmcp.com` — hardcoded in the
  codebase, not configurable. The domain is reserved; user
  registration refuses emails that match this string (see §3.5.1).
- **Password:** `MCP_SUPERADMIN_PASSWORD_HASH` env var (bcrypt),
  required at startup. Missing or empty refuses the process to
  start with a clear error.

Only the password is operator-tunable. Hardcoding the email means
there's no way to accidentally collide with a regular user's email,
and no "which env variable takes precedence" confusion — the
superadmin is a fixed point of the system.

The superadmin's user id is the literal string `"0"` — reserved and
never issued to a real user. `uuid4().hex` is 32 hex chars and can
never equal `"0"`, so the id space is disjoint by construction; the
`users` table additionally has a CHECK refusing `id = '0'` as a
defense-in-depth safety net.

Properties that follow:

- **Singleton.** Exactly one pair of env values is valid at a time;
  there's no DB row to accidentally duplicate.
- **Unchangeable via the UI.** No form, API endpoint, or SQL
  migration edits the superadmin. Changing the superadmin means
  rotating env vars + restarting the process. That friction is
  intentional: operators should not be able to change who owns the
  platform without touching infra.
- **Not listed in any UI members table or users page** — it's an
  out-of-band identity, not a row in a queryable set. The UI
  surfaces "Logged in as SUPERADMIN" in the topbar as a visual
  marker.
- **No transfer flow.** "Transfer superadmin" is literally rotating
  the env — the deferred wave in §10 becomes "add a CLI subcommand
  that rehashes a new password for the env var and reloads it,"
  not a DB operation.

At login, `authenticate_via_server` first checks whether the
incoming email equals the hardcoded superadmin string; on match it
verifies against `MCP_SUPERADMIN_PASSWORD_HASH` and returns a
superadmin-flagged Operator without touching the DB. Regular users
fall through to the existing DB lookup.

The session grows one new flag:

```
session.user = {
    "user_id": "0" | "<uuid4hex>",
    "email":   "<email>",
    "is_superadmin": bool,
    "active_account_id": <uuid4hex> | None,
    "active_role":      "account-admin" | "contributor" | "viewer" | None,
}
```

`is_superadmin` is stamped at login and cannot be changed thereafter
for this session. `active_account_id` is None for a superadmin who
hasn't switched into a tenant context yet; see §3.4 for selection
rules.

### 2.4 Transition from Wave 8b

The `0004_accounts_and_memberships` migration (order-sensitive —
statements must execute in this sequence):

1. Create `accounts` and `account_memberships` tables.
2. Drop `users.role` — the column is no longer needed (§2.3
   handles superadmin out-of-band; every DB row is just a user
   identity).
3. Add `users.last_active_account_id` column with
   `ForeignKey("accounts.id", ondelete="SET NULL")` (nullable).
   This happens AFTER step 1 so the FK target exists.
4. Create one `default` account.
5. Every existing 8b `viewer` becomes a `viewer` membership in
   `default`.
6. Every existing 8b `admin` becomes an `account-admin` membership
   in its own fresh account named `"{email}'s team"`. Each admin
   gets their own tenant; admins who previously shared the flat
   pool don't silently merge.
7. Add the `account_memberships` indices (composite PK on
   `(user_id, account_id)` plus a secondary on `account_id` alone
   for membership-list lookups).
8. Add the `users.id != '0'` CHECK so no DB row can accidentally
   collide with the hardcoded superadmin id. (Wave 8b rows use
   uuid4 hex which is 32 chars and cannot equal `"0"`; the CHECK
   passes against existing rows without a table rewrite.)

The old `MCP_WEBUI_OPERATORS` env var stays supported for
bootstrapping the `users` table on an empty DB (same semantics as
Wave 8b). Every entry becomes a regular user with an `account-admin`
membership in `default`. Empty table + empty env still logs the
"refuse all logins" warning.

Superadmin identity comes from the `MCP_SUPERADMIN_PASSWORD_HASH`
env var (see §2.3). This is **required** in Wave 9 deployments —
missing or empty refuses startup with a clear error message.

---

## 3. Data model

### 3.1 `accounts` table

```python
class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # uuid4 hex
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = ...
    updated_at: Mapped[datetime] = ...
```

Account `name` is unique for the operator UX (pickable in
dropdowns); `id` is what FKs reference.

### 3.2 `account_memberships` table

```python
class AccountMembership(Base):
    __tablename__ = "account_memberships"

    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = ...
    updated_at: Mapped[datetime] = ...
```

Key properties:

- `(user_id, account_id)` is the primary key — a user can't be in
  the same account twice with different roles. If you need more
  capability, bump the role.
- Both FKs `ON DELETE CASCADE`: deleting a user or account removes
  their membership rows. Catalog ownership is handled separately
  (§3.5.4 / §4.1).
- `role` values: `account-admin | contributor | viewer`.
- No superadmin memberships — superadmin is identity-level.

### 3.3 `users` table changes

The Wave 8b `users.role` column is **dropped**. Every DB user row is
just an identity — all authority comes from memberships. Superadmin
lives in env vars (§2.3), not the DB.

Fields: `id` (uuid4 hex, never `"0"`), `email` (unique), `display_name`,
`password_hash`, `disabled`, timestamps, `last_login_at`. Unchanged
from Wave 8b minus the role column.

Add a CHECK: `id != '0'` to guarantee no DB row ever collides with
the hardcoded superadmin identity.

**Session state** — see §2.3 for the full shape. The rules that
matter outside that section:

- `memberships` are **not cached on the session**. Every request
  re-resolves the caller's memberships from the DB via an indexed
  lookup (one `SELECT * FROM account_memberships WHERE user_id = ?`
  per request, ≤ 10 rows typically). This avoids stale-role
  surprises after a role change in another session.
- `active_account_id` and `active_role` are stamped at login
  (§3.4) and updated only by `POST /session/switch-account`.
- `is_superadmin` is stamped at login by matching the email against
  `MCP_SUPERADMIN_EMAIL`. Cannot be mutated mid-session.

### 3.4 Active-account selection at login

A user with multiple memberships needs an active account picked at
login so `POST /skills` etc. know where to stamp `account_id`.

Selection order:

1. **Last-used.** A new `users.last_active_account_id` column
   (nullable) records the last `active_account_id` the user held
   before their previous logout. If set and the user still has a
   membership there, use it.
2. **Oldest membership.** Otherwise, pick the earliest-created
   membership for this user (deterministic tie-break: `account_id`
   lexicographic).
3. **None.** If the user has zero memberships, log them in with
   `active_account_id = None` and render the landing page with a
   banner: "You've been removed from every account. Ask an admin
   to add you back or sign out."

For a superadmin (`is_superadmin=True`), `active_account_id` is
`None` at login; they pick one via the switcher when they need to
create catalog content in a specific account. Read-only actions
(listing all accounts, etc.) work without an active account.

`last_active_account_id` is updated on every
`POST /session/switch-account` and on logout. The column has
`ON DELETE SET NULL` against `accounts.id`, so an account-delete
(§3.7) quietly clears it rather than breaking the next login.

**Stale-session self-heal.** If a user is removed from their
current `active_account_id` by another session (e.g., an admin
kicks them), their own session's `active_account_id` still points
at an account they no longer belong to. On the next request, the
membership-resolution layer detects the mismatch, clears
`active_account_id` + `active_role`, and re-runs the §3.4
selection inline. The request then proceeds against the new
active account (or the landing banner if they have no memberships
left). No explicit re-login is required.

### 3.5 User / membership lifecycle

Wave 9 moves user creation out of the admin surface entirely. Users
self-register, and accounts are created by the users who want them.
Account-admins manage **account and membership access**, not user
rows. There is no "admin deletes a user" path — admins can only
remove someone's **membership**; the user's identity row persists
as long as the user exists.

#### 3.5.1 User signup (self-service)

Public endpoint:

```
POST /signup
  body: {email, password, display_name}
  - Creates a users row, bcrypt-hashes the password.
  - Refuses if `email == 'superadmin@skillfulmcp.com'` (reserved).
  - Refuses duplicate emails (existing account offers "Sign in").
  - Settings-gated: `MCP_ALLOW_PUBLIC_SIGNUP` (default: false).
    When false, `/signup` only accepts emails that are listed in
    at least one pending invitation (§3.5.2). Deployments that
    want open signup flip the flag.
  - On success, consumes every `pending_membership` row matching
    the new user's email: insert the corresponding membership
    rows, delete the pending rows. All in the same transaction.
  - Logs the signup event with `invited_memberships=[...]` for
    audit.
```

New users with no invitation and no `MCP_ALLOW_PUBLIC_SIGNUP=true`
cannot land anywhere — the signup endpoint 403s. A landing-page
banner explains what to do ("ask the account admin to invite you").

Self-delete:

```
DELETE /users/me
  - Removes the caller's user row. Cascades all their memberships.
  - Last-admin guard runs per account: if removing the user's
    account-admin membership would leave an account with zero
    active admins, the call 409s with a list of the affected
    accounts. The user must either promote someone else or delete
    each account first.
  - Owned catalog rows follow the membership-removal reassignment
    rule (§3.5.4) per account.
```

No other hard-delete path for users exists. Abuse moderation: the
superadmin can `disabled=true` any user, which blocks login and
hides them from membership pickers without removing the row.

#### 3.5.2 Invite / add / remove memberships

An account-admin doesn't create user rows — they invite emails.
If the email matches an existing user, the membership is added
immediately. If not, a **pending invitation** row is stored and
consumed when that email eventually signs up (§3.5.1).

New table `pending_memberships`:

```python
class PendingMembership(Base):
    __tablename__ = "pending_memberships"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(
        String, ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    invited_by_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    created_at: Mapped[datetime] = ...
    __table_args__ = (UniqueConstraint("email", "account_id",
        name="uq_pending_membership_email_account"),)
```

Security note: Wave 9 does not send invitation emails or use
verification tokens. That's deferred to Wave 9.1 along with SMTP.
Implication: if evil-bob signs up with alice@corp.com before Alice
does, he inherits the pending memberships intended for her. This is
a known gap that the email-verification follow-up closes. Until
then, deployments with sensitive sharing should keep
`MCP_ALLOW_PUBLIC_SIGNUP=false` (the default) and trust that
operators are picking email recipients who have not yet registered
but will.

Endpoints:

```
POST   /accounts/{account_id}/members
  body: {email, role}
  - If a users row with that email exists → insert
    account_memberships(user_id, account_id, role).
  - Else → insert pending_memberships(email, account_id, role).
  - 409 if (email, account_id) already present in either table.

GET    /accounts/{account_id}/members
  - Returns the merged list of active memberships + pending
    invitations (distinguished by a `pending: bool` flag).

PUT    /accounts/{account_id}/members/{user_id}
  body: {role}
  - Active memberships only; pending entries are re-issued via
    DELETE + POST.
  - Role-change rules: §3.5.3.

DELETE /accounts/{account_id}/members/{user_id}?new_owner_id=<uid>
  - Remove the membership; last-admin guarded.
  - Optional new_owner_id reassigns the departing user's catalog
    rows in this account (§3.5.4).
  - Does NOT delete the user row.

DELETE /accounts/{account_id}/pending/{pending_id}
  - Revoke a pending invitation.
```

Only superadmin and account-admins of `account_id` can call these.

#### 3.5.3 Role changes

- Changing a membership's role happens on
  `PUT /accounts/{account_id}/members/{user_id}` with body
  `{role: "<new>"}`.
- Any account-admin can change any other membership's role in
  their account, including promoting a contributor to another
  account-admin (§2.2 flagged this as intentional).
- An account-admin cannot demote themselves from `account-admin`
  if they'd become the last admin — the last-admin guard rejects.
- Superadmin can change any membership's role in any account.

#### 3.5.4 Catalog reassignment on membership removal

When a membership is removed (either via §3.5.2 DELETE or via
§3.5.1 self-delete), the departing user's owned catalog rows in
**that specific account** need a new owner. The handler accepts an
optional `new_owner_id` (a user with membership in the same
account); default is the oldest account-admin of the account.
Auto-promotion rule:

| What the target inherits | Minimum membership role |
| ------------------------ | ------------------------ |
| owned skills / skillsets | `contributor` |

A `viewer` who is picked as the target is promoted to `contributor`
in that account. Promotions to `account-admin` never happen via
the inheritance path — that's always an explicit operator action.

### 3.6 Create an account (any logged-in user)

Account creation is **self-service**. Any authenticated user can
call:

```
POST /accounts
  body: {name}
  - Creates an accounts row with name (unique check).
  - Atomically inserts an account_memberships row for the caller
    with role='account-admin'.
  - Updates the caller's users.last_active_account_id so their
    next request lands in the new account.
  - Rate-limited: MCP_ACCOUNT_CREATE_PER_USER_PER_DAY (default 5)
    throttles per-user creation to prevent runaway tenant spam.
```

Account-admins do **not** mint other accounts for their users —
an admin's authority is scoped to memberships and catalog within
their own account. If a member wants their own separate account,
they create it themselves and optionally invite their
collaborators.

The superadmin does not participate in the normal create flow —
they're oversight, not a tenant creator. They can still call
`POST /accounts` (any user can), but it's not their primary role.

### 3.7 Delete an account

Deleting an account is authorized for any `account-admin` of that
account OR the superadmin. Because `account_id` is **NOT NULL** on
skills, skillsets, and agents (§4.1), the handler cannot orphan
catalog rows — it must cascade-delete them as part of the same
transaction.

Wave 9 requires a double interlock:

1. Query param `?confirm_user_count=<N>&confirm_skill_count=<M>`
   must match the current counts exactly. Fat-finger protection.
2. Query param `?cascade_catalog=1` required when `confirm_skill_count
   + confirm_skillset_count + confirm_agent_count > 0`. Without this
   flag, the endpoint returns 409 with "account still has catalog
   content; set cascade_catalog=1 to hard-delete, or move content
   first."

With both interlocks satisfied, the handler:

1. `DELETE FROM skill_shares WHERE skill_id IN (SELECT id FROM skills WHERE account_id = ?)`
   (and the skillset equivalent) — shares cascade away.
2. `DELETE FROM skills WHERE account_id = ?` (cascades to
   `skill_files` via existing FK).
3. `DELETE FROM skillsets WHERE account_id = ?` (cascades to
   `skill_skillsets`).
4. `DELETE FROM agents WHERE account_id = ?`.
5. Memberships cascade automatically via their FK.
6. `DELETE FROM accounts WHERE id = ?`.

All in one transaction. Emits one `account.deleted` structured log
line per affected row for forensics.

Last-admin guard does not apply here — deleting the whole account
is a separate concept from demoting the last admin within it.

---

## 4. Catalog: ownership, visibility, sharing

### 4.1 Account-scoped ownership

New columns on `skills`, `skillsets`, and `agents`:

```python
account_id: Mapped[str] = mapped_column(
    String, ForeignKey("accounts.id", ondelete="RESTRICT"),
    nullable=False, index=True,
)
owner_user_id: Mapped[str | None] = mapped_column(
    String, ForeignKey("users.id", ondelete="SET NULL"),
    nullable=True, index=True,
)
owner_email_snapshot: Mapped[str | None] = mapped_column(
    String, nullable=True,
)
```

- `account_id` is **required** — the tenant boundary. Stamped from
  the session's `active_account_id` at create time. Immutable
  thereafter in Wave 9 (a future wave can add move-between-accounts).
  `ON DELETE RESTRICT` is deliberate: it makes accidental cascades
  from a raw `DELETE FROM accounts` impossible. The account-delete
  handler in §3.7 issues explicit `DELETE`s on child rows in order
  before deleting the account, opting into the cascade explicitly.
- `owner_user_id` is the creator. Stamped from the session. Nullable
  only because a user can be deleted out from under their owned
  rows; the membership-removal handler (§3.5) reassigns proactively
  so this SET NULL branch is a safety net, not a primary path.
- `owner_email_snapshot` is a denorm so the UI can show
  `"owned by deleted-user@corp.com"` after a null-out.

There is **no pre-Wave-9 catalog sweep.** Wave 9 has no production
deployment to migrate; the migration creates the columns with
non-null constraints from day one and existing rows (if any) get a
one-shot assignment during the migration to a single `default`
account owned by the first env-bootstrap user. Fresh deployments
stamp every row at create time.

### 4.2 Visibility tiers

Three tiers on `visibility`:

| Value | Who can read (in addition to the owner) |
| ----- | ---------------------------------------- |
| `public` | Anyone — any authenticated agent, anonymous UI visitors. |
| `account` | **(default after migration)** any user with a membership in the resource's account; plus allow-list emails (cross-account OK). |
| `private` | Strictest. Only users who **already have access to the resource's account** — i.e., hold a membership — AND are either the owner, an `account-admin` of the account, or on the allow list. Allow-list entries for emails **without** an account membership have no effect on private resources. |

Rationale for private's extra gate: operators asked for a
"confidential within the org" tier. If the allow list could grant
cross-account access to private resources, it would defeat the
"private requires account access" property. So for private we
intersect the allow list with the membership set.

Migration from Wave 8a (no production to preserve, but the mapping
is documented for completeness): `public` stays `public`;
`private` becomes `account`. Owners who want stricter semantics
can flip to `private` afterward.

### 4.3 Allow list

Email-keyed `skill_shares` / `skillset_shares` tables, unchanged
from earlier drafts:

```python
class SkillShare(Base):
    __tablename__ = "skill_shares"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    skill_id: Mapped[str] = mapped_column(
        String, ForeignKey("skills.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)
    granted_by_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    granted_at: Mapped[datetime] = ...
    __table_args__ = (UniqueConstraint("skill_id", "email",
        name="uq_skill_share_skill_email"),)
```

`SkillsetShare` is parallel.

Email normalization: `.strip().lower()` at insert. Basic regex
validation. Duplicates are UNIQUE-constraint no-ops.

Interaction with visibility (recap):

- `public` → allow list is redundant; the UI refuses to add entries
  with a 400 ("resource is already world-readable").
- `account` → allow list grants cross-account read access. An
  entry for an email that has no membership in the resource's
  account still works — that's the whole point.
- `private` → allow list only takes effect for emails that **also**
  have a membership in the resource's account. An orphan allow-list
  entry is a silent no-op; the UI surfaces this as
  `[needs account access]` on the row so the grantor can fix it.

### 4.4 Combined visibility check

```python
def can_read(resource, user, user_memberships) -> bool:
    if resource.visibility == "public":
        return True
    if user is None:
        return False
    if user.role == "superadmin":
        return True

    in_account = resource.account_id in user_memberships
    role_in_account = user_memberships.get(resource.account_id)  # None | str

    if resource.owner_user_id == user.id:
        return True

    if resource.visibility == "account":
        if in_account:
            return True
        if share_exists(resource, user.email):
            return True
        return False

    # visibility == "private"
    if not in_account:
        return False
    if role_in_account == "account-admin":
        return True
    if share_exists(resource, user.email):
        return True
    return False
```

Notes:

- `user_memberships` is a `{account_id: role}` dict fetched fresh
  from the DB on **every request**, not cached on the session. An
  indexed `SELECT * FROM account_memberships WHERE user_id = ?`
  returns ≤ 10 rows typically — cheap. Avoids stale-role surprises
  after a role change in another session.
- `account-admin` implicitly sees all catalog content in their
  account — matches GitHub org-owner UX. Contributors / viewers
  see only their own private resources + the ones explicitly shared
  with them.
- Cross-account allow-list sharing works for `account` visibility;
  for `private`, the recipient must also be a member.

### 4.5 Agent authorization is unchanged

Allow lists and membership state still do **not** widen an agent's
JWT. Agents are first-class entities (§4.6); the authorization layer
for agent tokens uses `public` + explicit per-agent grants + (new in
Wave 9) the agent's own account membership for account-scoped
content.

### 4.6 Agents are account-scoped

`agents.account_id` is **NOT NULL** from day one. Every agent lives
in exactly one account; there is no migration window for
account-less agents (Wave 9 has no production to preserve, and the
authorization model would be incoherent with an account-less agent).
An agent can only hold grants that are reachable from its own
account:

- `public` skills / skillsets → always allowed.
- `account` visibility → allowed only for resources in the same
  account.
- `private` → never — agents don't have allow-list eligibility
  (they have no email to resolve against, and we don't want a
  side-channel granting agent access to private data).

Token issuance:

- `POST /token` for an agent is gated on the caller being a
  `superadmin` OR having an `account-admin` membership in the
  agent's account.
- Minted tokens carry only the grants valid under the rules above.
  Attempts to narrow *broader* than the agent's registered
  skills/skillsets still 400 (unchanged Wave 8c rule); attempts to
  narrow to values outside the agent's account's reach are
  similarly rejected.

---

## 5. API changes

### 5.1 New endpoints

User identity (self-service):

```
POST   /signup                             public; see §3.5.1
  body: {email, password, display_name}
  - Refuses the reserved superadmin@skillfulmcp.com email.
  - Gated by MCP_ALLOW_PUBLIC_SIGNUP; when false, the email must
    match at least one pending_memberships row.
  - Consumes pending memberships into real memberships.

PUT    /users/me                           self; update display_name / password.
DELETE /users/me                           self; last-admin guarded per §3.5.1.
```

Accounts (any authenticated user):

```
POST   /accounts                           any user; see §3.6
  body: {name}

GET    /accounts                           superadmin: all; user: accounts they're a member of.
GET    /accounts/{id}                      superadmin; membership-holders for their own account.
DELETE /accounts/{id}?confirm_user_count=N&confirm_skill_count=M&confirm_skillset_count=S&confirm_agent_count=A&cascade_catalog=1
                                           account-admin of the target OR superadmin; see §3.7.
```

Memberships (account-admin or superadmin only):

```
POST   /accounts/{account_id}/members
  body: {email, role}
  - If email matches a users row → insert membership.
  - Else → insert pending_memberships row.

GET    /accounts/{account_id}/members
  - Returns active + pending, with a `pending: bool` flag.

PUT    /accounts/{account_id}/members/{user_id}
  body: {role}

DELETE /accounts/{account_id}/members/{user_id}?new_owner_id=<uid>
  - Runs the catalog-reassignment step if the departing user owns
    skills/skillsets in this account (§3.5.4). Last-admin guarded
    via SELECT ... FOR UPDATE (§2.2).

DELETE /accounts/{account_id}/pending/{pending_id}
```

There is **no** `/admin/users` surface. Admins manage memberships,
not user identities. The superadmin is oversight-only — they can
read any user row through `/accounts/{id}/members` listings but
cannot delete user rows; the self-delete endpoint is the only
removal path. The one exception is `disabled=true`, which the
superadmin can set on any user via `PUT /users/{id}/disable`
(platform-level abuse moderation).

Sharing (unchanged from earlier draft):

```
POST   /skills/{id}/shares                 owner/account-admin/superadmin
GET    /skills/{id}/shares
DELETE /skills/{id}/shares/{share_id}

POST   /skillsets/{id}/shares
GET    /skillsets/{id}/shares
DELETE /skillsets/{id}/shares/{share_id}
```

Agents (extends existing routes):

```
GET    /agents                             filters to the caller's active account
                                           (superadmin can pass ?account_id=)
POST   /agents                             body must include account_id or
                                           defaults to session.active_account_id
POST   /token                              unchanged URL; new auth:
                                           superadmin OR account-admin of
                                           the agent's account
```

Sessions / switcher:

```
POST   /session/switch-account             body: {account_id}
  - Moves the session's active_account_id + active_role to the
    target account, if the caller has a membership there.
  - Superadmin can switch into any account (acts as implicit admin).
  - Rejected with 403 otherwise.
```

### 5.2 Modified endpoints

- `POST /skills`, `POST /skillsets`: stamp `account_id` from the
  session's `active_account_id`, `owner_user_id` from session user.
  Admin-key CLI callers must pass `account_id` explicitly.
- `GET /skills`, `GET /skillsets`: filter per §4.4 using the session
  memberships. `?mine=1` → owner match. `?shared=1` → shares only.
  `?account_id=<id>` → only valid for superadmins, or for users
  whose membership set includes that id.
- `PUT`, `DELETE` on skills / skillsets: owner OR `account-admin`
  membership in `resource.account_id` OR superadmin.
- `POST /token`: see §4.6.

### 5.3 Validation

- Email on `/shares` and `/members`: trimmed + lowercased. Regex
  `^[^@]+@[^@]+\.[^@]+$`. 400 on syntax failure.
- Cannot share with your own email (no-op + 400).
- Cannot add a share to a `public` resource (400).
- Setting `visibility=private` on a resource with allow-list
  entries whose emails aren't members of the account: allowed, but
  the UI warns those entries are inert.
- Cannot remove the last `account-admin` membership of an account
  (last-admin guard).

---

## 6. Web UI changes

### 6.1 Account switcher (topbar)

A dropdown in the header showing the session's active account +
every account the user has a membership in. Selecting another
account POSTs `/session/switch-account` and reloads.

Superadmin sees every account in the dropdown plus a divider + "All
accounts" view for cross-account browsing.

### 6.2 Accounts page (superadmin)

New top-level nav item, superadmin-only. Lists every account with
columns: name, admin count, user count, skill count, created.
Row actions: open (→ /accounts/{id}), delete (with the
confirmation-count interlock).

Superadmin does NOT have a create-account form here — account
creation is the self-service flow at `/accounts/new` (§6.7), which
every authenticated user (including superadmin) can use. The
superadmin's role on this page is observe + delete, not mint.

### 6.3 Account detail page

Covers one account, scoped to the caller's view of it:

- **Members** tab: table of `{email, role, joined, last login,
  owns count}`. Invite-member form (email + role). Row actions:
  change role, remove membership.

  A small help text under the table reads:
  > Platform authority (superadmin) is not a membership and is
  > not listed here. Superadmins are managed out-of-band via
  > environment configuration.

- **Agents** tab: the usual list, filtered to this account; Mint
  Token action requires an `account-admin` membership here.
- **Catalog** tab: account's skills + skillsets summary.
- **Settings** tab (superadmin only): rename, delete-account.

### 6.4 Membership-removal dialog

```
┌─ Remove bob@corp.com from "Corp Ops" ──────────────────┐
│ Role: contributor                                      │
│ Owns 3 skills and 1 skillset in this account.          │
│                                                         │
│ Reassign ownership to:                                  │
│   ● account-admin (default) — alice@corp.com          │
│   ○ Another member:       [ dropdown ]                 │
│                                                         │
│ ⚠ If reassigning to a viewer, they'll be promoted to   │
│   contributor in this account so they can hold         │
│   owned resources.                                     │
│                                                         │
│ Bob's user account remains — he may have memberships   │
│ in other accounts.                                     │
│                                                         │
│  [ Cancel ]                      [ Remove membership ] │
└─────────────────────────────────────────────────────────┘
```

### 6.5 Sharing card (skill / skillset detail)

```
┌─ Sharing ──────────────────────────────────────────────┐
│ Visibility:                                            │
│   ○ Private — owner, account-admins, and account-      │
│              member allow-list entries only            │
│   ● Account — all members of "Corp Ops" + allow list   │
│   ○ Public  — any authenticated agent                  │
│                                                         │
│ Allow list:                                             │
│   alice@partner.com    [registered / member] [×]       │
│   bob@partner.com      [registered / no member] [×]    │
│   carol@partner.com    [pending invite]         [×]    │
│   + Add email                                           │
└─────────────────────────────────────────────────────────┘
```

The status column surfaces the `private`-allow-list nuance: entries
with `no member` are visible in the list but don't grant access.
Clicking one opens an invite-to-account shortcut for admins.

**Flipping to `private` while allow-list entries exist.** When the
operator selects `private` and there are non-member allow-list
entries, the form intercepts the submit and shows a confirmation
modal:

```
┌─ Flip to private? ─────────────────────────────────────┐
│ 2 allow-list entries are for emails that aren't        │
│ members of this account:                                │
│   • bob@partner.com                                    │
│   • carol@partner.com                                  │
│                                                         │
│ These entries will remain in the list but will NOT     │
│ grant access while visibility is `private`. They       │
│ reactivate if you flip back to `account`.              │
│                                                         │
│  [ Cancel ]   [ Set private anyway ]                   │
└─────────────────────────────────────────────────────────┘
```

The flip is logged as a structured `resource.visibility_changed`
event so the audit trail captures the silent-disable of those
entries.

The reverse flip (`private` → `account`) runs a symmetric
confirmation when non-member entries exist: "N allow-list entries
will become effective (previously-inert because they weren't
account members)." This prevents a quiet grant to users the
operator has forgotten about.

### 6.6 Landing page (already shipped, clarification)

Public catalog landing stays as shipped in commit [9b75439]. The
`account` visibility tier is hidden from anonymous visitors; the
public list stays `public` only.

When `is_superadmin=True` and `active_account_id is None`, the
landing-page counts (skills / skillsets / agents) are **platform-
wide** — sum across every account. When the superadmin switches
into an account, the counts drop to that account's scope. Regular
users always see their active account's counts.

### 6.7 Signup & create-account pages

Two new top-level pages for the self-service flow:

- `GET /signup` — form with email, password, display_name fields.
  When `MCP_ALLOW_PUBLIC_SIGNUP=false`, the form pre-validates the
  email via `GET /signup/invite-check?email=...` and refuses to
  submit unless at least one pending invitation matches.
- `GET /accounts/new` — any logged-in user can reach this;
  renders a form with a single "Account name" field and submits to
  `POST /accounts`. On success redirects to the new account's
  detail page.

---

## 7. Why allow lists / memberships don't widen agent JWTs

Agents are first-class entities in the `agents` table with admin-
configured grants. Allow lists and the account hierarchy govern
**operator UI access**, never agent capability:

- Operators are humans browsing a web UI.
- Agents are long-lived service identities with narrow scoped tokens.
- A compromised operator session can swap accounts or add itself to
  shares, but the agent's grant set lives in `agents` rows and
  doesn't change.

New in Wave 9: agents additionally carry an `account_id`, which
bounds which account-scoped resources they can see (§4.6). This
tightens rather than loosens the rule.

---

## 8. Role-check implementation

Four dependencies:

```python
def require_superadmin():
    """Fails 403 unless session.is_superadmin is True.
    Used for platform-wide actions like create-account,
    delete-account, hard-delete-user."""

def require_membership(path_param: str = "account_id"):
    """Fails 403 unless the caller has a membership in the path
    account_id, OR is superadmin. Superadmin always passes."""

def require_membership_role(path_param: str = "account_id",
                            *allowed_membership_roles):
    """Like require_membership but additionally checks the
    membership's role ∈ allowed_membership_roles. Superadmin
    short-circuits to True here too — effectively an implicit
    account-admin of every account for authz purposes.

    When the superadmin path short-circuits on a write-class
    endpoint (POST/PUT/DELETE), the dep emits a structured audit
    log line:
       event=admin.superadmin_acting
       actor_email=superadmin@skillfulmcp.com
       account_id=<path>  method=<METHOD>  path=<URL>
       target_user_id=<id if present in path>
       resource_id=<id if present in path>
    so the forensics trail captures who took the action, which
    account it touched, and which specific row was affected (if
    applicable). Handlers can extend the log bag with additional
    `interesting_fields` via a request-scoped helper if they have
    more to record (e.g., `old_role` and `new_role` on a
    role-change)."""

def require_catalog_access(resource_kind: str,
                           op: str = "read"):
    """Per-resource predicate wrapped as a dep. Fetches the resource,
    resolves the caller's memberships, and applies §4.4 (read) or
    §5.2 (write) rules. Superadmin always passes; write access emits
    the same audit log line as above."""
```

Gating:

- `POST /accounts` → `require_authenticated_user()` (any logged-in user; rate-limited).
- `DELETE /accounts/{id}` → `require_membership_role("id", "account-admin")` (superadmin short-circuits).
- `POST /accounts/{id}/members`, `PUT .../members/{user_id}`, `DELETE .../members/{user_id}` → `require_membership_role("id", "account-admin")`.
- `POST /skills`, `POST /skillsets` → `require_membership_role("<session.active_account_id>", "contributor", "account-admin")`.
- `PUT`/`DELETE` skills/skillsets → `require_catalog_access(..., op="write")`.
- `POST /token` → `require_membership_role("<agent.account_id>", "account-admin")`.

The `active_account_id` check for resource creation is a session
property, so the dep reads it via `Request.session`; it does **not**
trust a client-supplied `account_id` unless the caller is superadmin.
A superadmin without an `active_account_id` trying to create a
skill gets a 400 with "switch into an account first, or pass
`account_id` explicitly."

---

## 9. Tests

- `tests/test_accounts.py` — create / list / delete accounts;
  superadmin-only gating; last-admin guard at the service layer.
- `tests/test_memberships.py` — add / remove / change-role; last-
  admin guard (409 on the wire); cross-account attempts blocked;
  the same user with different roles in two accounts.
- `tests/test_superadmin.py` — env-hardcoded superadmin (match on
  `MCP_SUPERADMIN_EMAIL`, verify against `MCP_SUPERADMIN_PASSWORD_HASH`);
  `is_superadmin` session flag; `users.id = '0'` CHECK refuses
  collisions; startup fails when either env var is missing;
  superadmin writes emit the `admin.superadmin_acting` audit line.
- `tests/test_user_delete.py` — remove-from-account reassigns; hard-
  delete-user refused while the user holds any account-admin
  membership.
- `tests/test_visibility.py` — extend Wave 8a tests with the new
  tiers, including the private-requires-membership rule and the
  "inert" private allow-list entry.
- `tests/test_shares.py` — cross-account sharing (account tier) +
  private-visibility allow-list interaction + inert-entry surface
  in GET.
- `tests/test_agents.py` — agents now carry `account_id`; list
  filters by active account; agent creation defaults to the
  session active account.
- `tests/test_api_token.py` — extend to assert minting is refused
  unless the caller has an `account-admin` membership in the
  agent's account.
- `tests/test_session_switch.py` — `/session/switch-account` moves
  the active membership; rejected for accounts the caller isn't in.
- `tests/test_webui_accounts.py` — UI for accounts list, account
  detail tabs, membership-removal dialog, sharing card tiers.

Coverage gate stays at 85%. New code ≈ 1,000 LoC; tests ≈ 700 LoC.

---

## 10. Out of scope

- **Group-based shares** (`@customer.com` wildcard, LDAP groups).
- **Per-version shares** — skill-id-keyed, applies to all versions.
- **Time-boxed shares** (`expires_at`). One column / read filter,
  easy to add later.
- **Audit log** of membership + sharing + account operations.
  Structured log lines emitted today; queryable audit table is a
  follow-up tracked in
  [visibility-and-accounts.md §out-of-scope](visibility-and-accounts.md#out-of-scope-call-out-for-clarity).
- **SMTP invitations** — Wave 9 stores pending shares + initial
  passwords as rows; the UI offers a "copy invite link" fallback.
  Ship email delivery separately (Wave 9.1).
- **Transfer-superadmin** flow. Wave 9 treats the superadmin
  identity as env-configured; rotation means rotating
  `MCP_SUPERADMIN_EMAIL` + `MCP_SUPERADMIN_PASSWORD_HASH` and
  restarting. A future wave can add a CLI subcommand that rehashes
  a new password + reloads the env without a full restart.
- **Moving catalog rows between accounts.** `account_id` on skills /
  skillsets is immutable after create. Future wave can add
  `POST /admin/skills/{id}/move-account` for cross-account content
  migration.
- **Group roles beyond the three.** No `editor` / `auditor` /
  `release-manager` in Wave 9. Add when an operator org actually
  asks for the split.
- **Per-account SSO / OIDC.** The existing Wave 6b future-work item
  still applies; Wave 9 stays on password auth.

---

## 11. Sequencing

| Step | Deliverable |
| ---- | ----------- |
| 9.0 | Migration `0004_accounts_and_memberships`: create `accounts`, `account_memberships`, and `pending_memberships` tables. Drop `users.role`. Add `users.last_active_account_id` (FK ON DELETE SET NULL to `accounts.id`). Add `users.id != '0'` CHECK. Migrate Wave 8b admins/viewers per §2.4. Env superadmin (`MCP_SUPERADMIN_PASSWORD_HASH`, email hardcoded) required at startup. Service-layer last-admin guard (with `SELECT ... FOR UPDATE`). |
| 9.1 | `/signup`, `/accounts`, `/accounts/{id}/members`, `/accounts/{id}/pending`, `/users/me` endpoints. `require_superadmin`, `require_membership_role`, `require_authenticated_user` deps + `admin.superadmin_acting` audit logging on writes. Session stores `{user_id, email, is_superadmin, active_account_id, active_role}` (memberships re-fetched per request). `/session/switch-account`. Active-account selection at login per §3.4. Self-heal of stale `active_account_id` on every request. |
| 9.2 | Migration `0005_catalog_account`: `account_id` (NOT NULL) + `owner_user_id` + `owner_email_snapshot` on skills + skillsets + agents. Server-side stamping from `active_account_id` on create. New `visibility='account'` tier (replaces Wave 8a 2-state). Update `can_read` / agent authorization. |
| 9.3 | Filter GET lists per §4.4 + `?mine` / `?shared` / `?account_id` params. |
| 9.4 | Migration `0006_shares`: `skill_shares` / `skillset_shares` tables + `/shares` CRUD endpoints. Private + private-with-inert-entry semantics. |
| 9.5 | Web UI: account switcher, Accounts page (superadmin), Account detail tabs, Sharing card with private-flip confirmation, My Catalog filters. |
| 9.6 | Membership-removal dialog + reassignment target picker + auto-promotion preview. |
| 9.x | (optional) SMTP invitations; (optional) CLI superadmin-rotate subcommand; (optional) move-account for catalog rows. |

Hard dependencies: 9.0 is the foundation. 9.1 depends on it. 9.2
depends on 9.0 + 9.1. 9.3, 9.4, 9.5, 9.6 can interleave once 9.2
is in. No pre-Wave-9 catalog sweep needed (no production to preserve).
