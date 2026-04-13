# User Management — Ownership, Roles, and Allow Lists

Status: **proposed** (Wave 9). Builds on:

- [Wave 8b](visibility-and-accounts.md#wave-8b-users--roles--shipped) — DB-backed users with `admin` / `viewer` roles.
- [Wave 8a](visibility-and-accounts.md#1-visibility-model) — `public` / `private` flag on skills + skillsets.

This spec adds a third axis to the catalog: **ownership**, plus an
**email-based allow list** for private items so non-public skills can
still be shared with specific people without granting them full operator
access.

---

## 1. Motivation

Today the operator pool is flat: every admin can edit every skill, every
viewer can see every skill, and sharing outside the operator set requires
flipping the item to `public` (which exposes it to *all* authenticated
agents).

Missing in between:

- **"My skills"** — a normal contributor should CRUD the skills they
  created without being able to touch someone else's.
- **Targeted sharing** — "let alice@customer.com and bob@customer.com
  view this skill without giving them operator accounts."

These map to the familiar GitHub / Google Docs model:
owner + per-resource allow list + public toggle.

---

## 2. Roles and hierarchy

Wave 8b has a flat role set (`admin` / `viewer`). Wave 9 replaces it
with a **four-level tree** rooted at a singleton `superadmin`:

```
                superadmin  (root, cannot be deleted)
                /        \
           admin         admin
          /    \        /    \
   contributor viewer  contributor viewer
```

| Role | Can create | Can delete | Catalog capabilities |
| ---- | ---------- | ---------- | -------------------- |
| `superadmin` | `admin` | anyone except self | Everything in the catalog. Impersonate, reassign, sweep orphans. |
| `admin` | `contributor`, `viewer` **under themselves** | their own descendants | See / edit everything in the catalog. Manage sharing, reassign owners within their subtree. |
| `contributor` | — | — | CRUD on resources they **own**; read on public + shared-with-them. |
| `viewer` | — | — | Read-only. Browse public + shared-with-them. No create. |

Key rules:

- **Exactly one `superadmin` exists at all times.** It is the root of
  the tree and every other account has a `parent_user_id` pointing up.
  The superadmin row cannot be deleted, cannot be demoted, and cannot
  be disabled through the UI.
- **Admins can only create `contributor` / `viewer` accounts.** They
  cannot create other admins — only `superadmin` can. This keeps the
  admin-creation audit trail narrow: who minted admin X? Always the
  superadmin.
- **An admin's management surface is their subtree.** They can edit
  and delete accounts they (directly or transitively) created. They
  cannot touch siblings, other admins' subtrees, or the superadmin.
- Role transitions an admin can perform: `contributor ↔ viewer`
  within their subtree. Promotions to `admin` are superadmin-only.

### 2.1 Transition from Wave 8b

Wave 8b deployments have N existing admins and M existing viewers, all
flat. The `0004_user_hierarchy` migration:

1. Adds `parent_user_id` (nullable).
2. Picks the **oldest** existing `admin` by `created_at` and promotes
   them to `superadmin` (updates `role` + sets `parent_user_id = NULL`).
   Tie-breaks on lowest `id` for determinism.
3. Every other existing admin becomes `admin` with
   `parent_user_id = <the-new-superadmin.id>`.
4. Every existing viewer becomes `viewer` with
   `parent_user_id = <the-new-superadmin.id>`.
5. Adds a CHECK / partial-unique constraint so only one row can have
   `role = 'superadmin'` at a time (see §2.3 for the exact enforcement).

Env-bootstrap behavior changes accordingly: on a fresh DB, the **first
entry** of `MCP_WEBUI_OPERATORS` becomes `superadmin`; subsequent
entries become `admin` under it. An empty table + empty env still logs
the "refuse all logins" warning, same as today.

### 2.2 `parent_user_id` column

```python
parent_user_id: Mapped[str | None] = mapped_column(
    String,
    ForeignKey("users.id", ondelete="RESTRICT"),
    nullable=True, index=True,
)
```

- `NULL` iff `role == "superadmin"`. Enforced by a CHECK:
  `(parent_user_id IS NULL) = (role = 'superadmin')`.
- `ON DELETE RESTRICT` — you cannot delete a user while they still
  have children. The delete handler (§3.3) does the re-parenting
  first, inside the same transaction.

### 2.3 Superadmin singleton invariant

Three layers guard the "exactly one superadmin" rule:

1. **Unique partial index** on `role = 'superadmin'`:
   `CREATE UNIQUE INDEX ix_users_one_superadmin ON users(role) WHERE role = 'superadmin'`
   — any INSERT/UPDATE that would produce a second row errors out at
   the DB level.
2. **Service layer** refuses to delete / disable / demote a user whose
   role is `superadmin`. Returns 409 with a helpful message.
3. **UI** hides the delete / disable / role-change controls for the
   superadmin row and substitutes a lock badge.

A follow-up wave can add a "transfer superadmin" admin flow (promote
someone else, then demote self), but it's out of scope here — the
initial cut treats the role as set-once-at-bootstrap.

---

## 3. Ownership

### 3.1 Data model

Two new columns on `skills` and `skillsets`:

```python
owner_user_id: Mapped[str | None] = mapped_column(
    String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
)
owner_email_snapshot: Mapped[str | None] = mapped_column(String, nullable=True)
```

`owner_user_id` is the authoritative pointer. `owner_email_snapshot` is
denormalized for two reasons:

1. The allow list (below) is email-based and may reference users who
   don't exist yet. Keeping the owner as an email makes the two
   identity spaces symmetrical.
2. If the owner account is deleted, the skill shouldn't vanish from
   the admin UI — we want to see `"owned by deleted-user@corp.com"`
   rather than an empty cell.

Deleting a user is never a "nuke the row" operation — it always
triggers a reassignment step that moves **child accounts** and
**owned catalog rows** onto a new account in the same transaction, so
the hierarchy and the permission model stay whole.

There are two reassignment targets the caller can choose between:

#### 3.3.1 Default: reassign to the deleted user's parent

If the caller doesn't specify a target, the deleted user's parent
inherits everything. No promotion needed — a parent is always at
least one tier above the child (admin parent → contributor/viewer
child; superadmin parent → admin child).

```python
def delete_user(db, user_id, *, new_owner_id: str | None = None) -> bool:
    victim = db.get(User, user_id)
    if victim is None:
        return False
    if victim.role == "superadmin":
        raise ValueError("Cannot delete the superadmin")

    target_id = new_owner_id or victim.parent_user_id
    _reassign_and_delete(db, victim, target_id)
    return True


def _reassign_and_delete(db, victim, target_id):
    # 1. Re-parent children. `parent_user_id` FK is ON DELETE RESTRICT,
    #    so this must happen before the DELETE or the row won't leave.
    db.query(User).filter(User.parent_user_id == victim.id).update({
        "parent_user_id": target_id,
    })

    # 2. Reassign owned catalog rows to the same target.
    target_email = _email_of(db, target_id)
    db.query(Skill).filter(Skill.owner_user_id == victim.id).update({
        "owner_user_id": target_id,
        "owner_email_snapshot": target_email,
    })
    db.query(Skillset).filter(Skillset.owner_user_id == victim.id).update(...)

    # 3. Promote target if the inheritance requires it (see 3.3.2).
    _maybe_promote(db, target_id, victim)

    # 4. Drop the victim row.
    db.delete(victim)
    db.commit()
```

#### 3.3.2 Operator-picked target with auto-promotion

The UI exposes a "Reassign to a specific account" option on the
delete dialog (§6.2.1). The handler receives `new_owner_id` and
validates:

- The target exists and is in the **caller's management surface**
  (their subtree, or anyone if caller is `superadmin`).
- The target is not the victim themselves.
- The target is not a descendant of the victim — reassigning to
  someone whose `parent_user_id` chains back through the victim
  would orphan them mid-transaction. The handler rejects this with
  409 and a hint.

Because the chosen target may be a peer or sibling of the victim
(not a parent), its current role can be too low to hold what it's
inheriting. The auto-promotion rule:

| Victim's role | Inherited from victim | Minimum target role |
| ------------- | --------------------- | -------------------- |
| `admin` | child contributors/viewers + owned catalog | `admin` |
| `contributor` | owned catalog rows (no children) | `contributor` |
| `viewer` | nothing (viewers own nothing, have no children) | `viewer` (no-op) |

Concretely:

- Delete an **admin**, reassign to a `contributor` → target promoted
  to `admin`. Delete an admin, reassign to a `viewer` → target
  promoted to `admin`. (Viewers never have subordinates today, but
  the moment they inherit any, they stop being viewers.)
- Delete a **contributor**, reassign to a `viewer` → target promoted
  to `contributor`. Reassign to another `contributor` or `admin` →
  no change.
- Only `superadmin` can promote someone to `admin`. If the caller
  is an `admin` and the chosen target would need promotion to
  `admin` to hold the inheritance, the handler returns 403 with
  "choose an existing admin or ask the superadmin to perform this
  delete." This keeps the rule "only superadmins mint admins"
  (§2) honest even through the delete path.

Promotion bumps are logged as a structured event (`user.promoted`,
with `reason=inheritance`) so the audit trail captures any role
change the operator didn't explicitly click for.

#### 3.3.3 Safety nets

- `acting_admin_id` (session) is passed to the HTTP handler; the
  handler validates both "can I delete this victim?" and "can I
  reassign to this target?" before calling the service. The service
  enforces hierarchy invariants (singleton superadmin, no cycles,
  parent exists) but doesn't decide authorization.
- Admin-key CLI deletes (no session) still call the service with
  `new_owner_id` either explicit (CLI flag) or defaulted to the
  victim's parent. No orphan rows.
- The FK on `owner_user_id` stays `ON DELETE SET NULL` as a safety
  net for direct-DB surgery that bypasses the service entirely. Any
  rows that end up orphaned that way surface on a `/users/orphans`
  admin sweep page (9.1).

Why the handler-level reassignment (rather than just SET NULL):

- Keeps every catalog row with a live owner at all times.
  Permission checks never have a special "orphan" branch.
- Keeps the hierarchy total — no floating subtrees. The tree
  invariants at §2 can be checked with a single recursive query
  against `parent_user_id` without needing to exclude orphan roots.
- Deterministic regardless of who triggers the delete — two admins
  performing the same action (same victim, same target) leave the
  DB in the same state.

### 3.2 Semantics

- A newly-created skill / skillset is owned by the **authenticated user
  who created it**. The POST endpoint reads the session and stamps
  ownership server-side; clients cannot spoof `owner_user_id`.
- Items created via the admin-key CLI path (no session) land with
  `owner_user_id = NULL`; admins manage these explicitly. Adding an
  owner is a one-click action in the UI.
- Ownership transfer is admin-only. A contributor cannot hand their
  skill to someone else without admin involvement.

### 3.3 Migration for existing rows

Migration `0004_ownership`:

```sql
ALTER TABLE skills ADD COLUMN owner_user_id TEXT
    REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE skills ADD COLUMN owner_email_snapshot TEXT;
CREATE INDEX ix_skills_owner_user_id ON skills(owner_user_id);
-- Same for skillsets.
```

Existing rows get `NULL` ownership (admin-owned). Admins can bulk-assign
from a new `/admin/catalog/assign-owner` page, or leave them admin-only.

---

## 4. Allow list (email-based)

### 4.1 Goals

- Share a private skill / skillset with a known email **without**
  creating a user account for them.
- When that email later registers, the allow list entry resolves to
  their user without manual rework.
- Allow list works independently of the `public` flag — setting
  `visibility = public` makes the allow list moot, but we don't
  delete it (toggling back to private restores the shared access).

### 4.2 Data model

```python
class SkillShare(Base):
    __tablename__ = "skill_shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    skill_id: Mapped[str] = mapped_column(String, ForeignKey("skills.id",
        ondelete="CASCADE"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)  # normalized
    granted_by_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint("skill_id", "email",
        name="uq_skill_share_skill_email"),)
```

`SkillsetShare` is parallel. No FK from `email` to `users.email`,
deliberately — we want allow-list entries for unregistered addresses.

**Why rows keyed by email (not user_id):** if we keyed by user, then
sharing with a future user would be impossible. The FK-less email
string is the canonical identifier; when a user registers with that
email, their new session's `email` field automatically matches the
existing share row.

### 4.3 Semantics

Visibility check (replaces the Wave 8a rule):

```python
def can_read(resource, user) -> bool:
    if resource.visibility == "public":
        return True
    if user and user.role in ("admin", "superadmin"):
        return True
    if user and resource.owner_user_id == user.id:
        return True
    if user and user.email and share_exists(resource, user.email):
        return True
    return False
```

For the **agent-facing** authorization layer (JWT claims), we still
only use `public` + explicit grants. Allow lists are for **operator
UI access**, not token scopes — an agent's token is still bounded by
the registered agent config. See §7 for the rationale.

### 4.4 UI

On the skill / skillset edit page, new "Sharing" card:

- Radio: `private` / `public` (existing).
- When `private`: textarea + "Add" button to type one email per line.
  Each added email becomes a chip with an (x) button.
- Table of existing entries: email, status (`registered` / `pending`),
  granted-by, granted-at, remove button.

**Email normalization** at insert: `.strip().lower()`. Basic syntax
check (regex `^[^@]+@[^@]+\.[^@]+$`); deeper deliverability is out of
scope. Duplicates are a no-op (UNIQUE constraint).

### 4.5 Notification (optional, Wave 9.1)

Initial Wave 9 does not send email. A pending email is just a row;
the grantee discovers the share when they next log in. A later wave
can add SMTP config + a "invite" flow that mails the grantee a link.

---

## 5. API changes

### 5.1 New endpoints

Sharing:

```
POST   /skills/{id}/shares       body: {email}      owner/admin only
GET    /skills/{id}/shares                           owner/admin only
DELETE /skills/{id}/shares/{share_id}                owner/admin only

POST   /skillsets/{id}/shares    body: {email}
GET    /skillsets/{id}/shares
DELETE /skillsets/{id}/shares/{share_id}
```

User-management (extends Wave 8b `/admin/users/*`):

```
POST   /admin/users                body: {email, password, role,
                                          display_name, parent_user_id}
  - `role` allowed values depend on caller: admin can only create
    contributor / viewer; superadmin can additionally create admin.
  - `parent_user_id` defaults to the caller's id. Admins can only
    specify themselves (or descend into their subtree for
    re-parenting flows). Superadmin can specify any admin.

DELETE /admin/users/{id}          query: ?new_owner_id=<uid>   # optional
  - No query param → reassign to deleted user's parent (default).
  - `new_owner_id=<uid>` → reassign the subtree + owned catalog rows
    to that account instead. Auto-promotes the target if its role
    is below what it needs to inherit (see §3.3.2).

GET    /admin/users/{id}/descendants   # subtree listing for admins
```

### 5.2 Modified endpoints

- `POST /skills` and `POST /skillsets`: stamp `owner_user_id` from the
  session. Admin-key-only callers (CLI) get `NULL` ownership.
- `GET /skills` and `GET /skillsets`: filter to items the caller can
  see per §4.3. `superadmin` and `admin` see everything;
  `contributor` / `viewer` see owned + shared + public. Query param
  `?mine=1` narrows to "owned by me".
- `PUT` / `DELETE` on skills / skillsets: require ownership OR a
  managing role (`admin` / `superadmin`). 403 otherwise.

### 5.3 Validation

- Email field: trimmed + lowercased. 400 on syntactically invalid.
- Cannot share with your own email (no-op + 400, clarity).
- Cannot share with a `public` resource (400 with hint: "already
  world-readable").

---

## 6. Web UI changes

### 6.1 Skill list page

Add an "Owner" column. For the signed-in user's own skills, a
lightning-bolt badge. Filter pills: `All catalog` / `Mine` / `Shared
with me` / `Public`.

### 6.2 Skill detail

"Owner" line under the title. Shows owner email; for admins, a
"Reassign" button that opens a modal with a user picker.

Replace the existing visibility radio with a richer **Sharing** card:

```
┌─ Sharing ───────────────────────────────────┐
│ ○ Private (owner + allow list)              │
│ ● Public (any authenticated agent)          │
│                                              │
│ Allow list (private only):                   │
│   alice@customer.com   [registered] [×]     │
│   bob@customer.com     [pending]    [×]     │
│   + Add email                                │
└──────────────────────────────────────────────┘
```

### 6.2.1 Delete-user dialog

Clicking Delete on a user row opens a modal instead of issuing the
DELETE immediately:

```
┌─ Delete user: alice@customer.com ─────────────────────────┐
│ This user owns 3 skills and 1 skillset.                   │
│ This user manages 2 accounts (bob@x.com, carol@x.com).    │
│                                                            │
│ Reassign everything to:                                    │
│   ● alice's parent (default) — you, admin@ops.com         │
│   ○ Someone else:     [ dropdown of accounts in subtree ] │
│                                                            │
│ ⚠ If reassigning to a contributor / viewer, they'll be    │
│   promoted to admin so they can manage inherited accounts.│
│                                                            │
│  [ Cancel ]                              [ Delete user ]  │
└────────────────────────────────────────────────────────────┘
```

The promotion warning renders conditionally — the form inspects the
target's current role + what the victim carries and shows the exact
transition ("Promote bob@x.com from contributor → admin"). Admin-tier
promotions are only offered when the logged-in caller is a superadmin
(§3.3.2).

### 6.3 Users page

Replaces the Wave 8b flat list with a subtree view:

- Superadmin sees the whole tree (indented rows, collapsible).
- Admin sees themselves + their descendants.
- Each row shows role, email, "Owns" count (skills + skillsets),
  "Manages" count (direct children), last login.
- Superadmin row has a lock badge and no delete / disable controls
  (§2.3).
- "New user" button on every admin's row scope-limits the create
  form to `contributor` / `viewer` with `parent_user_id` pre-filled;
  the superadmin's button additionally allows `admin` with any
  target parent.

### 6.4 Account page

A new "My catalog" section with links to:
- My skills (= `/skills?mine=1`)
- My skillsets (= `/skillsets?mine=1`)
- Shared with me (= `/skills?shared=1`)

---

## 7. Why allow lists don't bleed into JWT scope

A reader might ask: if alice@customer.com is on the allow list of
skill X, shouldn't her agents' JWTs automatically include X?

No, deliberately. Agents are first-class entities in the
[`agents` table](../mcp_server/models.py), registered by admins, with
explicit skillset / skill / scope grants. They are not "owned" by a
human operator and a human operator's UI access does not automatically
translate to agent capability:

- Operators are humans browsing a web UI.
- Agents are long-lived service identities with narrow scoped tokens.
- The allow list lets a human *see* a skill so they can decide
  whether to register an agent against it — but the agent grant is
  still an explicit admin action.

Keeping the two identity planes separate also means a compromised
operator session can't silently expand an agent's reach.

---

## 8. Role check implementation

Two new dependencies, layered on the Wave 8b `require_role`:

```python
def require_ownership_or_role(resource_kind: str, *allowed_roles: str):
    """FastAPI dep factory — fails with 403 unless the session operator
    either (a) owns the path-id'd resource or (b) has one of
    `allowed_roles`. `admin` and `superadmin` always pass the role
    gate."""

def require_ancestor_of(path_param: str = "user_id"):
    """FastAPI dep factory for `/admin/users/{user_id}` routes —
    fails with 403 unless the session operator is an ancestor of the
    target user in the hierarchy, OR is the superadmin."""
```

Gating:

- `POST /skills`, `POST /skillsets` → `require_role("contributor", "admin", "superadmin")` (viewers can't create).
- `PUT`/`DELETE` on skills / skillsets → `require_ownership_or_role("admin", "superadmin")`.
- `/admin/users/*` mutations → `require_ancestor_of("user_id")`.
- Promoting to `admin` (either direct or via delete-reassign auto-promote) → superadmin-only (checked inside the handler, not a dep, because it's a conditional branch).

---

## 9. Tests

Test suites to add (parallel to Wave 8b's `test_users.py`):

- `tests/test_hierarchy.py` — migration from the flat 8b model;
  singleton-superadmin invariant (DB-level uniqueness + service
  refusal); cycle detection on reassignment; `require_ancestor_of`
  dep rejects cross-subtree requests.
- `tests/test_user_delete.py` — default reassignment to parent;
  operator-picked target with auto-promotion (contributor → admin,
  viewer → contributor / admin); admin cannot pick a target that would
  need promotion to `admin` (403); superadmin can; reassigning to
  a descendant of the victim returns 409.
- `tests/test_ownership.py` — service layer: create stamps owner;
  update by non-owner 403s; admin can update anything; owner can't
  transfer.
- `tests/test_shares.py` — email normalization, duplicate idempotency,
  unknown-email persistence (share survives without a matching user),
  registered-user matching (email → user.id lookup at read time).
- `tests/test_api_sharing.py` — HTTP integration covering the allow
  list endpoints + GET filtering.
- `tests/test_webui_sharing.py` — template renders share list, add /
  remove UX forwards the correct payload.
- `tests/test_webui_user_delete.py` — delete-user modal shows the
  correct promotion warning based on target + victim; form POSTs
  `new_owner_id`.

Target: keep the 85% coverage gate. New code is ~650 LoC; tests
should add ~500 LoC.

---

## 10. Out of scope

- Group-based allow lists (`@customer.com` wildcard, or LDAP groups).
  Postpone until someone asks.
- Per-version shares (sharing only skill `v1.2.0`, not `v2.0.0`).
  Shares are skill-id scoped and apply to all versions.
- Time-boxed shares (`expires_at`). Easy to add later — one column,
  one read filter.
- Audit log of share grants / revocations. Log lines are emitted
  today; a queryable audit table is the same follow-up called out in
  [visibility-and-accounts.md](visibility-and-accounts.md#out-of-scope-call-out-for-clarity).
- Email invitations when a share is added. Requires SMTP config; ship
  separately (Wave 9.1).
- Configurable fallback owner (e.g., `MCP_CATALOG_FALLBACK_OWNER_EMAIL`)
  as an implicit reassignment target. Wave 9 requires the operator to
  explicitly pick a target via the delete dialog (or accept the
  parent default). Add the env override if an org complains that
  their "ops-bot" service account should absorb deletions instead.
- "Transfer superadmin" flow (promote an existing admin to
  superadmin then demote self). Wave 9 treats the superadmin role
  as set-once-at-bootstrap. Worth doing when the role's owner
  changes — scope that wave to include a one-time-token confirmation
  step so it's not a single-click hand-off.
- Demotion-on-delete (e.g., auto-demote an admin back to contributor
  if they end up with no subordinates after a reassignment away from
  them).
  Deliberately skipped: promotions triggered by inheritance are safe,
  but automatic demotion surprises the operator and can drop
  capabilities they still rely on. Admins demote via the explicit
  edit flow instead.

---

## 11. Sequencing

| Step | Deliverable |
| ---- | ----------- |
| 9.0 | Migration `0004_user_hierarchy`: add `parent_user_id`, introduce `superadmin` + `contributor` roles, promote oldest admin, re-parent everyone to it. Unique partial index on superadmin. Service-layer invariants + `require_ancestor_of` dep. |
| 9.1 | Migration `0005_ownership`: `owner_user_id` + `owner_email_snapshot` on skills/skillsets. Stamp owner server-side on POST. New `/admin/users/{id}/descendants` endpoint. |
| 9.2 | Filter GET lists by ownership + new `?mine=` / `?shared=` params. |
| 9.3 | Migration `0006_shares`: `skill_shares` / `skillset_shares` tables + `/shares` CRUD endpoints. |
| 9.4 | Web UI: sharing card, Owner column, My Catalog page, subtree users page. |
| 9.5 | Delete-user modal with reassignment-target picker + auto-promotion preview. New `new_owner_id` query param on `DELETE /admin/users/{id}`. |
| 9.6 | Admin-led explicit ownership reassignment for catalog rows (separate from the delete flow). |
| 9.x | (optional) SMTP invitations; (optional) transfer-superadmin flow. |

Each sub-step ships independently, but there's a hard dependency
order at the bottom: 9.0 must land before 9.1 (ownership needs a
hierarchy to resolve against on delete), and 9.3 must land before
9.4's sharing card has anything to render. 9.2 and 9.5 can ship in
either order once 9.1 is in.
