"""HTTP tests for `/admin/accounts/*` (Wave 9.1).

Pairs with tests/test_accounts.py which exercises the service layer.
This file pins the HTTP envelope: status codes, response shapes,
delete interlocks, invite→pending path.
"""

from __future__ import annotations

from tests.conftest import ADMIN_HEADERS


def _mk_user(client, email, password="s3cret-pass"):
    r = client.post(
        "/admin/users",
        json={"email": email, "password": password},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _mk_account(client, name, admin_user_id):
    r = client.post(
        "/admin/accounts",
        json={"name": name, "initial_admin_user_id": admin_user_id},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 201, r.text
    return r.json()


class TestAccountCRUD:
    def test_create_list_get(self, client):
        admin = _mk_user(client, "ceo@corp.com")
        a = _mk_account(client, "Corp Ops", admin["id"])
        assert a["name"] == "Corp Ops"

        r = client.get("/admin/accounts", headers=ADMIN_HEADERS)
        assert r.status_code == 200
        names = [row["name"] for row in r.json()]
        assert "Corp Ops" in names

        r = client.get(f"/admin/accounts/{a['id']}", headers=ADMIN_HEADERS)
        assert r.status_code == 200
        assert r.json()["id"] == a["id"]

    def test_get_unknown_404(self, client):
        r = client.get("/admin/accounts/no-such", headers=ADMIN_HEADERS)
        assert r.status_code == 404

    def test_duplicate_name_409(self, client):
        admin = _mk_user(client, "a@x.com")
        _mk_account(client, "Dup", admin["id"])
        r = client.post(
            "/admin/accounts",
            json={"name": "Dup", "initial_admin_user_id": admin["id"]},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 409

    def test_unknown_initial_admin_409(self, client):
        r = client.post(
            "/admin/accounts",
            json={"name": "Ghost", "initial_admin_user_id": "nobody"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 409


class TestAccountDelete:
    def test_deletes_empty_account(self, client):
        # The bootstrapped `default` account may still have the env
        # operator as a member, so create a fresh empty-ish account
        # and remove its admin first — but we can't (last-admin
        # guard). Instead, test the interlock: even a 1-member
        # account requires confirm_user_count=1.
        admin = _mk_user(client, "admin@x.com")
        a = _mk_account(client, "Deletable", admin["id"])

        # Wrong confirm count → 409.
        r = client.delete(
            f"/admin/accounts/{a['id']}?confirm_user_count=0",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 409
        assert "does not match" in r.json()["detail"]

        # Matching count → 204.
        r = client.delete(
            f"/admin/accounts/{a['id']}?confirm_user_count=1",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 204

        # Gone.
        assert client.get(
            f"/admin/accounts/{a['id']}", headers=ADMIN_HEADERS
        ).status_code == 404

    def test_delete_unknown_404(self, client):
        r = client.delete(
            "/admin/accounts/no-such?confirm_user_count=0",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 404


class TestMemberships:
    def test_invite_existing_user_creates_active_membership(self, client):
        admin = _mk_user(client, "owner@x.com")
        a = _mk_account(client, "T1", admin["id"])
        bob = _mk_user(client, "bob@x.com")

        r = client.post(
            f"/admin/accounts/{a['id']}/members",
            json={"email": "bob@x.com", "role": "contributor"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["pending"] is False
        assert body["role"] == "contributor"
        assert body["user_id"] == bob["id"]

    def test_invite_unknown_email_creates_pending(self, client):
        admin = _mk_user(client, "owner@x.com")
        a = _mk_account(client, "T2", admin["id"])

        r = client.post(
            f"/admin/accounts/{a['id']}/members",
            json={"email": "Future@X.com", "role": "viewer"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["pending"] is True
        assert body["email"] == "future@x.com"  # normalized
        assert body["role"] == "viewer"

    def test_list_members_merges_active_and_pending(self, client):
        admin = _mk_user(client, "owner@x.com")
        a = _mk_account(client, "T3", admin["id"])
        _mk_user(client, "alice@x.com")
        client.post(
            f"/admin/accounts/{a['id']}/members",
            json={"email": "alice@x.com", "role": "contributor"},
            headers=ADMIN_HEADERS,
        )
        client.post(
            f"/admin/accounts/{a['id']}/members",
            json={"email": "future@x.com", "role": "viewer"},
            headers=ADMIN_HEADERS,
        )
        r = client.get(
            f"/admin/accounts/{a['id']}/members", headers=ADMIN_HEADERS
        )
        assert r.status_code == 200
        rows = r.json()
        emails = {row["email"]: row["pending"] for row in rows}
        assert emails["owner@x.com"] is False
        assert emails["alice@x.com"] is False
        assert emails["future@x.com"] is True

    def test_invite_invalid_role_422(self, client):
        admin = _mk_user(client, "owner@x.com")
        a = _mk_account(client, "T4", admin["id"])
        r = client.post(
            f"/admin/accounts/{a['id']}/members",
            json={"email": "bob@x.com", "role": "king"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 422

    def test_update_role_and_last_admin_guard(self, client):
        admin = _mk_user(client, "owner@x.com")
        a = _mk_account(client, "T5", admin["id"])

        # Demoting the lone admin → 409 (last-admin guard).
        r = client.put(
            f"/admin/accounts/{a['id']}/members/{admin['id']}",
            json={"role": "viewer"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 409
        assert "last account-admin" in r.json()["detail"]

        # Promote another user to admin, then demote the original.
        alice = _mk_user(client, "alice@x.com")
        client.post(
            f"/admin/accounts/{a['id']}/members",
            json={"email": "alice@x.com", "role": "contributor"},
            headers=ADMIN_HEADERS,
        )
        r = client.put(
            f"/admin/accounts/{a['id']}/members/{alice['id']}",
            json={"role": "account-admin"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200
        r = client.put(
            f"/admin/accounts/{a['id']}/members/{admin['id']}",
            json={"role": "viewer"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200

    def test_remove_member_last_admin_guard(self, client):
        admin = _mk_user(client, "owner@x.com")
        a = _mk_account(client, "T6", admin["id"])
        r = client.delete(
            f"/admin/accounts/{a['id']}/members/{admin['id']}",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 409

    def test_remove_member_with_bad_new_owner(self, client):
        admin = _mk_user(client, "owner@x.com")
        a = _mk_account(client, "T7", admin["id"])
        bob = _mk_user(client, "bob@x.com")
        client.post(
            f"/admin/accounts/{a['id']}/members",
            json={"email": "bob@x.com", "role": "contributor"},
            headers=ADMIN_HEADERS,
        )
        # new_owner_id isn't a member of this account → 400.
        stranger = _mk_user(client, "stranger@x.com")
        r = client.delete(
            f"/admin/accounts/{a['id']}/members/{bob['id']}"
            f"?new_owner_id={stranger['id']}",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 400


class TestPendingInvites:
    def test_revoke_pending(self, client):
        admin = _mk_user(client, "owner@x.com")
        a = _mk_account(client, "P1", admin["id"])
        r = client.post(
            f"/admin/accounts/{a['id']}/members",
            json={"email": "future@x.com", "role": "viewer"},
            headers=ADMIN_HEADERS,
        )
        pending_id = r.json()["id"]

        r = client.delete(
            f"/admin/accounts/{a['id']}/pending/{pending_id}",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 204

        # Now truly gone.
        r = client.delete(
            f"/admin/accounts/{a['id']}/pending/{pending_id}",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 404

    def test_cannot_revoke_cross_account(self, client):
        """Pending IDs are cross-account global, but revoke must
        match the pending row's account — defense for the Wave 9.5
        session-scoped admin-admin case."""
        a1_admin = _mk_user(client, "a1@x.com")
        a1 = _mk_account(client, "A1", a1_admin["id"])
        a2_admin = _mk_user(client, "a2@x.com")
        a2 = _mk_account(client, "A2", a2_admin["id"])

        r = client.post(
            f"/admin/accounts/{a1['id']}/members",
            json={"email": "pending@x.com", "role": "viewer"},
            headers=ADMIN_HEADERS,
        )
        pid = r.json()["id"]

        # Try to revoke from the wrong account → 404.
        r = client.delete(
            f"/admin/accounts/{a2['id']}/pending/{pid}",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 404
