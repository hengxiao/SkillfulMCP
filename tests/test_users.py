"""User identity service tests (Wave 9 shape — role-less users).

Moved Wave 8b role-management tests + last-admin guard into
`test_accounts.py`; this file only exercises the identity layer
(create/read/update/delete + bootstrap + /admin/users HTTP surface).
"""

from __future__ import annotations

import json

import pytest

from mcp_server import users as user_svc
from mcp_server.pwhash import hash_password
from tests.conftest import ADMIN_HEADERS


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------

class TestServiceLayer:
    def test_create_and_fetch(self, db_session):
        u = user_svc.create_user(
            db_session,
            email="Alice@Example.COM",
            password_hash=hash_password("hunter22"),
            display_name="  Alice  ",
        )
        # Email is normalized to lowercase; display_name is stripped.
        assert u.email == "alice@example.com"
        assert u.display_name == "Alice"
        assert u.disabled is False
        # Wave 9: users row carries no role column.
        assert not hasattr(u, "role")

        looked_up = user_svc.get_user_by_email(db_session, "ALICE@example.com")
        assert looked_up is not None and looked_up.id == u.id

    def test_duplicate_email_raises(self, db_session):
        user_svc.create_user(
            db_session, email="a@x.com",
            password_hash=hash_password("p"),
        )
        with pytest.raises(ValueError, match="already in use"):
            user_svc.create_user(
                db_session, email="a@x.com",
                password_hash=hash_password("p"),
            )

    def test_reserved_email_rejected(self, db_session):
        """Wave 9: superadmin@skillfulmcp.com cannot be registered."""
        with pytest.raises(ValueError, match="reserved"):
            user_svc.create_user(
                db_session,
                email="superadmin@skillfulmcp.com",
                password_hash=hash_password("p"),
            )

    def test_reserved_email_case_normalized(self, db_session):
        """The reserved-email check runs on the normalized form."""
        with pytest.raises(ValueError, match="reserved"):
            user_svc.create_user(
                db_session,
                email="  SUPERADMIN@SkillfulMCP.com  ",
                password_hash=hash_password("p"),
            )

    def test_update_partial(self, db_session):
        u = user_svc.create_user(
            db_session, email="c@x.com",
            password_hash=hash_password("p"),
        )
        u2 = user_svc.update_user(db_session, u.id, disabled=True)
        assert u2 and u2.disabled is True
        # Display name left alone when not passed.
        assert u2.display_name == u.display_name

    def test_delete(self, db_session):
        u = user_svc.create_user(
            db_session, email="d@x.com",
            password_hash=hash_password("p"),
        )
        assert user_svc.delete_user(db_session, u.id) is True
        assert user_svc.get_user(db_session, u.id) is None
        assert user_svc.delete_user(db_session, u.id) is False


class TestBootstrap:
    def test_noop_when_table_populated(self, db_session, monkeypatch):
        user_svc.create_user(
            db_session, email="pre@x.com",
            password_hash=hash_password("p"),
        )
        monkeypatch.setenv("MCP_WEBUI_OPERATORS", json.dumps(
            [{"email": "new@x.com", "password_hash": hash_password("p")}]
        ))
        created = user_svc.bootstrap_from_env(db_session)
        assert created == 0
        assert user_svc.get_user_by_email(db_session, "new@x.com") is None

    def test_seeds_when_empty(self, db_session, monkeypatch):
        monkeypatch.setenv("MCP_WEBUI_OPERATORS", json.dumps(
            [{"email": "Seed@X.com", "password_hash": hash_password("p"),
              "display_name": "Seed"}]
        ))
        created = user_svc.bootstrap_from_env(db_session)
        assert created == 1
        u = user_svc.get_user_by_email(db_session, "seed@x.com")
        assert u is not None
        # Wave 9: no role column to check.
        assert u.email == "seed@x.com"

    def test_bad_json_is_not_fatal(self, db_session, monkeypatch):
        monkeypatch.setenv("MCP_WEBUI_OPERATORS", "not-json")
        assert user_svc.bootstrap_from_env(db_session) == 0


# ---------------------------------------------------------------------------
# HTTP — /admin/users/*
# ---------------------------------------------------------------------------

class TestAdminUsersHTTP:
    def test_crud_round_trip(self, client):
        # Create.
        r = client.post("/admin/users",
                        json={"email": "ops@x.com", "password": "s3cret-pass",
                              "display_name": "Ops"},
                        headers=ADMIN_HEADERS)
        assert r.status_code == 201, r.text
        user = r.json()
        assert user["email"] == "ops@x.com"
        assert "role" not in user           # Wave 9: no role field
        assert "password_hash" not in user  # never exposed

        # List contains them.
        r = client.get("/admin/users", headers=ADMIN_HEADERS)
        assert r.status_code == 200
        assert any(u["id"] == user["id"] for u in r.json())

        # Update display_name (no role field).
        r = client.put(f"/admin/users/{user['id']}",
                       json={"display_name": "New Name"},
                       headers=ADMIN_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["display_name"] == "New Name"

        # Authenticate.
        r = client.post("/admin/users/authenticate",
                        json={"email": "ops@x.com", "password": "s3cret-pass"},
                        headers=ADMIN_HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == user["id"]
        assert body["is_superadmin"] is False

        # Bad password → 401.
        r = client.post("/admin/users/authenticate",
                        json={"email": "ops@x.com", "password": "wrong"},
                        headers=ADMIN_HEADERS)
        assert r.status_code == 401

        # Delete.
        r = client.delete(f"/admin/users/{user['id']}", headers=ADMIN_HEADERS)
        assert r.status_code == 204

    def test_duplicate_email_409(self, client):
        client.post("/admin/users",
                    json={"email": "dup@x.com", "password": "s3cret-pass"},
                    headers=ADMIN_HEADERS)
        r = client.post("/admin/users",
                        json={"email": "dup@x.com", "password": "s3cret-pass"},
                        headers=ADMIN_HEADERS)
        assert r.status_code == 409

    def test_disabled_user_cannot_authenticate(self, client):
        r = client.post("/admin/users",
                        json={"email": "off@x.com", "password": "s3cret-pass"},
                        headers=ADMIN_HEADERS)
        uid = r.json()["id"]
        client.put(f"/admin/users/{uid}",
                   json={"disabled": True},
                   headers=ADMIN_HEADERS)
        r = client.post("/admin/users/authenticate",
                        json={"email": "off@x.com", "password": "s3cret-pass"},
                        headers=ADMIN_HEADERS)
        assert r.status_code == 401

    def test_requires_admin_key(self, client):
        r = client.get("/admin/users")  # no X-Admin-Key
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Superadmin env-hardcoded auth
# ---------------------------------------------------------------------------

class TestSuperadminAuth:
    def test_env_password_matches(self, client):
        from tests.conftest import SUPERADMIN_TEST_PASSWORD
        r = client.post(
            "/admin/users/authenticate",
            json={"email": "superadmin@skillfulmcp.com",
                  "password": SUPERADMIN_TEST_PASSWORD},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["is_superadmin"] is True
        assert body["id"] == "0"
        assert body["email"] == "superadmin@skillfulmcp.com"

    def test_normalization_blocks_variants(self, client):
        from tests.conftest import SUPERADMIN_TEST_PASSWORD
        r = client.post(
            "/admin/users/authenticate",
            json={"email": "  SUPERADMIN@SkillfulMCP.com  ",
                  "password": SUPERADMIN_TEST_PASSWORD},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["is_superadmin"] is True

    def test_wrong_password_rejected(self, client):
        r = client.post(
            "/admin/users/authenticate",
            json={"email": "superadmin@skillfulmcp.com",
                  "password": "nope"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 401

    def test_superadmin_cannot_register_via_admin_users(self, client):
        r = client.post(
            "/admin/users",
            json={"email": "superadmin@skillfulmcp.com",
                  "password": "s3cret-pass"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 409
        assert "reserved" in r.json()["detail"]
