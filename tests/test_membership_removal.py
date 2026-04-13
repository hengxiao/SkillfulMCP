"""Wave 9.6 — removal preview + auto-promotion on reassignment.

Exercises both the service layer (`describe_removal`,
`remove_membership(new_owner_id=)`) and the HTTP surface (`GET
/admin/accounts/{id}/members/{user_id}/removal-preview`, `DELETE
.../members/{user_id}?new_owner_id=...`).
"""

from __future__ import annotations

from mcp_server import accounts as acct_svc
from mcp_server import catalog as cat_svc
from mcp_server import registry
from mcp_server.schemas import AgentCreate, SkillCreate, SkillsetCreate
from mcp_server import users as user_svc
from mcp_server.pwhash import hash_password

from tests.conftest import ADMIN_HEADERS


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------

def _mk(db_session):
    alice = user_svc.create_user(
        db_session, email="alice@x.com", password_hash=hash_password("p")
    )
    bob = user_svc.create_user(
        db_session, email="bob@x.com", password_hash=hash_password("p")
    )
    acct = acct_svc.create_account(
        db_session, name="Team", initial_admin_user_id=alice.id
    )
    acct_svc.add_membership(
        db_session, account_id=acct.id, user_id=bob.id, role="viewer"
    )
    return alice, bob, acct


class TestDescribeRemoval:
    def test_lists_owned_counts_and_candidates(self, db_session):
        alice, bob, acct = _mk(db_session)
        # Alice owns 2 skills + 1 skillset.
        cat_svc.create_skill(
            db_session,
            SkillCreate(id="s1", name="S1", version="1.0.0",
                        account_id=acct.id, owner_user_id=alice.id),
        )
        cat_svc.create_skill(
            db_session,
            SkillCreate(id="s2", name="S2", version="1.0.0",
                        account_id=acct.id, owner_user_id=alice.id),
        )
        cat_svc.create_skillset(
            db_session,
            SkillsetCreate(id="ss1", name="SS1",
                           account_id=acct.id, owner_user_id=alice.id),
        )
        registry.create_agent(
            db_session,
            AgentCreate(id="ag1", name="Ag1", scope=["read"],
                        account_id=acct.id, owner_user_id=alice.id),
        )

        preview = acct_svc.describe_removal(
            db_session, account_id=acct.id, user_id=alice.id
        )
        assert preview["owns_skills"] == 2
        assert preview["owns_skillsets"] == 1
        assert preview["owns_agents"] == 1
        # default_target is ... none, because Alice is the only
        # admin and the dropdown excludes her.
        assert preview["default_target"] is None
        # Dropdown lists Bob only.
        assert len(preview["target_members"]) == 1
        assert preview["target_members"][0]["email"] == "bob@x.com"
        assert preview["target_members"][0]["role"] == "viewer"


class TestAutoPromotionOnReassign:
    def test_viewer_target_is_promoted_to_contributor(self, db_session):
        alice, bob, acct = _mk(db_session)
        # Alice owns a skill.
        cat_svc.create_skill(
            db_session,
            SkillCreate(id="s1", name="S1", version="1.0.0",
                        account_id=acct.id, owner_user_id=alice.id),
        )
        # Promote someone else to admin first so Alice can be removed
        # without tripping the last-admin guard.
        alice2 = user_svc.create_user(
            db_session, email="alice2@x.com", password_hash=hash_password("p")
        )
        acct_svc.add_membership(
            db_session, account_id=acct.id, user_id=alice2.id,
            role="account-admin",
        )

        # Reassign from Alice → Bob (viewer). Should auto-promote
        # Bob to contributor.
        acct_svc.remove_membership(
            db_session, account_id=acct.id, user_id=alice.id,
            new_owner_id=bob.id,
        )
        bob_mem = acct_svc.get_membership(
            db_session, account_id=acct.id, user_id=bob.id
        )
        assert bob_mem.role == "contributor"
        # Skill owner_user_id is now Bob.
        from mcp_server.models import Skill
        s = db_session.query(Skill).filter_by(id="s1").first()
        assert s.owner_user_id == bob.id

    def test_no_transfer_no_promotion(self, db_session):
        """If the departing user owns nothing, we don't touch the
        target's role."""
        alice, bob, acct = _mk(db_session)
        alice2 = user_svc.create_user(
            db_session, email="a2@x.com", password_hash=hash_password("p")
        )
        acct_svc.add_membership(
            db_session, account_id=acct.id, user_id=alice2.id,
            role="account-admin",
        )
        acct_svc.remove_membership(
            db_session, account_id=acct.id, user_id=alice.id,
            new_owner_id=bob.id,  # Bob is viewer; no promotion needed
        )
        bob_mem = acct_svc.get_membership(
            db_session, account_id=acct.id, user_id=bob.id
        )
        # Stayed a viewer since nothing was transferred.
        assert bob_mem.role == "viewer"

    def test_contributor_target_stays_contributor(self, db_session):
        """No demotion or upgrade when the target is already
        contributor — inheritance needs at least contributor."""
        alice, bob, acct = _mk(db_session)
        acct_svc.update_membership_role(
            db_session, account_id=acct.id, user_id=bob.id,
            new_role="contributor",
        )
        alice2 = user_svc.create_user(
            db_session, email="a2@x.com", password_hash=hash_password("p")
        )
        acct_svc.add_membership(
            db_session, account_id=acct.id, user_id=alice2.id,
            role="account-admin",
        )
        cat_svc.create_skill(
            db_session,
            SkillCreate(id="s1", name="S1", version="1.0.0",
                        account_id=acct.id, owner_user_id=alice.id),
        )
        acct_svc.remove_membership(
            db_session, account_id=acct.id, user_id=alice.id,
            new_owner_id=bob.id,
        )
        bob_mem = acct_svc.get_membership(
            db_session, account_id=acct.id, user_id=bob.id
        )
        assert bob_mem.role == "contributor"


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

class TestRemovalPreviewEndpoint:
    def test_returns_counts_and_targets(self, client):
        # Build a small account.
        alice = client.post("/admin/users",
                            json={"email": "alice@x.com", "password": "s3cret-pass"},
                            headers=ADMIN_HEADERS).json()
        _ = client.post("/admin/users",
                          json={"email": "bob@x.com", "password": "s3cret-pass"},
                          headers=ADMIN_HEADERS).json()
        a = client.post("/admin/accounts",
                        json={"name": "T", "initial_admin_user_id": alice["id"]},
                        headers=ADMIN_HEADERS).json()
        client.post(f"/admin/accounts/{a['id']}/members",
                    json={"email": "bob@x.com", "role": "viewer"},
                    headers=ADMIN_HEADERS)
        client.post("/skills",
                    json={"id": "s1", "name": "S1", "version": "1.0.0",
                          "account_id": a["id"], "owner_user_id": alice["id"]},
                    headers=ADMIN_HEADERS)

        r = client.get(
            f"/admin/accounts/{a['id']}/members/{alice['id']}/removal-preview",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["owns_skills"] == 1
        assert body["owns_skillsets"] == 0
        assert body["owns_agents"] == 0
        assert body["target_members"][0]["email"] == "bob@x.com"
        assert body["target_members"][0]["role"] == "viewer"

    def test_unknown_account_404(self, client):
        r = client.get(
            "/admin/accounts/no-such/members/u1/removal-preview",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 404

    def test_unknown_membership_404(self, client):
        admin = client.post("/admin/users",
                            json={"email": "admin@x.com", "password": "s3cret-pass"},
                            headers=ADMIN_HEADERS).json()
        a = client.post("/admin/accounts",
                        json={"name": "X", "initial_admin_user_id": admin["id"]},
                        headers=ADMIN_HEADERS).json()
        r = client.get(
            f"/admin/accounts/{a['id']}/members/u2/removal-preview",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 404


class TestDeleteWithReassignment:
    def test_delete_with_new_owner_id_promotes_viewer(self, client):
        alice = client.post("/admin/users",
                            json={"email": "al@x.com", "password": "s3cret-pass"},
                            headers=ADMIN_HEADERS).json()
        bob = client.post("/admin/users",
                          json={"email": "bb@x.com", "password": "s3cret-pass"},
                          headers=ADMIN_HEADERS).json()
        a = client.post("/admin/accounts",
                        json={"name": "T2", "initial_admin_user_id": alice["id"]},
                        headers=ADMIN_HEADERS).json()
        client.post(f"/admin/accounts/{a['id']}/members",
                    json={"email": "bb@x.com", "role": "viewer"},
                    headers=ADMIN_HEADERS)
        # Promote a second admin so Alice can leave without
        # tripping the last-admin guard.
        _ = client.post("/admin/users",
                             json={"email": "al2@x.com", "password": "s3cret-pass"},
                             headers=ADMIN_HEADERS).json()
        client.post(f"/admin/accounts/{a['id']}/members",
                    json={"email": "al2@x.com", "role": "account-admin"},
                    headers=ADMIN_HEADERS)

        client.post(
            "/skills",
            json={"id": "sk", "name": "Sk", "version": "1.0.0",
                  "account_id": a["id"], "owner_user_id": alice["id"]},
            headers=ADMIN_HEADERS,
        )

        r = client.delete(
            f"/admin/accounts/{a['id']}/members/{alice['id']}"
            f"?new_owner_id={bob['id']}",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 204

        # Bob promoted.
        members = client.get(
            f"/admin/accounts/{a['id']}/members", headers=ADMIN_HEADERS
        ).json()
        bob_row = next(m for m in members if m.get("email") == "bb@x.com")
        assert bob_row["role"] == "contributor"
