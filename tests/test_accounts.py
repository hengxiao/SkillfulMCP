"""Wave 9.0 accounts + memberships service tests.

Covers :mod:`mcp_server.accounts` without going through HTTP — the
9.1 wave adds routers on top. The three big invariants to pin down
here:

- `create_account` atomically adds an `account-admin` membership for
  the caller AND stamps their `last_active_account_id`.
- The last-admin guard rejects the removal (or demotion) that would
  leave an account with zero active `account-admin` memberships.
- `consume_pending_for_user` turns pending invitations into real
  memberships at signup time.
"""

from __future__ import annotations

import pytest

from mcp_server import accounts as acct_svc
from mcp_server import users as user_svc
from mcp_server.accounts import LastAdminError
from mcp_server.pwhash import hash_password


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mk_user(db, email, *, disabled=False):
    u = user_svc.create_user(db, email=email, password_hash=hash_password("p"))
    if disabled:
        user_svc.update_user(db, u.id, disabled=True)
    return u


# ---------------------------------------------------------------------------
# Accounts CRUD
# ---------------------------------------------------------------------------

class TestCreateAccount:
    def test_creates_account_and_admin_membership(self, db_session):
        u = _mk_user(db_session, "alice@x.com")
        a = acct_svc.create_account(
            db_session, name="Corp Ops", initial_admin_user_id=u.id
        )
        assert a.name == "Corp Ops"

        # Membership row.
        m = acct_svc.get_membership(
            db_session, account_id=a.id, user_id=u.id
        )
        assert m is not None and m.role == "account-admin"

        # Active account stamped on the user.
        db_session.refresh(u)
        assert u.last_active_account_id == a.id

    def test_duplicate_name_rejected(self, db_session):
        u = _mk_user(db_session, "a@x.com")
        acct_svc.create_account(db_session, name="dup", initial_admin_user_id=u.id)
        with pytest.raises(ValueError, match="already in use"):
            acct_svc.create_account(
                db_session, name="dup", initial_admin_user_id=u.id
            )

    def test_unknown_initial_admin_rejected(self, db_session):
        with pytest.raises(ValueError, match="does not exist"):
            acct_svc.create_account(
                db_session, name="ghost", initial_admin_user_id="no-such-user"
            )


# ---------------------------------------------------------------------------
# Memberships + last-admin guard
# ---------------------------------------------------------------------------

class TestMemberships:
    def test_add_membership_enforces_valid_role(self, db_session):
        admin = _mk_user(db_session, "admin@x.com")
        a = acct_svc.create_account(
            db_session, name="T1", initial_admin_user_id=admin.id
        )
        bob = _mk_user(db_session, "bob@x.com")

        with pytest.raises(ValueError, match="role must be one of"):
            acct_svc.add_membership(
                db_session, account_id=a.id, user_id=bob.id, role="dictator"
            )

    def test_add_membership_duplicate_rejected(self, db_session):
        admin = _mk_user(db_session, "admin@x.com")
        a = acct_svc.create_account(
            db_session, name="T2", initial_admin_user_id=admin.id
        )
        bob = _mk_user(db_session, "bob@x.com")
        acct_svc.add_membership(
            db_session, account_id=a.id, user_id=bob.id, role="contributor"
        )
        with pytest.raises(ValueError, match="already has a membership"):
            acct_svc.add_membership(
                db_session, account_id=a.id, user_id=bob.id, role="viewer"
            )

    def test_remove_membership_returns_false_when_absent(self, db_session):
        admin = _mk_user(db_session, "admin@x.com")
        a = acct_svc.create_account(
            db_session, name="T3", initial_admin_user_id=admin.id
        )
        ghost = _mk_user(db_session, "ghost@x.com")
        assert (
            acct_svc.remove_membership(db_session, account_id=a.id, user_id=ghost.id)
            is False
        )

    def test_last_admin_cannot_be_removed(self, db_session):
        admin = _mk_user(db_session, "admin@x.com")
        a = acct_svc.create_account(
            db_session, name="T4", initial_admin_user_id=admin.id
        )
        with pytest.raises(LastAdminError):
            acct_svc.remove_membership(
                db_session, account_id=a.id, user_id=admin.id
            )

    def test_last_admin_cannot_be_demoted(self, db_session):
        admin = _mk_user(db_session, "admin@x.com")
        a = acct_svc.create_account(
            db_session, name="T5", initial_admin_user_id=admin.id
        )
        with pytest.raises(LastAdminError):
            acct_svc.update_membership_role(
                db_session, account_id=a.id, user_id=admin.id, new_role="viewer"
            )

    def test_promote_contributor_then_remove_original_admin(self, db_session):
        admin = _mk_user(db_session, "admin@x.com")
        a = acct_svc.create_account(
            db_session, name="T6", initial_admin_user_id=admin.id
        )
        bob = _mk_user(db_session, "bob@x.com")
        acct_svc.add_membership(
            db_session, account_id=a.id, user_id=bob.id, role="contributor"
        )
        # Promote Bob; now there are two admins.
        acct_svc.update_membership_role(
            db_session, account_id=a.id, user_id=bob.id, new_role="account-admin"
        )
        # Remove the original admin — guard passes (Bob remains).
        assert (
            acct_svc.remove_membership(
                db_session, account_id=a.id, user_id=admin.id
            )
            is True
        )

    def test_disabled_admin_does_not_satisfy_last_admin_guard(self, db_session):
        """A disabled account-admin shouldn't count toward the 'remaining
        admins' total — they can't actually act."""
        admin = _mk_user(db_session, "admin@x.com")
        a = acct_svc.create_account(
            db_session, name="T7", initial_admin_user_id=admin.id
        )
        bob = _mk_user(db_session, "bob@x.com")
        acct_svc.add_membership(
            db_session, account_id=a.id, user_id=bob.id, role="account-admin"
        )
        # Disable bob.
        user_svc.update_user(db_session, bob.id, disabled=True)
        # Removing the *active* admin would strand the account — only a
        # disabled admin would remain.
        with pytest.raises(LastAdminError):
            acct_svc.remove_membership(
                db_session, account_id=a.id, user_id=admin.id
            )


# ---------------------------------------------------------------------------
# Pending memberships
# ---------------------------------------------------------------------------

class TestPendingMemberships:
    def test_consume_on_user_creation(self, db_session):
        admin = _mk_user(db_session, "admin@x.com")
        a = acct_svc.create_account(
            db_session, name="Invite Me", initial_admin_user_id=admin.id
        )
        # Admin invites an email that doesn't exist yet.
        acct_svc.add_pending_membership(
            db_session,
            account_id=a.id,
            email="Future@User.com",
            role="contributor",
            invited_by_user_id=admin.id,
        )
        # Future user signs up (simulated).
        future = _mk_user(db_session, "future@user.com")
        created = acct_svc.consume_pending_for_user(
            db_session, user_id=future.id, email="future@user.com"
        )
        assert len(created) == 1
        assert created[0].role == "contributor"
        assert created[0].account_id == a.id
        # Pending row is consumed.
        assert acct_svc.list_pending_for_email(db_session, "future@user.com") == []

    def test_duplicate_pending_rejected(self, db_session):
        admin = _mk_user(db_session, "admin@x.com")
        a = acct_svc.create_account(
            db_session, name="Dup Pending", initial_admin_user_id=admin.id
        )
        acct_svc.add_pending_membership(
            db_session, account_id=a.id, email="x@y.com", role="viewer"
        )
        with pytest.raises(ValueError, match="already exists"):
            acct_svc.add_pending_membership(
                db_session, account_id=a.id, email="x@y.com", role="contributor"
            )

    def test_consume_skips_existing_membership(self, db_session):
        """If the signing-up user already has a membership in the account
        (unusual but possible after a pending → admin race), the consume
        step silently drops the pending row without failing."""
        admin = _mk_user(db_session, "admin@x.com")
        a = acct_svc.create_account(
            db_session, name="Race", initial_admin_user_id=admin.id
        )
        # Admin pre-invites an email + also pre-adds a membership.
        acct_svc.add_pending_membership(
            db_session, account_id=a.id, email="racer@x.com", role="viewer"
        )
        racer = _mk_user(db_session, "racer@x.com")
        acct_svc.add_membership(
            db_session, account_id=a.id, user_id=racer.id, role="contributor"
        )
        # Consume: the pending row clears without clobbering the
        # existing membership's role.
        acct_svc.consume_pending_for_user(
            db_session, user_id=racer.id, email="racer@x.com"
        )
        assert acct_svc.list_pending_for_email(db_session, "racer@x.com") == []
        m = acct_svc.get_membership(
            db_session, account_id=a.id, user_id=racer.id
        )
        assert m.role == "contributor"


# ---------------------------------------------------------------------------
# Bootstrap the default account
# ---------------------------------------------------------------------------

class TestAccountCRUDEdges:
    def test_get_by_name_and_get_by_id(self, db_session):
        u = _mk_user(db_session, "admin@x.com")
        a = acct_svc.create_account(
            db_session, name="Named", initial_admin_user_id=u.id
        )
        assert acct_svc.get_account_by_name(db_session, "Named").id == a.id
        assert acct_svc.get_account_by_name(db_session, "does-not") is None
        assert acct_svc.get_account(db_session, a.id).name == "Named"
        assert acct_svc.get_account(db_session, "ghost") is None

    def test_empty_name_rejected(self, db_session):
        u = _mk_user(db_session, "admin@x.com")
        with pytest.raises(ValueError, match="required"):
            acct_svc.create_account(
                db_session, name="   ", initial_admin_user_id=u.id
            )

    def test_delete_account_removes_memberships_by_cascade(self, db_session):
        admin = _mk_user(db_session, "admin@x.com")
        a = acct_svc.create_account(
            db_session, name="GoAway", initial_admin_user_id=admin.id
        )
        # Membership exists now.
        assert len(acct_svc.list_memberships(db_session, a.id)) == 1
        assert acct_svc.delete_account(db_session, a.id) is True
        # Membership is gone via FK CASCADE.
        assert acct_svc.list_memberships(db_session, a.id) == []
        # Service returns False when asked to delete a missing row.
        assert acct_svc.delete_account(db_session, a.id) is False


class TestPendingEdges:
    def test_unknown_account_rejected(self, db_session):
        with pytest.raises(ValueError, match="does not exist"):
            acct_svc.add_pending_membership(
                db_session, account_id="no-such",
                email="x@y.com", role="viewer",
            )

    def test_empty_email_rejected(self, db_session):
        u = _mk_user(db_session, "admin@x.com")
        a = acct_svc.create_account(
            db_session, name="Zero", initial_admin_user_id=u.id
        )
        with pytest.raises(ValueError, match="email is required"):
            acct_svc.add_pending_membership(
                db_session, account_id=a.id, email="  ", role="viewer",
            )

    def test_list_for_email_empty(self, db_session):
        assert acct_svc.list_pending_for_email(db_session, "") == []
        assert acct_svc.list_pending_for_email(db_session, "nobody@x.com") == []

    def test_delete_pending(self, db_session):
        admin = _mk_user(db_session, "admin@x.com")
        a = acct_svc.create_account(
            db_session, name="DelMe", initial_admin_user_id=admin.id
        )
        p = acct_svc.add_pending_membership(
            db_session, account_id=a.id, email="x@y.com", role="viewer"
        )
        assert acct_svc.delete_pending_membership(db_session, p.id) is True
        assert acct_svc.delete_pending_membership(db_session, p.id) is False


class TestMembershipRoleUpdate:
    def test_noop_on_same_role(self, db_session):
        admin = _mk_user(db_session, "admin@x.com")
        a = acct_svc.create_account(
            db_session, name="NoOp", initial_admin_user_id=admin.id
        )
        bob = _mk_user(db_session, "bob@x.com")
        acct_svc.add_membership(
            db_session, account_id=a.id, user_id=bob.id, role="viewer"
        )
        # Same role → no change, returns the row.
        m = acct_svc.update_membership_role(
            db_session, account_id=a.id, user_id=bob.id, new_role="viewer"
        )
        assert m.role == "viewer"

    def test_unknown_membership_rejected(self, db_session):
        admin = _mk_user(db_session, "admin@x.com")
        a = acct_svc.create_account(
            db_session, name="Ghost", initial_admin_user_id=admin.id
        )
        with pytest.raises(ValueError, match="no membership"):
            acct_svc.update_membership_role(
                db_session, account_id=a.id, user_id="nobody", new_role="viewer",
            )

    def test_invalid_role_rejected(self, db_session):
        admin = _mk_user(db_session, "admin@x.com")
        a = acct_svc.create_account(
            db_session, name="Bad", initial_admin_user_id=admin.id
        )
        with pytest.raises(ValueError, match="role must be one of"):
            acct_svc.update_membership_role(
                db_session, account_id=a.id, user_id=admin.id, new_role="tyrant",
            )

    def test_list_for_user_covers_all_accounts(self, db_session):
        """A user with memberships in two accounts should appear in
        list_memberships_for_user with both rows."""
        alice = _mk_user(db_session, "alice@x.com")
        bob = _mk_user(db_session, "bob@x.com")
        a1 = acct_svc.create_account(
            db_session, name="A1", initial_admin_user_id=alice.id
        )
        a2 = acct_svc.create_account(
            db_session, name="A2", initial_admin_user_id=bob.id
        )
        acct_svc.add_membership(
            db_session, account_id=a2.id, user_id=alice.id, role="contributor"
        )
        alice_mems = acct_svc.list_memberships_for_user(db_session, alice.id)
        roles = {m.account_id: m.role for m in alice_mems}
        assert roles == {a1.id: "account-admin", a2.id: "contributor"}


class TestBootstrapDefault:
    @staticmethod
    def _wipe_accounts(db_session):
        """Remove the `default` account seeded by the conftest
        fixture so these tests can observe bootstrap_default_account
        on a pristine DB."""
        from mcp_server.models import Account
        db_session.query(Account).delete()
        db_session.commit()

    def test_creates_default_and_attaches_existing_users(self, db_session):
        self._wipe_accounts(db_session)
        _mk_user(db_session, "a@x.com")
        _mk_user(db_session, "b@x.com")
        a = acct_svc.bootstrap_default_account(db_session)
        assert a is not None and a.name == "default"
        assert len(acct_svc.list_memberships(db_session, a.id)) == 2

    def test_noop_when_account_exists(self, db_session):
        self._wipe_accounts(db_session)
        admin = _mk_user(db_session, "admin@x.com")
        acct_svc.create_account(
            db_session, name="already-there", initial_admin_user_id=admin.id
        )
        # Default shouldn't be created — an account already exists.
        result = acct_svc.bootstrap_default_account(db_session)
        assert result is None
        assert len(acct_svc.list_accounts(db_session)) == 1
