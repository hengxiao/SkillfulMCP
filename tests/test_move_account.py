"""Wave 9.x item L — cross-account catalog migration.

POST /admin/{skills|skillsets|agents}/{id}/move-account moves a row
into another account. Shares + ownership snapshots are wiped by
design; skill-version chains move together.
"""

from __future__ import annotations

from tests.conftest import ADMIN_HEADERS


def _mk_user(client, email):
    return client.post(
        "/admin/users",
        json={"email": email, "password": "s3cret-pass"},
        headers=ADMIN_HEADERS,
    ).json()


def _mk_account(client, name, admin_id):
    return client.post(
        "/admin/accounts",
        json={"name": name, "initial_admin_user_id": admin_id},
        headers=ADMIN_HEADERS,
    ).json()


class TestMoveSkill:
    def test_moves_all_versions_and_wipes_shares(self, client):
        u = _mk_user(client, "ops@x.com")
        src = _mk_account(client, "Src", u["id"])
        dst = _mk_account(client, "Dst", u["id"])

        # Create three versions of the same skill in the src account.
        for v in ("1.0.0", "1.1.0", "2.0.0"):
            client.post(
                "/skills",
                json={
                    "id": "shared-skill", "name": "Shared", "version": v,
                    "visibility": "account", "account_id": src["id"],
                    "owner_user_id": u["id"],
                },
                headers=ADMIN_HEADERS,
            )
        # Add a share.
        client.post(
            "/skills/shared-skill/shares",
            json={"email": "guest@partner.com"},
            headers=ADMIN_HEADERS,
        )

        # Move.
        r = client.post(
            "/admin/skills/shared-skill/move-account",
            json={"target_account_id": dst["id"]},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["versions_moved"] == 3
        assert body["shares_wiped"] == 1

        # All versions now in dst.
        versions = client.get(
            "/admin/skills/shared-skill/versions", headers=ADMIN_HEADERS
        ).json()
        assert len(versions) == 3
        # Latest row reflects the new account + cleared owner.
        latest = client.get(
            "/admin/skills/shared-skill", headers=ADMIN_HEADERS
        ).json()
        assert latest["account_id"] == dst["id"]
        assert latest["owner_user_id"] is None

        # Shares list is empty.
        assert client.get(
            "/skills/shared-skill/shares", headers=ADMIN_HEADERS
        ).json() == []

    def test_unknown_target_400(self, client):
        client.post(
            "/skills",
            json={"id": "s1", "name": "S1", "version": "1.0.0"},
            headers=ADMIN_HEADERS,
        )
        r = client.post(
            "/admin/skills/s1/move-account",
            json={"target_account_id": "ghost-account"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 400

    def test_unknown_skill_404(self, client):
        u = _mk_user(client, "x@x.com")
        dst = _mk_account(client, "Dst", u["id"])
        r = client.post(
            "/admin/skills/no-such/move-account",
            json={"target_account_id": dst["id"]},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 404


class TestMoveSkillset:
    def test_moves_and_wipes_shares(self, client):
        u = _mk_user(client, "ops@x.com")
        src = _mk_account(client, "Src", u["id"])
        dst = _mk_account(client, "Dst", u["id"])
        client.post(
            "/skillsets",
            json={"id": "ss1", "name": "SS1", "visibility": "account",
                  "account_id": src["id"]},
            headers=ADMIN_HEADERS,
        )
        client.post(
            "/skillsets/ss1/shares",
            json={"email": "guest@partner.com"},
            headers=ADMIN_HEADERS,
        )

        r = client.post(
            "/admin/skillsets/ss1/move-account",
            json={"target_account_id": dst["id"]},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["shares_wiped"] == 1

        assert client.get(
            "/skillsets/ss1/shares", headers=ADMIN_HEADERS
        ).json() == []

    def test_404_on_missing(self, client):
        u = _mk_user(client, "x@x.com")
        dst = _mk_account(client, "Dst", u["id"])
        r = client.post(
            "/admin/skillsets/nope/move-account",
            json={"target_account_id": dst["id"]},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 404


class TestMoveAgent:
    def test_moves_and_clears_owner(self, client):
        u = _mk_user(client, "ops@x.com")
        src = _mk_account(client, "Src", u["id"])
        dst = _mk_account(client, "Dst", u["id"])

        client.post(
            "/agents",
            json={"id": "a1", "name": "A1", "scope": ["read"],
                  "account_id": src["id"], "owner_user_id": u["id"]},
            headers=ADMIN_HEADERS,
        )

        r = client.post(
            "/admin/agents/a1/move-account",
            json={"target_account_id": dst["id"]},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200
        # Post-move fetch shows the new account + cleared owner.
        agent = client.get("/agents/a1", headers=ADMIN_HEADERS).json()
        assert agent["account_id"] == dst["id"]
        assert agent["owner_user_id"] is None

    def test_404_on_missing(self, client):
        u = _mk_user(client, "x@x.com")
        dst = _mk_account(client, "Dst", u["id"])
        r = client.post(
            "/admin/agents/no-such/move-account",
            json={"target_account_id": dst["id"]},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 404
