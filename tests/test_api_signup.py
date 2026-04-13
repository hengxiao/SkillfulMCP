"""HTTP tests for `/admin/signup` and `/admin/users/{id}/disable`
(Wave 9.1).

Signup needs to:
- refuse the reserved superadmin pseudo-email (normalized),
- return 409 on duplicate,
- atomically consume every pending invitation matching the new
  user's email, and
- stamp last_active_account_id to the first consumed account.

Disable needs to flip `users.disabled`, block authenticate(), and
leave the row + its memberships intact.
"""

from __future__ import annotations

from tests.conftest import ADMIN_HEADERS


def _mk_user(client, email):
    r = client.post(
        "/admin/users",
        json={"email": email, "password": "s3cret-pass"},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _mk_account(client, name, admin_id):
    r = client.post(
        "/admin/accounts",
        json={"name": name, "initial_admin_user_id": admin_id},
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 201, r.text
    return r.json()


class TestSignup:
    def test_creates_user_without_invites(self, client):
        r = client.post(
            "/admin/signup",
            json={
                "email": "new@x.com",
                "password": "s3cret-pass",
                "display_name": "New",
            },
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["email"] == "new@x.com"
        assert body["display_name"] == "New"
        assert body["consumed_account_ids"] == []
        # Still logs in.
        auth = client.post(
            "/admin/users/authenticate",
            json={"email": "new@x.com", "password": "s3cret-pass"},
            headers=ADMIN_HEADERS,
        )
        assert auth.status_code == 200

    def test_consumes_pending_invitation(self, client):
        admin = _mk_user(client, "owner@x.com")
        a = _mk_account(client, "Invite Me", admin["id"])
        # Pre-invite an email that doesn't exist yet.
        client.post(
            f"/admin/accounts/{a['id']}/members",
            json={"email": "future@x.com", "role": "contributor"},
            headers=ADMIN_HEADERS,
        )
        # Signup with that email.
        r = client.post(
            "/admin/signup",
            json={"email": "Future@X.com", "password": "s3cret-pass"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201
        body = r.json()
        assert body["consumed_account_ids"] == [a["id"]]
        # Membership is real now.
        members = client.get(
            f"/admin/accounts/{a['id']}/members", headers=ADMIN_HEADERS
        ).json()
        emails = {m["email"]: (m["role"], m["pending"]) for m in members}
        assert emails["future@x.com"] == ("contributor", False)

    def test_reserved_email_rejected_400(self, client):
        r = client.post(
            "/admin/signup",
            json={
                "email": "superadmin@skillfulmcp.com",
                "password": "s3cret-pass",
            },
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 400
        assert "reserved" in r.json()["detail"]

    def test_reserved_email_normalized(self, client):
        r = client.post(
            "/admin/signup",
            json={
                "email": "  SUPERADMIN@SkillfulMCP.com ",
                "password": "s3cret-pass",
            },
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 400
        assert "reserved" in r.json()["detail"]

    def test_duplicate_email_409(self, client):
        client.post(
            "/admin/signup",
            json={"email": "dup@x.com", "password": "s3cret-pass"},
            headers=ADMIN_HEADERS,
        )
        r = client.post(
            "/admin/signup",
            json={"email": "dup@x.com", "password": "s3cret-pass"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 409


class TestDisableUser:
    def test_disable_blocks_login(self, client):
        u = _mk_user(client, "victim@x.com")
        # Can log in at first.
        assert (
            client.post(
                "/admin/users/authenticate",
                json={"email": "victim@x.com", "password": "s3cret-pass"},
                headers=ADMIN_HEADERS,
            ).status_code
            == 200
        )
        # Disable.
        r = client.put(
            f"/admin/users/{u['id']}/disable",
            json={"disabled": True},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200
        # Login blocked.
        r = client.post(
            "/admin/users/authenticate",
            json={"email": "victim@x.com", "password": "s3cret-pass"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 401

    def test_reenable_restores_login(self, client):
        u = _mk_user(client, "back@x.com")
        client.put(
            f"/admin/users/{u['id']}/disable",
            json={"disabled": True},
            headers=ADMIN_HEADERS,
        )
        r = client.put(
            f"/admin/users/{u['id']}/disable",
            json={"disabled": False},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200
        r = client.post(
            "/admin/users/authenticate",
            json={"email": "back@x.com", "password": "s3cret-pass"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200

    def test_unknown_user_404(self, client):
        r = client.put(
            "/admin/users/no-such/disable",
            json={"disabled": True},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 404

    def test_disable_does_not_remove_memberships(self, client):
        """Membership rows are preserved across disable so re-enable
        is a single flip. (The last-admin guard filters out disabled
        admins, so a disabled admin can't satisfy the invariant.)"""
        admin = _mk_user(client, "owner@x.com")
        a = _mk_account(client, "Keep", admin["id"])
        bob = _mk_user(client, "bob@x.com")
        client.post(
            f"/admin/accounts/{a['id']}/members",
            json={"email": "bob@x.com", "role": "contributor"},
            headers=ADMIN_HEADERS,
        )
        client.put(
            f"/admin/users/{bob['id']}/disable",
            json={"disabled": True},
            headers=ADMIN_HEADERS,
        )
        members = client.get(
            f"/admin/accounts/{a['id']}/members", headers=ADMIN_HEADERS
        ).json()
        bob_rows = [m for m in members if m.get("email") == "bob@x.com"]
        assert len(bob_rows) == 1
        assert bob_rows[0]["disabled"] is True
        assert bob_rows[0]["role"] == "contributor"
