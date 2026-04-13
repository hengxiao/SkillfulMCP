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

- **"My skills"** — a normal user should CRUD the skills they created
  without being able to touch someone else's.
- **Targeted sharing** — "let alice@customer.com and bob@customer.com
  view this skill without giving them operator accounts."

These map to the familiar GitHub / Google Docs model:
owner + per-resource allow list + public toggle.

---

## 2. Roles

Wave 8b has `admin` and `viewer`. Wave 9 adds one more and redefines
them:

| Role | Capabilities |
| ---- | ------------ |
| `admin` | Everything. Manages users, sees / edits every resource regardless of owner, can impersonate. |
| `user` | Normal operator. CRUD on the resources they **own**; read on resources shared with them (public, or their email on the allow list). |
| `viewer` | Read-only. Can browse public items + items shared with them. Cannot create anything. |

The existing Wave 8b deployments have only `admin` and `viewer` users.
Migration: every existing `admin` stays `admin`; existing `viewer`s stay
`viewer`. The new `user` role is introduced alongside; admins can
promote or create new users at any role.

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

`ondelete="SET NULL"` at the FK level prevents cascading-deletes of
catalog data, but we don't actually want orphans. The user-delete
**handler** runs a reassignment step inside the same transaction before
the row is removed:

```python
def delete_user(db, user_id, *, acting_admin_id: str) -> bool:
    # Reassign the deleted user's owned items to the admin performing
    # the delete. Keeps a live owner on every row so nothing falls
    # through the cracks of permission checks.
    db.query(Skill).filter(Skill.owner_user_id == user_id).update({
        "owner_user_id": acting_admin_id,
        "owner_email_snapshot": _email_of(db, acting_admin_id),
    })
    db.query(Skillset).filter(Skillset.owner_user_id == user_id).update(...)
    db.delete(user)
    db.commit()
```

The acting admin's id is taken from the session — the HTTP handler
passes it through, so the service layer never has to guess. Emits a
structured log line per reassigned row for the audit trail.

Why reassign-to-deleter (rather than NULL + manual cleanup):

- Keeps every catalog row with a live owner at all times. Permission
  checks stay simple — no special case for "orphan" rows.
- "You deleted them, you own the mess" is the least surprising
  default for the admin driving the action. They can transfer
  ownership onward after the fact using the standard reassign flow
  (§6.2).
- Admin-key CLI deletes (no session) fall back to SET NULL. Those
  rows surface on a new `/users/orphans` admin page so the ops team
  can sweep them.

The FK stays `ON DELETE SET NULL` as a safety net for the CLI path
and for any future direct-DB surgery; the handler is the primary
ownership policy, not the database.

### 3.2 Semantics

- A newly-created skill / skillset is owned by the **authenticated user
  who created it**. The POST endpoint reads the session and stamps
  ownership server-side; clients cannot spoof `owner_user_id`.
- Items created via the admin-key CLI path (no session) land with
  `owner_user_id = NULL`; admins manage these explicitly. Adding an
  owner is a one-click action in the UI.
- Ownership transfer is admin-only. A user cannot hand their skill
  to someone else without admin involvement.

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
    if user and user.role == "admin":
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

```
POST   /skills/{id}/shares       body: {email}      owner/admin only
GET    /skills/{id}/shares                           owner/admin only
DELETE /skills/{id}/shares/{share_id}                owner/admin only

POST   /skillsets/{id}/shares    body: {email}
GET    /skillsets/{id}/shares
DELETE /skillsets/{id}/shares/{share_id}
```

### 5.2 Modified endpoints

- `POST /skills` and `POST /skillsets`: stamp `owner_user_id` from the
  session. Admin-key-only callers (CLI) get `NULL` ownership.
- `GET /skills` and `GET /skillsets`: filter to items the caller can
  see per §4.3. Admins see everything; users see owned + shared +
  public. Query param `?mine=1` narrows to "owned by me".
- `PUT` / `DELETE` on skills / skillsets: require ownership OR
  `admin` role. 403 otherwise.

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

### 6.3 Users page (admins only, Wave 8b)

No schema change. Add an "Owns" column counting owned skills +
skillsets, linking to a filtered list.

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

A new dependency, analogous to the Wave 8b `require_role`:

```python
def require_ownership_or_role(resource_kind: str, *allowed_roles: str):
    """FastAPI dep factory — fails with 403 unless the session operator
    either (a) owns the path-id'd resource or (b) has one of
    `allowed_roles`."""
```

Used on every mutating `/skills/{id}` and `/skillsets/{id}` route.

Viewers (`role=viewer`) cannot create anything; the existing
`require_role("admin", "user")` dep goes on POST endpoints.

---

## 9. Tests

Test suites to add (parallel to Wave 8b's `test_users.py`):

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

Target: keep the 85% coverage gate. New code is ~400 LoC; tests
should add ~300 LoC.

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
  for the user-delete reassignment. Wave 9 hard-codes "reassign to the
  acting admin" because that's the least surprising default and avoids
  another config knob. Add the override if an org complains that their
  "ops-bot" service account should absorb deletions instead.

---

## 11. Sequencing

| Step | Deliverable |
| ---- | ----------- |
| 9.0 | Role rename (`viewer` stays, add `user`) + `require_role` update |
| 9.1 | Migration `0004_ownership` + stamp owner in POST handlers |
| 9.2 | Filter GET lists by ownership + new `?mine=` / `?shared=` params |
| 9.3 | Migration `0005_shares` + `/shares` CRUD endpoints |
| 9.4 | Web UI: Sharing card, Owner column, My Catalog page |
| 9.5 | Admin: reassign-ownership UI |
| 9.x | (optional) SMTP invitations |

Each sub-step ships independently. 9.1 without 9.3 is still useful —
you get per-user "my skills" even before allow lists land.
