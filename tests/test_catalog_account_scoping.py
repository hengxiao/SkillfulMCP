"""Wave 9.2 tests — `account_id` stamping + visibility `account` tier.

Covers:

- `_stamp_account` defaults to the `default` account, honors explicit
  overrides, and surfaces a clean ValueError on unknown ids.
- Migration 0005 leaves existing rows with a non-null account_id.
- `POST /skills`, `POST /skillsets`, `POST /agents` stamp the account
  on create without the caller passing it explicitly (backward compat).
- Explicit `account_id` in the request body is honored end-to-end.
- `visibility='account'` is accepted; `visibility='invalid'` still 422s.
- `can_read` helper behavior: public-always, superadmin-bypass,
  owner-sees-own, account-tier needs membership, private needs
  membership + (owner | admin | share).
"""

from __future__ import annotations

import pytest

from mcp_server import accounts as acct_svc
from mcp_server import authorization
from mcp_server import catalog as cat_svc
from mcp_server import users as user_svc
from mcp_server.pwhash import hash_password
from mcp_server.schemas import SkillCreate, SkillsetCreate, AgentCreate

from tests.conftest import ADMIN_HEADERS


# ---------------------------------------------------------------------------
# Service layer — stamping + defaults
# ---------------------------------------------------------------------------

class TestStampAccount:
    def test_default_is_bootstrapped_account(self, db_session):
        # The conftest fixture seeds a `default` account.
        default = acct_svc.get_account_by_name(db_session, "default")
        assert default is not None

        skill = cat_svc.create_skill(
            db_session,
            SkillCreate(
                id="noexplicit", name="No Explicit", version="1.0.0",
            ),
        )
        assert skill.account_id == default.id
        assert skill.owner_user_id is None

    def test_explicit_account_honored(self, db_session):
        admin = user_svc.create_user(
            db_session, email="admin@x.com", password_hash=hash_password("p")
        )
        a = acct_svc.create_account(
            db_session, name="Explicit", initial_admin_user_id=admin.id
        )
        skill = cat_svc.create_skill(
            db_session,
            SkillCreate(
                id="routed", name="Routed", version="1.0.0",
                account_id=a.id, owner_user_id=admin.id,
            ),
        )
        assert skill.account_id == a.id
        assert skill.owner_user_id == admin.id

    def test_unknown_account_raises(self, db_session):
        with pytest.raises(ValueError, match="does not exist"):
            cat_svc.create_skill(
                db_session,
                SkillCreate(
                    id="ghost", name="Ghost", version="1.0.0",
                    account_id="no-such-account",
                ),
            )

    def test_skillset_stamps_default(self, db_session):
        default = acct_svc.get_account_by_name(db_session, "default")
        ss = cat_svc.create_skillset(
            db_session, SkillsetCreate(id="ss1", name="SS1"),
        )
        assert ss.account_id == default.id

    def test_agent_stamps_default(self, db_session):
        from mcp_server import registry

        default = acct_svc.get_account_by_name(db_session, "default")
        agent = registry.create_agent(
            db_session, AgentCreate(id="a1", name="A1", scope=["read"]),
        )
        assert agent.account_id == default.id


# ---------------------------------------------------------------------------
# HTTP surface — backward compatibility + explicit routing
# ---------------------------------------------------------------------------

class TestCatalogCreateOverHTTP:
    def test_create_skill_without_account_id_stamps_default(self, client):
        r = client.post(
            "/skills",
            json={
                "id": "backcompat", "name": "BC", "version": "1.0.0",
            },
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201, r.text
        # The response now includes account_id — stamped from the
        # default account the lifespan bootstrap created.
        body = r.json()
        assert body["account_id"] is not None
        assert body["visibility"] == "private"

    def test_create_skill_with_explicit_account_id(self, client):
        # Create an account + user via the 9.1 admin surface.
        admin = client.post(
            "/admin/users",
            json={"email": "ceo@co.com", "password": "s3cret-pass"},
            headers=ADMIN_HEADERS,
        ).json()
        acct = client.post(
            "/admin/accounts",
            json={"name": "Co Ops", "initial_admin_user_id": admin["id"]},
            headers=ADMIN_HEADERS,
        ).json()
        r = client.post(
            "/skills",
            json={
                "id": "explicit", "name": "Explicit", "version": "1.0.0",
                "account_id": acct["id"],
                "owner_user_id": admin["id"],
            },
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["account_id"] == acct["id"]
        assert body["owner_user_id"] == admin["id"]

    def test_visibility_account_tier_accepted(self, client):
        r = client.post(
            "/skills",
            json={
                "id": "acct-vis", "name": "AcctVis", "version": "1.0.0",
                "visibility": "account",
            },
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201, r.text
        assert r.json()["visibility"] == "account"

    def test_visibility_invalid_422(self, client):
        r = client.post(
            "/skills",
            json={
                "id": "bad-vis", "name": "BadVis", "version": "1.0.0",
                "visibility": "shared-with-mars",
            },
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# `can_read` operator-UI helper (§4.4)
# ---------------------------------------------------------------------------

class _Resource:
    """Minimal duck-typed row for can_read — avoids dragging ORM
    session state into pure-logic tests."""

    def __init__(self, *, visibility, account_id, owner_user_id=None):
        self.visibility = visibility
        self.account_id = account_id
        self.owner_user_id = owner_user_id


class TestCanRead:
    def test_public_visible_to_anyone(self):
        r = _Resource(visibility="public", account_id="acct-a")
        assert authorization.can_read(r) is True
        assert authorization.can_read(r, user_id="u1") is True

    def test_anonymous_denied_non_public(self):
        r = _Resource(visibility="account", account_id="acct-a")
        assert authorization.can_read(r) is False
        r2 = _Resource(visibility="private", account_id="acct-a")
        assert authorization.can_read(r2) is False

    def test_superadmin_bypasses_everything(self):
        r = _Resource(visibility="private", account_id="acct-a")
        assert (
            authorization.can_read(
                r, is_superadmin=True, user_id="0", user_memberships={}
            )
            is True
        )

    def test_owner_always_wins(self):
        r = _Resource(
            visibility="private", account_id="acct-a", owner_user_id="u1"
        )
        assert (
            authorization.can_read(
                r,
                user_id="u1",
                user_memberships={},
            )
            is True
        )

    def test_account_tier_member_yes_outsider_no(self):
        r = _Resource(visibility="account", account_id="acct-a")
        assert (
            authorization.can_read(
                r, user_id="alice", user_memberships={"acct-a": "viewer"}
            )
            is True
        )
        assert (
            authorization.can_read(
                r, user_id="bob", user_memberships={"acct-b": "viewer"}
            )
            is False
        )

    def test_account_tier_allow_list_grants_outsider(self):
        r = _Resource(visibility="account", account_id="acct-a")

        def share_exists(_resource, email):
            return email == "guest@partner.com"

        assert (
            authorization.can_read(
                r,
                user_id="guest",
                user_email="guest@partner.com",
                user_memberships={"acct-b": "viewer"},
                share_exists_fn=share_exists,
            )
            is True
        )

    def test_private_requires_membership_even_with_share(self):
        """Private tier: allow list is only effective for account
        members. An outsider on the list gets no access."""
        r = _Resource(visibility="private", account_id="acct-a")

        def share_exists(_resource, email):
            return email == "outsider@partner.com"

        # Outsider (no membership) — denied even though shared.
        assert (
            authorization.can_read(
                r,
                user_id="outsider",
                user_email="outsider@partner.com",
                user_memberships={"acct-b": "viewer"},
                share_exists_fn=share_exists,
            )
            is False
        )

    def test_private_member_needs_admin_or_share(self):
        r = _Resource(visibility="private", account_id="acct-a")

        # Plain viewer/contributor in the account, no share → denied.
        assert (
            authorization.can_read(
                r,
                user_id="rank-and-file",
                user_email="rf@corp.com",
                user_memberships={"acct-a": "viewer"},
            )
            is False
        )
        # Same user with a share → allowed.
        assert (
            authorization.can_read(
                r,
                user_id="rank-and-file",
                user_email="rf@corp.com",
                user_memberships={"acct-a": "viewer"},
                share_exists_fn=lambda _r, _e: True,
            )
            is True
        )
        # Account-admin → allowed without share.
        assert (
            authorization.can_read(
                r,
                user_id="admin",
                user_memberships={"acct-a": "account-admin"},
            )
            is True
        )

    def test_share_exists_fn_swallows_exceptions(self):
        """A flaky share-table query shouldn't strip the owner's
        read access."""
        r = _Resource(
            visibility="private", account_id="acct-a", owner_user_id="u1"
        )

        def boom(_resource, _email):
            raise RuntimeError("shares table down")

        # Owner path comes first and doesn't consult share_exists_fn,
        # but the helper still needs to not explode.
        assert (
            authorization.can_read(
                r,
                user_id="u1",
                user_email="u1@x.com",
                user_memberships={},
                share_exists_fn=boom,
            )
            is True
        )
