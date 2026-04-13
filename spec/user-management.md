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

Superadmin is **not a `users` row**. It's a fixed identity defined
by two environment variables:

- `MCP_SUPERADMIN_EMAIL` — plain email string used at login.
- `MCP_SUPERADMIN_PASSWORD_HASH` — bcrypt hash, matched via the same
  `verify_password` helper as regular users.

The superadmin's user id is the literal string `"0"` — reserved and
never issued to a real user (uuid4 hex never produces `"0"`, and
the `users` table has a CHECK constraint refusing that value).

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

At login, `authenticate_via_server` checks the incoming email
against `MCP_SUPERADMIN_EMAIL` first; on match, it verifies against
`MCP_SUPERADMIN_PASSWORD_HASH` and returns a superadmin-flagged
Operator without touching the DB. Regular users fall through to the
existing DB lookup.

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
3. Create one `default` account.
4. Every existing 8b `viewer` becomes a `viewer` membership in
   `default`.
5. Every existing 8b `admin` becomes an `account-admin` membership
   in its own fresh account named `"{email}'s team"`. Each admin
   gets their own tenant; admins who previously shared the flat
   pool don't silently merge.
6. Add the `account_memberships` indices (composite PK on
   `(user_id, account_id)` plus a secondary on `account_id` alone
   for membership-list lookups).
7. Add the `users.id != '0'` CHECK so no DB row can accidentally
   collide with the hardcoded superadmin id.

The old `MCP_WEBUI_OPERATORS` env var stays supported for
bootstrapping the `users` table on an empty DB (same semantics as
Wave 8b). Every entry becomes a regular user with an `account-admin`
membership in `default`. Empty table + empty env still logs the
"refuse all logins" warning, same as today.

Superadmin identity comes from the new env pair `MCP_SUPERADMIN_EMAIL`
+ `MCP_SUPERADMIN_PASSWORD_HASH` (see §2.3). These are **required**
in Wave 9 deployments — missing either one refuses startup with a
clear error message.

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
`POST /session/switch-account` and on logout.

### 3.5 User / membership lifecycle

#### 3.5.1 Create a user

- **superadmin**: the only caller who can mint a user row that
  isn't attached to an account. In practice this is rare — most
  user creation happens via the invite flow below, which atomically
  creates the user + their first membership.
- **account-admin**: invites an email to their account via
  `POST /admin/accounts/{account_id}/members` (§3.5.2). If a user
  with that email already exists, a membership is added; if not, a
  new `users` row is created with a server-supplied initial
  password (copy-link fallback; SMTP delivery is Wave 9.1).
- **contributor / viewer**: cannot create users or memberships.

(There is no "platform user" vs "regular user" distinction anymore
— every DB row is just a user. The only platform-level identity is
the env-hardcoded superadmin in §2.3.)

#### 3.5.2 Add / remove membership

```
POST   /admin/accounts/{account_id}/members
  body: {email, role, password?}
  - If a user with `email` exists, add a membership with `role`.
  - Else create a user + add a membership in the same transaction.

DELETE /admin/accounts/{account_id}/members/{user_id}?new_owner_id=<uid>
  - Remove the membership (last-admin guarded, §2.2 pattern).
  - Optional new_owner_id reassigns the departing user's owned
    catalog rows in this account (§3.5.4).
  - Does NOT delete the user row — the user may have memberships
    in other accounts.
```

Only superadmin and account-admins of `account_id` can call these.

#### 3.5.3 Role changes

- Changing a membership's role happens on
  `PUT /admin/accounts/{account_id}/members/{user_id}` with body
  `{role: "<new>"}`.
- Any account-admin can change any other membership's role in their
  account, including promoting a contributor to another
  account-admin (§2.2 flagged this as intentional).
- An account-admin cannot demote themselves from `account-admin`
  if they'd become the last admin — the last-admin guard rejects
  the update.
- Superadmin can change any membership's role in any account.

#### 3.5.4 Delete a user row

Superadmin-only hard delete. Refused if the user holds any
`account-admin` membership; the operator must remove those
memberships first (promote another member, then demote /
remove the target). Normal "remove from account" is §3.5.2.

When removing a membership (§3.5.2), the departing user's owned
catalog rows in **that specific account** need a new owner. The
handler accepts an optional `new_owner_id` (a user with membership
in the same account); default is the oldest account-admin of the
account. Auto-promotion rule:

| What the target inherits | Minimum membership role |
| ------------------------ | ------------------------ |
| owned skills / skillsets | `contributor` |

A `viewer` who is picked as the target is promoted to `contributor`
in that account. Promotions to `account-admin` never happen via the
inheritance path — that's always an explicit operator action.

### 3.6 Delete an account

Superadmin-only. Because `account_id` is **NOT NULL** on skills,
skillsets, and agents (§4.1), the handler cannot orphan catalog
rows — it must either cascade-delete them or require the superadmin
to have moved them first.

Wave 9 chooses cascade-delete behind a double interlock:

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
  `ON DELETE RESTRICT` means an account cannot be dropped while
  catalog rows still reference it; the account-delete handler
  (§3.6) either cascades everything or refuses.
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

Accounts:

```
POST   /admin/accounts
  body: {name, initial_admin: {email, password?, display_name}}
  superadmin-only. Creates account + one account-admin membership
  in the same transaction. If `email` matches an existing user,
  just attach a membership; else create the user (initial password
  required in that branch).

GET    /admin/accounts                     superadmin: all; user: accounts they're a member of
GET    /admin/accounts/{id}                superadmin; membership-holders for their own account
DELETE /admin/accounts/{id}?confirm_user_count=N&confirm_skill_count=M
                                           superadmin-only (see §3.6)
```

Memberships:

```
POST   /admin/accounts/{account_id}/members
  body: {email, role, password?}

GET    /admin/accounts/{account_id}/members

PUT    /admin/accounts/{account_id}/members/{user_id}
  body: {role}

DELETE /admin/accounts/{account_id}/members/{user_id}?new_owner_id=<uid>
  - Runs the catalog-reassignment step if the departing user owns
    skills/skillsets in this account (§3.5.4). Protected by
    last-admin guard.
```

Users (identity-level; simplified from Wave 8b):

```
POST   /admin/users                        superadmin-only
  body: {email, password, display_name, role}
  role: 'user' | 'superadmin' (platform-level)

PUT    /admin/users/{id}                   superadmin: any; self: display_name + password
DELETE /admin/users/{id}                   superadmin-only; see §3.5.4
```

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

New top-level nav item, superadmin-only. Lists accounts with
columns: name, admin count, user count, skill count, created.
Row actions: open (→ /admin/accounts/{id}), delete (with the
confirmation-count interlock).

Create-account form: name + initial admin (email + password +
display_name). One atomic call to `POST /admin/accounts`.

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

### 6.6 Landing page (already shipped, clarification)

Public catalog landing stays as shipped in commit [9b75439]. The
`account` visibility tier is hidden from anonymous visitors; the
public list stays `public` only.

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
       account_id=<path>  method=<METHOD>  path=<URL>
    so the forensics trail captures which account each superadmin
    action touched."""

def require_catalog_access(resource_kind: str,
                           op: str = "read"):
    """Per-resource predicate wrapped as a dep. Fetches the resource,
    resolves the caller's memberships, and applies §4.4 (read) or
    §5.2 (write) rules. Superadmin always passes; write access emits
    the same audit log line as above."""
```

Gating:

- `POST /admin/accounts`, `DELETE /admin/accounts/{id}` → `require_superadmin()`.
- `POST /admin/accounts/{id}/members`, `PUT .../members/{user_id}`, `DELETE .../members/{user_id}` → `require_membership_role("id", "account-admin")`.
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
| 9.0 | Migration `0004_accounts_and_memberships`: create `accounts` + `account_memberships` tables, drop `users.role`, add `users.last_active_account_id` + `users.id != '0'` CHECK. Migrate Wave 8b admins/viewers per §2.4. Env superadmin (`MCP_SUPERADMIN_EMAIL` + `MCP_SUPERADMIN_PASSWORD_HASH`) required at startup. Service-layer last-admin guard (with `SELECT ... FOR UPDATE`). |
| 9.1 | `/admin/accounts/*` + `/admin/accounts/{id}/members/*` endpoints. `require_superadmin` + `require_membership_role` deps + `admin.superadmin_acting` audit logging on writes. Session stores `{user_id, email, is_superadmin, active_account_id, active_role}` (memberships re-fetched per request). `/session/switch-account`. Active-account selection at login per §3.4. |
| 9.2 | Migration `0005_catalog_account`: `account_id` (NOT NULL) + `owner_user_id` + `owner_email_snapshot` on skills + skillsets + agents. Server-side stamping from `active_account_id` on create. New `visibility='account'` tier (replaces Wave 8a 2-state). Update `can_read` / agent authorization. |
| 9.3 | Filter GET lists per §4.4 + `?mine` / `?shared` / `?account_id` params. |
| 9.4 | Migration `0006_shares`: `skill_shares` / `skillset_shares` tables + `/shares` CRUD endpoints. Private + private-with-inert-entry semantics. |
| 9.5 | Web UI: account switcher, Accounts page (superadmin), Account detail tabs, Sharing card with private-flip confirmation, My Catalog filters. |
| 9.6 | Membership-removal dialog + reassignment target picker + auto-promotion preview. |
| 9.x | (optional) SMTP invitations; (optional) CLI superadmin-rotate subcommand; (optional) move-account for catalog rows. |

Hard dependencies: 9.0 is the foundation. 9.1 depends on it. 9.2
depends on 9.0 + 9.1. 9.3, 9.4, 9.5, 9.6 can interleave once 9.2
is in. No pre-Wave-9 catalog sweep needed (no production to preserve).
