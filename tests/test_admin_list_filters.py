"""Wave 9.3 — `/admin/skills` query-param filters.

Pins the behavior of `?account_id` / `?mine` / `?shared` + their
X-Owner-User-Id / X-Owner-User-Email header companions, plus the
400 errors when the header is missing.
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


def _mk_skill(client, skill_id, *, account_id=None, owner_user_id=None,
              visibility="private"):
    payload = {
        "id": skill_id, "name": skill_id, "version": "1.0.0",
        "visibility": visibility,
    }
    if account_id:
        payload["account_id"] = account_id
    if owner_user_id:
        payload["owner_user_id"] = owner_user_id
    r = client.post("/skills", json=payload, headers=ADMIN_HEADERS)
    assert r.status_code == 201, r.text
    return r.json()


class TestAccountFilter:
    def test_filters_by_account(self, client):
        u = _mk_user(client, "ops@x.com")
        a1 = _mk_account(client, "A1", u["id"])
        a2 = _mk_account(client, "A2", u["id"])
        _mk_skill(client, "in-a1", account_id=a1["id"])
        _mk_skill(client, "in-a2", account_id=a2["id"])

        r = client.get(
            f"/admin/skills?account_id={a1['id']}", headers=ADMIN_HEADERS
        )
        ids = [s["id"] for s in r.json()]
        assert ids == ["in-a1"]


class TestMineFilter:
    def test_filters_by_owner(self, client):
        alice = _mk_user(client, "alice@x.com")
        bob = _mk_user(client, "bob@x.com")
        _mk_skill(client, "alice-owned", owner_user_id=alice["id"])
        _mk_skill(client, "bob-owned", owner_user_id=bob["id"])
        _mk_skill(client, "ownerless")  # no owner

        r = client.get(
            "/admin/skills?mine=1",
            headers={**ADMIN_HEADERS, "X-Owner-User-Id": alice["id"]},
        )
        ids = [s["id"] for s in r.json()]
        assert ids == ["alice-owned"]

    def test_mine_requires_header(self, client):
        r = client.get("/admin/skills?mine=1", headers=ADMIN_HEADERS)
        assert r.status_code == 400
        assert "X-Owner-User-Id" in r.json()["detail"]


class TestSharedFilter:
    def test_filters_by_share_email(self, client):
        _mk_skill(client, "shared-one", visibility="account")
        _mk_skill(client, "shared-two", visibility="account")
        _mk_skill(client, "not-shared", visibility="account")

        # Grant the test email access to two of them.
        for sid in ("shared-one", "shared-two"):
            r = client.post(
                f"/skills/{sid}/shares",
                json={"email": "guest@partner.com"},
                headers=ADMIN_HEADERS,
            )
            assert r.status_code == 201, r.text

        r = client.get(
            "/admin/skills?shared=1",
            headers={
                **ADMIN_HEADERS,
                "X-Owner-User-Email": "Guest@partner.com",  # case normalized
            },
        )
        ids = sorted(s["id"] for s in r.json())
        assert ids == ["shared-one", "shared-two"]

    def test_shared_no_matches_returns_empty(self, client):
        _mk_skill(client, "private-s", visibility="account")
        r = client.get(
            "/admin/skills?shared=1",
            headers={
                **ADMIN_HEADERS,
                "X-Owner-User-Email": "nobody@elsewhere.com",
            },
        )
        assert r.status_code == 200
        assert r.json() == []

    def test_shared_requires_header(self, client):
        r = client.get("/admin/skills?shared=1", headers=ADMIN_HEADERS)
        assert r.status_code == 400


class TestCombined:
    def test_filters_AND_together(self, client):
        alice = _mk_user(client, "alice@x.com")
        a = _mk_account(client, "Team", alice["id"])
        _mk_skill(client, "alice-in-a",
                  account_id=a["id"], owner_user_id=alice["id"])
        _mk_skill(client, "alice-in-default",
                  owner_user_id=alice["id"])  # default account

        r = client.get(
            f"/admin/skills?account_id={a['id']}&mine=1",
            headers={**ADMIN_HEADERS, "X-Owner-User-Id": alice["id"]},
        )
        ids = [s["id"] for s in r.json()]
        assert ids == ["alice-in-a"]


class TestUnfilteredUnchanged:
    def test_no_filter_returns_everything(self, client):
        _mk_skill(client, "a")
        _mk_skill(client, "b")
        r = client.get("/admin/skills", headers=ADMIN_HEADERS)
        ids = sorted(s["id"] for s in r.json())
        assert ids == ["a", "b"]
