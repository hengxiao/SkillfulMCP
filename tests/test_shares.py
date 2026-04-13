"""Wave 9.4 — email allow-list tests.

Service layer:
- email normalization + regex validation
- public resource → share rejected
- unknown resource → ShareError
- duplicate → ShareError
- list + delete

HTTP:
- POST / GET / DELETE /skills/{id}/shares round-trip
- 409 on duplicate, 400 on bad email, 404 on missing resource
- cross-skill share_id protection on DELETE

Authorization:
- can_read wired to skill_share_exists: account-tier allow list
  grants cross-account read, private-tier + membership-less email
  remains denied.
"""

from __future__ import annotations

import pytest

from mcp_server import authorization
from mcp_server import shares as share_svc
from mcp_server.schemas import SkillCreate, SkillsetCreate
from mcp_server import catalog as cat_svc

from tests.conftest import ADMIN_HEADERS


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------

def _make_skill(db, skill_id="s1", visibility="account") -> None:
    cat_svc.create_skill(
        db,
        SkillCreate(
            id=skill_id, name=skill_id, version="1.0.0",
            visibility=visibility,
        ),
    )


def _make_skillset(db, skillset_id="ss1", visibility="account") -> None:
    cat_svc.create_skillset(
        db,
        SkillsetCreate(
            id=skillset_id, name=skillset_id, visibility=visibility,
        ),
    )


class TestSkillShareService:
    def test_round_trip(self, db_session):
        _make_skill(db_session)
        s = share_svc.add_skill_share(
            db_session, skill_id="s1", email="Alice@Example.com"
        )
        assert s.email == "alice@example.com"  # normalized
        rows = share_svc.list_skill_shares(db_session, "s1")
        assert len(rows) == 1 and rows[0].id == s.id

    def test_missing_skill_rejected(self, db_session):
        with pytest.raises(share_svc.ShareError, match="does not exist"):
            share_svc.add_skill_share(
                db_session, skill_id="ghost", email="a@b.com"
            )

    def test_public_resource_rejected(self, db_session):
        _make_skill(db_session, visibility="public")
        with pytest.raises(share_svc.ShareError, match="world-readable"):
            share_svc.add_skill_share(
                db_session, skill_id="s1", email="a@b.com"
            )

    def test_duplicate_rejected(self, db_session):
        _make_skill(db_session)
        share_svc.add_skill_share(
            db_session, skill_id="s1", email="dup@x.com"
        )
        with pytest.raises(share_svc.ShareError, match="already shared"):
            share_svc.add_skill_share(
                db_session, skill_id="s1", email="dup@x.com"
            )

    def test_invalid_email_rejected(self, db_session):
        _make_skill(db_session)
        with pytest.raises(share_svc.ShareError, match="not a valid address"):
            share_svc.add_skill_share(
                db_session, skill_id="s1", email="not-an-email"
            )

    def test_empty_email_rejected(self, db_session):
        _make_skill(db_session)
        with pytest.raises(share_svc.ShareError, match="required"):
            share_svc.add_skill_share(
                db_session, skill_id="s1", email="   "
            )

    def test_delete(self, db_session):
        _make_skill(db_session)
        s = share_svc.add_skill_share(
            db_session, skill_id="s1", email="a@b.com"
        )
        assert share_svc.delete_skill_share(db_session, s.id) is True
        assert share_svc.delete_skill_share(db_session, s.id) is False


class TestSkillsetShareService:
    def test_round_trip(self, db_session):
        _make_skillset(db_session)
        s = share_svc.add_skillset_share(
            db_session, skillset_id="ss1", email="c@d.com"
        )
        assert s.email == "c@d.com"
        assert len(share_svc.list_skillset_shares(db_session, "ss1")) == 1

    def test_missing_skillset_rejected(self, db_session):
        with pytest.raises(share_svc.ShareError, match="does not exist"):
            share_svc.add_skillset_share(
                db_session, skillset_id="no", email="a@b.com"
            )

    def test_public_skillset_rejected(self, db_session):
        _make_skillset(db_session, visibility="public")
        with pytest.raises(share_svc.ShareError, match="world-readable"):
            share_svc.add_skillset_share(
                db_session, skillset_id="ss1", email="a@b.com"
            )


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

def _mk_skill(client, skill_id="s1", visibility="account"):
    r = client.post(
        "/skills",
        json={
            "id": skill_id, "name": skill_id, "version": "1.0.0",
            "visibility": visibility,
        },
        headers=ADMIN_HEADERS,
    )
    assert r.status_code == 201, r.text


class TestSkillShareHTTP:
    def test_crud_round_trip(self, client):
        _mk_skill(client)
        r = client.post(
            "/skills/s1/shares",
            json={"email": "alice@customer.com"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201, r.text
        share_id = r.json()["id"]

        r = client.get("/skills/s1/shares", headers=ADMIN_HEADERS)
        emails = [s["email"] for s in r.json()]
        assert emails == ["alice@customer.com"]

        r = client.delete(
            f"/skills/s1/shares/{share_id}", headers=ADMIN_HEADERS
        )
        assert r.status_code == 204

    def test_duplicate_409(self, client):
        _mk_skill(client)
        client.post("/skills/s1/shares", json={"email": "a@b.com"},
                    headers=ADMIN_HEADERS)
        r = client.post("/skills/s1/shares", json={"email": "a@b.com"},
                        headers=ADMIN_HEADERS)
        assert r.status_code == 409

    def test_bad_email_400(self, client):
        _mk_skill(client)
        r = client.post(
            "/skills/s1/shares", json={"email": "not-valid"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 400

    def test_unknown_skill_400(self, client):
        r = client.post(
            "/skills/ghost/shares", json={"email": "a@b.com"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 400
        assert "does not exist" in r.json()["detail"]

    def test_public_skill_rejected(self, client):
        _mk_skill(client, visibility="public")
        r = client.post(
            "/skills/s1/shares", json={"email": "a@b.com"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 400
        assert "world-readable" in r.json()["detail"]

    def test_cross_skill_delete_404(self, client):
        """A share created under skill A must not be deletable under
        skill B's path."""
        _mk_skill(client, skill_id="a")
        _mk_skill(client, skill_id="b")
        r = client.post("/skills/a/shares", json={"email": "x@y.com"},
                        headers=ADMIN_HEADERS)
        sid = r.json()["id"]
        r = client.delete(f"/skills/b/shares/{sid}", headers=ADMIN_HEADERS)
        assert r.status_code == 404


class TestSkillsetShareHTTP:
    def test_crud_round_trip(self, client):
        client.post(
            "/skillsets",
            json={"id": "ss1", "name": "SS1", "visibility": "account"},
            headers=ADMIN_HEADERS,
        )
        r = client.post(
            "/skillsets/ss1/shares",
            json={"email": "alice@customer.com"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201, r.text
        r = client.get("/skillsets/ss1/shares", headers=ADMIN_HEADERS)
        assert r.status_code == 200
        assert r.json()[0]["email"] == "alice@customer.com"


# ---------------------------------------------------------------------------
# can_read integration via share_exists_fn
# ---------------------------------------------------------------------------

class TestCanReadWithShares:
    def test_account_tier_share_grants_outsider(self, db_session):
        _make_skill(db_session, visibility="account")
        share_svc.add_skill_share(
            db_session, skill_id="s1", email="outsider@partner.com"
        )
        skill = db_session.query(cat_svc.Skill).filter_by(id="s1").first()

        def share_fn(resource, email):
            return share_svc.skill_share_exists(db_session, resource, email)

        # Outsider with no account membership but on the allow list
        # sees the account-tier resource.
        assert (
            authorization.can_read(
                skill,
                user_id="u1",
                user_email="outsider@partner.com",
                user_memberships={},
                share_exists_fn=share_fn,
            )
            is True
        )

    def test_private_requires_membership_even_with_share(self, db_session):
        _make_skill(db_session, visibility="private")
        share_svc.add_skill_share(
            db_session, skill_id="s1", email="outsider@partner.com"
        )
        skill = db_session.query(cat_svc.Skill).filter_by(id="s1").first()

        def share_fn(resource, email):
            return share_svc.skill_share_exists(db_session, resource, email)

        # Outsider on allow list but no membership → denied for private.
        assert (
            authorization.can_read(
                skill,
                user_id="outsider",
                user_email="outsider@partner.com",
                user_memberships={"some-other-account": "viewer"},
                share_exists_fn=share_fn,
            )
            is False
        )
