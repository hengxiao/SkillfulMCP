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
real orgs want redundancy. The only constraint:

**Last-admin guard.** An account must have at least one non-disabled
`account-admin` membership at all times. The service layer refuses
any delete / demote / disable that would reduce the count to zero,
with a 409 and a hint: "promote another member to account-admin
first, or delete the entire account."

No transfer flow is needed — promoting a contributor to account-admin
is a plain role update, since there's no uniqueness to preserve.

### 2.3 Superadmin singleton invariant

Unchanged. Three layers:

1. **Unique partial index**:
   `CREATE UNIQUE INDEX ix_users_one_superadmin ON users(role) WHERE role = 'superadmin'`.
2. Service refuses to delete / disable / demote a superadmin (409).
3. UI hides the destructive controls on the superadmin row.

"Transfer superadmin" is a later wave.

Superadmin is stored on the `users` table directly (not via a
membership), since it has no account. The `role` column there is
either `'superadmin'` or `'user'` (a platform-neutral identity
that's meaningless without a membership).

### 2.4 Transition from Wave 8b

The `0004_accounts_and_memberships` migration:

1. Creates `accounts` and `account_memberships` tables.
2. Drops the 8b `role` column semantics and repurposes the column
   on `users` to `'superadmin' | 'user'`. The `VALID_ROLES` set on
   memberships is `{'account-admin', 'contributor', 'viewer'}`.
3. Picks the oldest existing 8b `admin` (by
   `(created_at NULLS LAST, id ASC)`) and promotes them to
   `superadmin`. Their `users.role` becomes `'superadmin'`.
4. Creates one account per remaining 8b admin named `"{email}'s team"`
   with that admin as its sole `account-admin` membership. Their
   `users.role` becomes `'user'`.
5. Creates one `default` account. Every 8b `viewer` becomes a
   `viewer` membership in `default`. Their `users.role` becomes
   `'user'`.
6. Adds the superadmin partial-unique index + the
   `account-admin count ≥ 1` last-admin guard enforced at the
   service layer.

Env bootstrap on a fresh DB:

- First entry of `MCP_WEBUI_OPERATORS` becomes the `superadmin`.
- A `default` account is created.
- Every subsequent entry becomes a `user` with an `account-admin`
  membership in `default`. Existing ops teams that relied on
  "everyone is an admin" keep that shape — they just all share
  one account now.
- Empty table + empty env still logs the "refuse all logins"
  warning.

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
  (§3.3.4 / §4.1).
- `role` values: `account-admin | contributor | viewer`.
- No superadmin memberships — superadmin is identity-level.

### 3.3 `users` table changes

```python
# users.role simplification
#   old (Wave 8b): 'admin' | 'viewer'
#   new (Wave 9):  'superadmin' | 'user'
#
# The account-scoped roles (account-admin / contributor / viewer)
# live on account_memberships, not here.
role: Mapped[str] = mapped_column(String, nullable=False, default="user")
```

Everything else on `users` (email, password_hash, display_name,
disabled, timestamps, last_login_at) is unchanged from Wave 8b.

Sessions now carry `{user_id, email, user_role, active_account_id,
active_role}`:

- `user_role` is from `users.role` (`'superadmin'` or `'user'`).
- `active_account_id` + `active_role` come from the membership the
  user has currently selected in the UI.
- A superadmin can switch into any account as an implicit
  account-admin for read-everything purposes; this is tracked on
  the session, not by creating a membership row.

### 3.4 User / membership lifecycle

#### 3.4.1 Create a user

- **superadmin**: creates a platform user (no membership). Optional
  `memberships` body field atomically creates N membership rows
  alongside the user, typically one at account-admin for a newly
  created account.
- **account-admin**: cannot create platform users. They can
  **invite** an email to their account (§3.4.2) — if a user with
  that email already exists, a membership is added; if not, a new
  `users` row is created with a server-generated initial password
  (delivered via whatever SMTP flow the deployment has; Wave 9 ships
  with a manual-copy fallback).
- **contributor / viewer**: cannot create users or memberships.

#### 3.4.2 Add / remove membership

```
POST   /admin/accounts/{account_id}/members
  body: {email, role, password?}
  - If a user with `email` exists, add a membership with `role`.
  - Else create a user + add a membership in the same transaction.

DELETE /admin/accounts/{account_id}/members/{user_id}
  - Remove the membership. Protected by the last-admin guard.
  - Does NOT delete the user row — the user may have memberships
    in other accounts.
```

Only superadmin and account-admins of `account_id` can call these.

#### 3.4.3 Role changes

- Changing a membership's role happens on
  `PUT /admin/accounts/{account_id}/members/{user_id}`:
  `{role: "<new>"}`.
- An account-admin can change any other membership's role in their
  account, including promoting a contributor to another
  account-admin. They cannot demote themselves below
  `account-admin` if they'd become the only one left (last-admin
  guard).
- Superadmin can change any membership's role anywhere.
- `users.role` only flips through superadmin (platform-level
  promotion / demotion), covered in §3.4.5.

#### 3.4.4 Delete a user

Two-level semantics:

- **Remove from an account only** → see §3.4.2 (delete a membership).
  The user stays registered; they may have memberships elsewhere.
- **Fully delete the user row** → superadmin-only. Refused if the
  user has any account-admin membership; the operator must remove
  those memberships first (either by re-promoting someone else and
  demoting, or by deleting the whole account). This is the hard-
  delete path; most operator actions use §3.4.2.

When removing a membership, the departing user's owned catalog rows
in **that specific account** need a new owner. The handler accepts an
optional `new_owner_id` (a user with membership in the same
account); default is the oldest account-admin. Auto-promotion rule:

| What the target inherits | Minimum membership role |
| ------------------------ | ------------------------ |
| owned skills / skillsets | `contributor` |

A `viewer` who is picked as the target is promoted to `contributor`
in that account. Promotions to `account-admin` never happen via the
inheritance path — that's always an explicit operator action.

#### 3.4.5 Platform-level role changes

- Only the superadmin can edit `users.role` on another row (i.e.,
  promote a `user` to `superadmin` or vice versa). That's the
  deferred "transfer superadmin" flow (§10).
- The `users.role = 'user'` vs `'superadmin'` flag is invisible to
  account-admins and below; they only see membership roles.

### 3.5 Delete an account

Superadmin-only. The handler:

1. Requires `?confirm_user_count=<N>&confirm_skill_count=<M>` query
   params that match the current membership and catalog counts —
   safety interlock against fat-fingering.
2. Cascades memberships (handled by FK).
3. Reassigns every skill, skillset, and agent owned in the account
   to the **superadmin** (as identity); `account_id` is nulled out
   so those rows become "platform orphans." The
   `/admin/catalog/orphans` page (§6) lets the superadmin re-home
   them or delete them.
4. Deletes the account row.

---

## 4. Catalog: ownership, visibility, sharing

### 4.1 Account-scoped ownership

New columns on `skills`, `skillsets`, and `agents`:

```python
account_id: Mapped[str | None] = mapped_column(
    String, ForeignKey("accounts.id", ondelete="SET NULL"),
    nullable=True, index=True,
)
owner_user_id: Mapped[str | None] = mapped_column(
    String, ForeignKey("users.id", ondelete="SET NULL"),
    nullable=True, index=True,
)
owner_email_snapshot: Mapped[str | None] = mapped_column(
    String, nullable=True,
)
```

- `account_id` is the tenant boundary. Stamped from the session's
  `active_account_id` at create time. Immutable thereafter in Wave 9
  (a future wave can add move-between-accounts).
- `owner_user_id` is the creator. Stamped from the session.
  Nullable so account-delete can leave orphan rows visible to the
  superadmin without cascading the whole catalog.
- `owner_email_snapshot` is a denorm so the UI can show
  `"owned by deleted-user@corp.com"` after a null-out.
- Both FKs `ON DELETE SET NULL` as a safety net. The handler-level
  reassignment in §3.4.4 / §3.5 is the primary policy.

Pre-Wave-9 rows have `NULL account_id` + `NULL owner_user_id` after
the initial migration. The superadmin sweeps these into accounts via
the new `/admin/catalog/assign-account` page as part of rollout.
The `account_id` column ships **nullable in 9.0**; a follow-up wave
flips it to NOT NULL once the sweep is complete.

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

Migration from Wave 8a:

- `visibility='public'` rows stay `public`.
- `visibility='private'` rows become `account` (most natural
  mapping — members of the migrated account will continue to see
  them). Owners who want stricter access can flip them to
  `private` post-migration; a one-time banner reminds them.

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

- `user_memberships` is a `{account_id: role}` dict the session
  caches per-login for cheap lookup.
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

`agents.account_id` is required (nullable only during the 9.0 →
9.x migration window). An agent can only hold grants that are
reachable from its own account:

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
                                           superadmin-only (see §3.5)
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
    skills/skillsets in this account (§3.4.4). Protected by
    last-admin guard.
```

Users (identity-level; simplified from Wave 8b):

```
POST   /admin/users                        superadmin-only
  body: {email, password, display_name, role}
  role: 'user' | 'superadmin' (platform-level)

PUT    /admin/users/{id}                   superadmin: any; self: display_name + password
DELETE /admin/users/{id}                   superadmin-only; see §3.4.4
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
def require_platform_role(*allowed: str):
    """Checks session.user_role ∈ allowed.
       Used only for 'create account' / 'create platform user'."""

def require_membership(path_param: str = "account_id"):
    """Fails 403 unless the caller has a membership in the path
    account_id, OR is superadmin."""

def require_membership_role(path_param: str = "account_id",
                            *allowed_membership_roles):
    """Like require_membership but additionally checks the
    membership's role. Used for mutating account endpoints."""

def require_catalog_access(resource_kind: str,
                           op: str = "read"):
    """Per-resource predicate wrapped as a dep. Fetches the resource,
    resolves the caller's memberships, and applies §4.4 (read) or
    §5.2 (write) rules."""
```

Gating:

- `POST /admin/accounts` → `require_platform_role("superadmin")`.
- `POST /admin/accounts/{id}/members` → `require_membership_role("id", "account-admin")`.
- `POST /skills`, `POST /skillsets` → `require_membership_role("<session.active_account_id>", "contributor", "account-admin")`.
- `PUT`/`DELETE` skills/skillsets → `require_catalog_access(..., op="write")`.
- `POST /token` → `require_membership_role("<agent.account_id>", "account-admin")`.

The `active_account_id` check for resource creation is a session
property, so the dep reads it via `Request.session`; it does **not**
trust a client-supplied `account_id` unless the caller is superadmin.

---

## 9. Tests

- `tests/test_accounts.py` — create / list / delete accounts;
  superadmin-only gating; last-admin guard at the service layer.
- `tests/test_memberships.py` — add / remove / change-role; last-
  admin guard (409 on the wire); cross-account attempts blocked;
  the same user with different roles in two accounts.
- `tests/test_user_hierarchy.py` — superadmin singleton invariant
  (DB-level uniqueness + service refusal); platform-user vs
  membership-role distinction.
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
- **Transfer-superadmin** flow. Wave 9 treats superadmin as set-
  once-at-bootstrap. A future wave adds a one-time-token handshake
  for promotion + self-demotion.
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
| 9.0 | Migration `0004_accounts_and_memberships`: create `accounts` + `account_memberships` tables, simplify `users.role` to `{superadmin, user}`, migrate Wave 8b admins/viewers per §2.4. Singleton-superadmin partial index. `VALID_MEMBERSHIP_ROLES` enum. Service-layer last-admin guard. |
| 9.1 | `/admin/accounts/*` + `/admin/accounts/{id}/members/*` endpoints. `require_platform_role` + `require_membership_role` deps. Session stores `{user_role, active_account_id, active_role, memberships}`. `/session/switch-account`. |
| 9.2 | Migration `0005_catalog_account`: `account_id` + `owner_user_id` + `owner_email_snapshot` on skills + skillsets + agents. Server-side stamping. New `visibility='account'` tier (replaces Wave 8a 2-state). Update `can_read` / authorization. |
| 9.3 | Filter GET lists per §4.4 + `?mine` / `?shared` / `?account_id` params. |
| 9.4 | Migration `0006_shares`: `skill_shares` / `skillset_shares` tables + `/shares` CRUD endpoints. Private + private-with-inert-entry semantics. |
| 9.5 | Web UI: account switcher, Accounts page (superadmin), Account detail tabs, Sharing card, My Catalog filters. |
| 9.6 | Membership-removal dialog + reassignment target picker + auto-promotion preview. |
| 9.7 | `/admin/catalog/assign-account` superadmin sweep page for pre-Wave-9 orphan rows; NOT-NULL follow-up migration after the sweep completes. |
| 9.x | (optional) SMTP invitations; (optional) transfer-superadmin flow; (optional) move-account for catalog rows. |

Hard dependencies: 9.0 is the foundation. 9.1 depends on it. 9.2
depends on 9.0 + 9.1. 9.3, 9.4, 9.5, 9.6 can interleave once 9.2
is in. 9.7 is cleanup — not required for the feature to be usable.
