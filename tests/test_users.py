"""
Wave 8b — user-management tests.

Three layers:
- Service layer (`mcp_server/users.py`) — CRUD + bootstrap.
- HTTP admin endpoints (`/admin/users/*`).
- End-to-end /admin/users/authenticate against a freshly seeded user.
"""

from __future__ import annotations

import json
import os

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
            role="admin",
            display_name="  Alice  ",
        )
        # Email is normalized to lowercase; display_name is stripped.
        assert u.email == "alice@example.com"
        assert u.display_name == "Alice"
        assert u.role == "admin"
        assert u.disabled is False

        looked_up = user_svc.get_user_by_email(db_session, "ALICE@example.com")
        assert looked_up is not None and looked_up.id == u.id

    def test_duplicate_email_raises(self, db_session):
        user_svc.create_user(
            db_session, email="a@x.com",
            password_hash=hash_password("p"), role="viewer",
        )
        with pytest.raises(ValueError, match="already in use"):
            user_svc.create_user(
                db_session, email="a@x.com",
                password_hash=hash_password("p"), role="viewer",
            )

    def test_invalid_role_rejected(self, db_session):
        with pytest.raises(ValueError, match="role must be one of"):
            user_svc.create_user(
                db_session, email="b@x.com",
                password_hash=hash_password("p"), role="superuser",
            )

    def test_update_partial(self, db_session):
        u = user_svc.create_user(
            db_session, email="c@x.com",
            password_hash=hash_password("p"), role="viewer",
        )
        u2 = user_svc.update_user(db_session, u.id, disabled=True, role="admin")
        assert u2 and u2.disabled is True and u2.role == "admin"
        # Display name left alone when not passed.
        assert u2.display_name == u.display_name

    def test_delete(self, db_session):
        u = user_svc.create_user(
            db_session, email="d@x.com",
            password_hash=hash_password("p"), role="viewer",
        )
        assert user_svc.delete_user(db_session, u.id) is True
        assert user_svc.get_user(db_session, u.id) is None
        assert user_svc.delete_user(db_session, u.id) is False


class TestBootstrap:
    def test_noop_when_table_populated(self, db_session, monkeypatch):
        user_svc.create_user(
            db_session, email="pre@x.com",
            password_hash=hash_password("p"), role="admin",
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
        assert u is not None and u.role == "admin"

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
                              "role": "admin", "display_name": "Ops"},
                        headers=ADMIN_HEADERS)
        assert r.status_code == 201, r.text
        user = r.json()
        assert user["email"] == "ops@x.com"
        assert user["role"] == "admin"
        assert "password_hash" not in user  # never exposed

        # List contains them.
        r = client.get("/admin/users", headers=ADMIN_HEADERS)
        assert r.status_code == 200
        assert any(u["id"] == user["id"] for u in r.json())

        # Update.
        r = client.put(f"/admin/users/{user['id']}",
                       json={"role": "viewer", "disabled": False},
                       headers=ADMIN_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "viewer"

        # Authenticate (via admin-key dep — internal Web UI uses this).
        r = client.post("/admin/users/authenticate",
                        json={"email": "ops@x.com", "password": "s3cret-pass"},
                        headers=ADMIN_HEADERS)
        assert r.status_code == 200
        assert r.json()["id"] == user["id"]

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
                    json={"email": "dup@x.com", "password": "s3cret-pass",
                          "role": "viewer"},
                    headers=ADMIN_HEADERS)
        r = client.post("/admin/users",
                        json={"email": "dup@x.com", "password": "s3cret-pass",
                              "role": "viewer"},
                        headers=ADMIN_HEADERS)
        assert r.status_code == 409

    def test_cannot_delete_last_admin(self, client):
        # Create a second admin so we can delete all the bootstrap ones
        # without the guard tripping yet.
        r = client.post("/admin/users",
                        json={"email": "only@x.com", "password": "s3cret-pass",
                              "role": "admin"},
                        headers=ADMIN_HEADERS)
        uid = r.json()["id"]
        for u in client.get("/admin/users", headers=ADMIN_HEADERS).json():
            if u["role"] == "admin" and u["id"] != uid:
                client.delete(f"/admin/users/{u['id']}", headers=ADMIN_HEADERS)
        # Only one admin left; deleting must 409.
        r = client.delete(f"/admin/users/{uid}", headers=ADMIN_HEADERS)
        assert r.status_code == 409
        assert "last remaining" in r.json()["detail"]

    def test_disabled_user_cannot_authenticate(self, client):
        r = client.post("/admin/users",
                        json={"email": "off@x.com", "password": "s3cret-pass",
                              "role": "viewer"},
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
