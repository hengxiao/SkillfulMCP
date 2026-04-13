# User Management — Accounts, Roles, Ownership, and Allow Lists

Status: **proposed** (Wave 9). Builds on:

- [Wave 8b](visibility-and-accounts.md#wave-8b-users--roles--shipped) — DB-backed users with `admin` / `viewer` roles.
- [Wave 8a](visibility-and-accounts.md#1-visibility-model) — `public` / `private` flag on skills + skillsets.

Wave 9 turns the flat operator pool into a **tenant model**: every user
belongs to an **Account**, and the `account-admin` of that account is
the only non-superadmin who can create or delete users in it. Catalog
ownership layers on top — users own skills and skillsets, accounts are
the sharing boundary, and an email-based allow list covers the cases
where sharing has to cross account boundaries.

---

## 1. Motivation

Wave 8b's flat `admin` / `viewer` pool doesn't scale beyond a single
ops team:

- Every admin sees every user; no isolation between teams or
  customers sharing the deployment.
- Every viewer sees every skill; no way to say "only the billing team
  sees the billing skills."
- Sharing outside the operator pool requires flipping a skill to
  `public`, which exposes it to *all* authenticated agents.

Wave 9 introduces **accounts** as the primary isolation boundary and
**email-based allow lists** as the escape hatch for targeted
cross-boundary sharing.

---

## 2. Accounts and roles

### 2.1 Shape

```
                superadmin  (platform-level, no account, singleton)
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
    Account A   Account B   Account C        ← tenants
        │           │           │
  account-admin  account-admin  account-admin
        │           │           │
   ┌────┼────┐      │      ┌────┼────┐
   ▼    ▼    ▼      ▼      ▼    ▼    ▼
 cont viewer cont  cont  cont viewer viewer
```

An **account** is a container for users, skills, and skillsets. Users
can only see / manage catalog content within their own account, except
via the public flag or an email allow list.

| Role | Belongs to | Can create | Can delete | Catalog scope |
| ---- | ---------- | ---------- | ---------- | -------------- |
| `superadmin` | no account (platform) | accounts, account-admins | any account or user (except self) | Sees everything across all accounts. |
| `account-admin` | exactly one account | `contributor` / `viewer` **in their account** | users in their account | Sees + edits everything in their account. |
| `contributor` | exactly one account | — | — | CRUD on resources they own; read on account-internal + shared-with-them + public. |
| `viewer` | exactly one account | — | — | Read-only. Sees account-internal + shared-with-them + public. |

Key rules:

- **Exactly one `superadmin` exists.** Unique, cannot be deleted,
  cannot be disabled, cannot be placed in an account.
- **Every non-superadmin user has a non-NULL `account_id`.** Enforced
  by a CHECK constraint: `(account_id IS NULL) = (role = 'superadmin')`.
- **Every account has exactly one `account-admin`** at any given
  time. Enforced by a unique partial index on
  `(account_id) WHERE role = 'account-admin'`.
- **Only `superadmin` can create accounts** (and therefore mint
  `account-admin` roles). Account-admins can only create
  `contributor` / `viewer` **in their own account**.
- **Cross-account management is superadmin-only.** An account-admin
  cannot see, edit, or delete users in another account.

### 2.2 Transition from Wave 8b

Wave 8b deployments have N existing `admin`s + M existing `viewer`s,
all in one flat pool. The `0004_accounts` migration:

1. Creates the `accounts` table.
2. Inserts one seed account `default` with
   `name = "Default"`.
3. Picks the oldest `admin` (by `(created_at NULLS LAST, id ASC)`) and
   promotes them to `superadmin` — no account.
4. Every *other* existing `admin` becomes `account-admin` of a
   fresh-per-admin account named `"{email}'s team"`. That keeps each
   pre-Wave-9 admin's implicit "everyone sees my stuff" behavior in
   their own tenant rather than silently merging them.
5. Every existing `viewer` is moved into the `default` account as a
   `viewer`. Admins can re-home them later.
6. Adds the unique-account-admin partial index and the
   `account_id IS NULL iff superadmin` CHECK.

Env-bootstrap change: on a fresh DB, the first entry of
`MCP_WEBUI_OPERATORS` becomes `superadmin` (no account); every
subsequent entry lands as an `account-admin` of a freshly created
account `"{email}'s team"`. Empty table + empty env still logs the
"refuse all logins" warning.

### 2.3 Superadmin singleton invariant

Triple-guarded:

1. **Unique partial index**:
   `CREATE UNIQUE INDEX ix_users_one_superadmin ON users(role) WHERE role = 'superadmin'`.
2. **Service layer** refuses to delete, disable, or demote a
   superadmin; 409 with a helpful message.
3. **UI** hides the destructive controls on the superadmin row and
   renders a lock badge.

"Transfer superadmin" (promote someone else, then demote self) is
explicitly a later wave — see §10.

### 2.4 Account-admin singleton invariant

One account-admin per account, at all times. Same three-layer guard:

1. Unique partial index on `(account_id) WHERE role = 'account-admin'`.
2. Service refuses to delete / demote the account-admin unless
   another user in the same account is simultaneously being promoted
   (the **transfer** flow, see §3.3.3).
3. UI forces the transfer flow rather than offering a direct delete.

---

## 3. Data model

### 3.1 `accounts` table

```python
class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # uuid4 hex
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=..., onupdate=...,
    )
```

### 3.2 `users` table — add `account_id`

```python
account_id: Mapped[str | None] = mapped_column(
    String,
    ForeignKey("accounts.id", ondelete="RESTRICT"),
    nullable=True,  # NULL iff role == 'superadmin', enforced by CHECK
    index=True,
)
```

`ON DELETE RESTRICT` — an account can't be dropped while users still
reference it. The account-delete handler (§3.3.4) reassigns or
removes users first.

The Wave 8b-era `role` column stays; allowed values change from
`{admin, viewer}` to `{superadmin, account-admin, contributor, viewer}`.
`VALID_ROLES` in [mcp_server/users.py](../mcp_server/users.py) expands
in lockstep.

### 3.3 User lifecycle operations

#### 3.3.1 Create

- `superadmin` can create any role with any `account_id`.
  Creating a new account creates its account-admin in the same
  transaction — you can't have an account with no admin.
- `account-admin` can create only `contributor` / `viewer` and only
  with `account_id = their own`. Attempts to specify another account
  return 403.
- `contributor` / `viewer` cannot create users.

#### 3.3.2 Edit (including role change)

- An account-admin can edit `display_name`, `disabled`, and role
  between `contributor ↔ viewer` for users in their account. They
  cannot edit themselves (anti-lockout; role change goes through the
  transfer flow).
- Superadmin can edit anyone, including promoting a contributor to
  account-admin — this requires either the transfer flow (§3.3.3) or
  the moving-to-another-account flow (§3.3.5).

#### 3.3.3 Delete — standard path

For `contributor` / `viewer`:

```python
def delete_user(db, user_id, *, new_owner_id: str | None = None):
    victim = db.get(User, user_id)
    if victim is None:
        return False
    if victim.role == "superadmin":
        raise ValueError("Cannot delete the superadmin")
    if victim.role == "account-admin":
        raise ValueError("Transfer account-admin to another user first (see §3.3.3 transfer flow)")

    # Reassign the victim's owned catalog rows. Default target is the
    # account-admin of their account; operator can override with any
    # other user in the same account (contributor / account-admin
    # only — viewers can't own).
    target_id = new_owner_id or _account_admin_of(db, victim.account_id).id
    _reassign_owned_rows(db, victim, target_id)

    # Auto-promote the target if it's a viewer (viewers can't own;
    # inheriting rows requires at least contributor).
    _maybe_promote(db, target_id, victim)

    db.delete(victim)
    db.commit()
```

For `account-admin` — **not a direct delete**. They have to transfer
their role first, then leave the role as a contributor / viewer, then
be deleted via the standard path:

```
[transfer] account-admin → promote contributor X to account-admin,
                           demote self to contributor
           (superadmin can also initiate this)
[then]     standard delete of the now-contributor
```

The transfer is a single UI action ("Transfer account-admin to…")
but two atomic role changes in the service layer, wrapped in one
transaction. Fails if the target is not a contributor in the same
account.

For `superadmin` — forbidden. Always. See §2.3.

#### 3.3.4 Delete — account

Only superadmin can delete an account. The delete handler:

1. Block if the account has any contributor or viewer users that
   the caller hasn't explicitly acknowledged (the UI shows the count
   and a confirmation field).
2. Hard-delete all shares pointing at catalog rows in this account.
3. Reassign every skill + skillset owned in this account to the
   **superadmin** (so the rows don't vanish, though they become
   orphaned-from-any-account-view).
4. Delete the users in the account.
5. Delete the account row.

Alternative considered: cascade-delete skills when the account is
deleted. Rejected — catalog data is too valuable to tie to an
account's lifecycle. The superadmin can then choose to delete or
migrate the reassigned rows explicitly.

#### 3.3.5 Moving a user between accounts

Superadmin-only. Changes `account_id`; their owned catalog rows do
**not** follow them (those stay with the original account). Their
personal shares (allow-list entries granting access to them) are
unaffected because allow lists are email-keyed (§4).

### 3.4 Auto-promotion on inheritance

Same rule as Wave 8b had internally, now scoped to a single account:

| Victim's role | What the target inherits | Minimum target role |
| ------------- | ------------------------ | -------------------- |
| `account-admin` | (forbidden — must transfer first) | N/A |
| `contributor` | owned skills + skillsets | `contributor` |
| `viewer` | nothing (viewers can't own) | no change |

Promotion happens only within the same account, triggered by an
explicit `new_owner_id` in the delete call. The service refuses to
promote across accounts.

Promotion events are logged as `user.promoted` with
`reason=inheritance`.

---

## 4. Ownership, visibility, and sharing

### 4.1 Skill / skillset ownership

Two new columns on `skills` and `skillsets`:

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

- `account_id` is **required** for every catalog row. Set from the
  creator's `account_id` on insert, immutable thereafter (moving a
  skill between accounts is a superadmin explicit-transfer op in a
  future wave).
- `owner_user_id` is the authoritative owner pointer; `owner_email_snapshot`
  is a denorm for the UI to display "owned by deleted-user@corp.com"
  if the owner is ever nulled out (last-resort path; normal deletes
  reassign owner via §3.3.3).

Pre-Wave-9 rows have `NULL` account + `NULL` owner after the
migration. A superadmin assigns them to accounts on the new
`/admin/catalog/assign-account` page (9.1).

### 4.2 Visibility tiers

Wave 8a's two-state `public` / `private` expands to three:

| `visibility` value | Who can read (in addition to the owner) |
| ------------------ | ---------------------------------------- |
| `public` | Anyone — any authenticated agent, anonymous UI visitors |
| `account` | **(new default)** all users in the owning `account_id`; plus anyone on the allow list |
| `private` | Only the owner; plus anyone on the allow list |

Migration from Wave 8a:

- `visibility='public'` rows stay `public`.
- `visibility='private'` rows become `account` (most natural
  mapping — existing members of the implicit "default admin pool"
  will continue to see them as members of the migrated account).
- Truly-private items have to be re-flipped by their owner after
  migration. A one-time banner on the skill-list page warns about
  this.

### 4.3 Allow list (email-based, cross-account)

Same data model as the previous hierarchical design — email-keyed
shares with no FK to users so not-yet-registered addresses work:

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
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    __table_args__ = (UniqueConstraint("skill_id", "email",
        name="uq_skill_share_skill_email"),)
```

`SkillsetShare` is parallel.

Accounts don't constrain the allow list: a share entry's email can
belong to any account (including none). When the grantee logs in,
the share resolves by matching their session email; their account
membership doesn't affect whether the share is honored.

### 4.4 Combined visibility check

```python
def can_read(resource, user) -> bool:
    if resource.visibility == "public":
        return True
    if user is None:
        return False
    if user.role == "superadmin":
        return True
    if resource.visibility == "account" and user.account_id == resource.account_id:
        return True
    if user.role == "account-admin" and user.account_id == resource.account_id:
        return True  # account-admin sees all of their account
    if resource.owner_user_id == user.id:
        return True
    if share_exists(resource, user.email):
        return True
    return False
```

Notes:

- `account-admin` implicitly sees all catalog content in their
  account, regardless of per-resource visibility. Mirrors the
  "account owner sees everything in their tenant" UX from GitHub,
  Google Workspace, etc.
- A share entry *overrides* account isolation: the whole point of
  the allow list is cross-boundary sharing.
- Private resources are invisible to everyone except the owner, the
  account-admin, and the allow list. Even other members of the same
  account can't see them.

### 4.5 Agent JWT scope — unchanged

Allow lists affect **operator UI access** only. The authorization
layer for agent tokens still uses `public` + explicit agent grants
exactly as before; account membership and allow-list entries do not
implicitly widen a JWT.

Rationale (unchanged from earlier drafts): operators are humans,
agents are service identities; the two identity planes should not
bleed. Keeping them separate means a compromised operator session
can't silently expand an agent's reach.

---

## 5. API changes

### 5.1 New endpoints

Accounts:

```
POST   /admin/accounts                     body: {name, admin: {email, password, display_name}}
  superadmin-only. Creates the account + its account-admin atomically.

GET    /admin/accounts                     superadmin-only
GET    /admin/accounts/{id}                superadmin; account-admin for own account
DELETE /admin/accounts/{id}?confirm=<n>    superadmin-only (see §3.3.4)
```

Users (extends Wave 8b `/admin/users/*`):

```
POST   /admin/users
  body: {email, password, role, display_name, account_id?}
  - account-admin: account_id defaults to caller's account and
    cannot be specified otherwise; role restricted to
    contributor / viewer.
  - superadmin: account_id required for non-superadmin roles;
    role restricted to contributor / viewer / account-admin.

DELETE /admin/users/{id}?new_owner_id=<uid>
  - Default new_owner_id = account-admin of victim's account.
  - new_owner_id must be in the victim's account (otherwise 400).
  - Role 'account-admin' cannot be deleted — use transfer flow.

POST   /admin/users/{id}/transfer-admin     body: {new_admin_id}
  - Atomically demote {id} (currently account-admin) to contributor
    and promote {new_admin_id} (currently contributor in same
    account) to account-admin.
  - Runs in a single transaction; the unique-admin partial index
    protects against races.

POST   /admin/users/{id}/move-account       body: {new_account_id}
  - superadmin-only; see §3.3.5.
```

Sharing — same as the earlier draft:

```
POST   /skills/{id}/shares                  body: {email}
GET    /skills/{id}/shares
DELETE /skills/{id}/shares/{share_id}

POST   /skillsets/{id}/shares               body: {email}
GET    /skillsets/{id}/shares
DELETE /skillsets/{id}/shares/{share_id}
```

Authorization:

- Managing shares = owner OR account-admin of the resource's account
  OR superadmin.
- Creating a skill = contributor / account-admin / superadmin
  (viewers can't create).

### 5.2 Modified endpoints

- `POST /skills`, `POST /skillsets`: stamp `account_id` + `owner_user_id`
  from the session. Admin-key-only callers (CLI) must supply
  `account_id` explicitly; attempts without it 400.
- `GET /skills`, `GET /skillsets`: filter per §4.4. Query params:
  - `?mine=1` → `owner_user_id = session.user_id`
  - `?shared=1` → resources the caller sees via the allow list only
    (useful for "who shared with me" screens)
  - `?account_id=<id>` → account-admins use this to narrow inside
    their account; superadmin uses it to pick one.
- `PUT`, `DELETE` on skills / skillsets: owner OR account-admin of
  `resource.account_id` OR superadmin. 403 otherwise.

### 5.3 Validation

- Email on `/shares`: trimmed + lowercased. Regex
  `^[^@]+@[^@]+\.[^@]+$`; deeper deliverability out of scope. 400
  on syntax failure.
- Cannot share with your own email (no-op + 400).
- Cannot share with a `public` resource (400 with hint).
- Cannot set `visibility=account` on a resource in no-account
  (orphaned by a pre-Wave-9 bulk migration) — operator must assign
  an account first.

---

## 6. Web UI changes

### 6.1 Accounts page (superadmin only)

New top-level nav item when logged in as superadmin. Table of
accounts with columns: name, admin (email of account-admin), user
count, skill count, created. Row actions: view, delete.

Create-account form: name, initial account-admin email + password
+ display_name. One button, one transaction.

### 6.2 Users page — now account-scoped

- Superadmin: dropdown at top to switch accounts, or "All" for the
  merged view. Lists users in the chosen scope.
- Account-admin: always scoped to their own account. No switcher.
- Contributors / viewers: no access (`/users` is admin-only).

Columns: email, display name, role badge, owns (count), last
login, status. Row actions: edit, delete. The account-admin row
has a lock badge and only a "Transfer admin" button (no direct
delete).

### 6.3 Account-admin transfer dialog

```
┌─ Transfer account-admin role ────────────────────────────┐
│ Current: alice@corp.com                                  │
│                                                           │
│ Promote to account-admin: [ dropdown of contributors ]   │
│                                                           │
│ alice will be demoted to contributor in the same         │
│ transaction. She keeps ownership of her skills and       │
│ skillsets.                                                │
│                                                           │
│  [ Cancel ]                        [ Transfer admin ]    │
└───────────────────────────────────────────────────────────┘
```

### 6.4 Delete-user dialog

```
┌─ Delete user: bob@corp.com ──────────────────────────────┐
│ Role: contributor (account: Corp Ops)                    │
│ Owns 3 skills and 1 skillset.                            │
│                                                           │
│ Reassign ownership to:                                    │
│   ● account-admin (default) — alice@corp.com            │
│   ○ Another account member:  [ dropdown ]                │
│                                                           │
│ ⚠ If reassigning to a viewer, they'll be promoted to     │
│   contributor so they can hold owned resources.          │
│                                                           │
│  [ Cancel ]                              [ Delete user ] │
└───────────────────────────────────────────────────────────┘
```

Dropdown contains contributors + the account-admin (viewers are
allowed but show an explicit promotion warning). Cross-account
targets are not listed — that would violate account isolation.

### 6.5 Skill / skillset detail — Sharing card

```
┌─ Sharing ────────────────────────────────────────────────┐
│ Visibility:                                              │
│   ○ Private (owner + allow list)                         │
│   ● Account (members of "Corp Ops" + allow list)         │
│   ○ Public (any authenticated agent)                     │
│                                                           │
│ Allow list (account / private only):                     │
│   alice@customer.com    [registered]    [×]             │
│   bob@customer.com      [pending]       [×]             │
│   + Add email                                            │
└───────────────────────────────────────────────────────────┘
```

### 6.6 Account page (`/account`)

Existing self-service password change, plus new "My organization"
card showing the account name, the account-admin's email, and the
user's role within the account.

### 6.7 Filter pills on `/skills`

`All visible` / `Mine` / `Shared with me` / `Public` / (for
account-admins and superadmin) `Account all`.

---

## 7. Why allow lists don't bleed into JWT scope

Unchanged from the earlier draft. Agents are first-class entities
with admin-configured grants; a human operator's UI access does not
translate to agent capability. Account membership also doesn't
change this — agents are registered per-agent-record and carry the
skills / skillsets / scope granted to them, not the account of
their creator.

This keeps a compromised operator session from silently widening
an agent's reach.

---

## 8. Role-check implementation

Three dependencies, plus a couple of predicate helpers:

```python
def require_role(*allowed: str): ...
# e.g. require_role("contributor", "account-admin", "superadmin")

def require_account_scope(path_param: str = "account_id"):
    """Fails 403 unless:
       - caller is superadmin, OR
       - caller is account-admin/contributor/viewer AND
         caller.account_id == <path account_id>.
    Used on /admin/accounts/{id}/... routes."""

def require_account_management(path_param: str = "user_id"):
    """Fails 403 unless caller is superadmin, or caller is
    account-admin of the target user's account. Used on
    /admin/users/{user_id}/... mutations."""
```

Ownership + sharing checks remain a per-handler predicate (they need
the resource fetched first to resolve visibility tier + owner).

---

## 9. Tests

New / changed suites:

- `tests/test_accounts.py` — CRUD on accounts (superadmin only);
  singleton-admin invariant; delete-with-confirmed-count.
- `tests/test_user_hierarchy.py` — role assignment rules: account-
  admin can't create admins; contributors can't create users;
  superadmin can do anything.
- `tests/test_user_delete.py` — default reassign to account-admin;
  operator-picked target; auto-promotion viewer → contributor;
  cross-account targets rejected; can't delete account-admin
  directly (must transfer).
- `tests/test_admin_transfer.py` — atomic demote/promote; race
  safety via the unique partial index; same-account constraint.
- `tests/test_visibility.py` — extend existing Wave 8a tests with
  `visibility=account` (account members see it, non-members don't,
  allow-list emails do, superadmin does).
- `tests/test_shares.py` — unchanged email-keyed model; now
  exercised across accounts.
- `tests/test_api_accounts.py` / `test_webui_accounts.py` —
  integration for the new admin pages.

Coverage gate stays at 85%. New code ≈ 900 LoC; tests ≈ 650 LoC.

---

## 10. Out of scope

- **Group-based shares** (`@customer.com` wildcards, LDAP groups).
- **Per-version shares** — shares are skill-id-keyed and apply to
  all versions.
- **Time-boxed shares** (`expires_at`). One column, one read filter,
  easy to add later.
- **Audit log** of share grants / revocations / account operations.
  Log lines are emitted today; a queryable audit table is the
  follow-up tracked in
  [visibility-and-accounts.md §out-of-scope](visibility-and-accounts.md#out-of-scope-call-out-for-clarity).
- **SMTP invitations** when a share is added. Requires SMTP config;
  ship separately (Wave 9.1).
- **Multi-account membership** — a user is in exactly one account.
  A later wave can introduce a join table if contractors-working-
  for-multiple-orgs becomes a real use case.
- **Transfer-superadmin** flow (promote an existing user to
  superadmin, demote self). Treated as set-once-at-bootstrap in
  Wave 9. Worth doing when the role's owner changes — scope that
  wave to include a one-time-token confirmation step so it's not a
  single-click hand-off.
- **Demotion-on-delete** (auto-demote an account-admin to contributor
  if their account empties out). Deliberately skipped: automatic
  demotion surprises the operator and can drop capabilities they
  still rely on. Admins demote via the explicit edit flow.
- **Moving catalog rows between accounts.** Initial Wave 9 keeps
  `account_id` on skills / skillsets immutable after creation. A
  future wave can add `POST /admin/skills/{id}/move-account` if
  cross-account content migration becomes a real need.

---

## 11. Sequencing

| Step | Deliverable |
| ---- | ----------- |
| 9.0 | Migration `0004_accounts`: add `accounts` table, add `account_id` to `users`, migrate Wave 8b admins + viewers per §2.2. New roles in `VALID_ROLES`. Partial indices for singleton-superadmin and one-admin-per-account. |
| 9.1 | `/admin/accounts/*` CRUD endpoints + `account_id` on `POST /admin/users` + `require_account_management` dep. Web UI `/accounts` page for superadmin. |
| 9.2 | Migration `0005_catalog_account`: add `account_id` + `owner_user_id` + `owner_email_snapshot` to skills / skillsets; stamp them server-side on create. `account` visibility tier (replaces the Wave 8a two-state model). |
| 9.3 | Filter `GET /skills`, `GET /skillsets` per §4.4 + `?mine` / `?shared` / `?account_id` query params. |
| 9.4 | Migration `0006_shares`: `skill_shares` / `skillset_shares` tables + `/shares` CRUD endpoints. |
| 9.5 | Web UI: Sharing card, account-scoped Users page, Account page, My Catalog filters. |
| 9.6 | Delete-user modal with reassignment-target picker + promotion preview; account-admin transfer dialog. |
| 9.x | (optional) SMTP invitations; (optional) transfer-superadmin flow; (optional) move-account for catalog rows. |

Hard dependencies: 9.0 is the foundation — nothing else lands
without it. 9.2 depends on 9.0 for `account_id`. 9.4 depends on
9.2 for the sharing UI to render meaningful visibility states.
9.3, 9.5, 9.6 can ship in any order once 9.2 + 9.4 are in.
