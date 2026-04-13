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

class TestBootstrapDefault:
    def test_creates_default_and_attaches_existing_users(self, db_session):
        _mk_user(db_session, "a@x.com")
        _mk_user(db_session, "b@x.com")
        a = acct_svc.bootstrap_default_account(db_session)
        assert a is not None and a.name == "default"
        assert len(acct_svc.list_memberships(db_session, a.id)) == 2

    def test_noop_when_account_exists(self, db_session):
        admin = _mk_user(db_session, "admin@x.com")
        acct_svc.create_account(
            db_session, name="already-there", initial_admin_user_id=admin.id
        )
        # Default shouldn't be created — an account already exists.
        result = acct_svc.bootstrap_default_account(db_session)
        assert result is None
        assert len(acct_svc.list_accounts(db_session)) == 1
