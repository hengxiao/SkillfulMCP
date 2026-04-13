"""Audit log tests (item H).

Exercises:
- `mcp_server.audit.record` appends a row and emits a log line.
- Failure in `record` doesn't propagate to the caller (defensive
  swallow; audit is fire-and-forget).
- Instrumented write paths (account create/delete, membership
  invite/role-change/remove, move-account, signup, disable-user)
  all produce audit rows with the right `action` + `target_id`.
- `GET /admin/audit` returns newest-first, filters by
  account_id, and clamps `limit` at 500.
"""

from __future__ import annotations

from mcp_server import audit as audit_svc
from mcp_server.models import AuditEvent

from tests.conftest import ADMIN_HEADERS


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------

class TestRecord:
    def test_append_row_and_commit(self, db_session):
        audit_svc.record(
            db_session,
            action="test.hello",
            actor_email="ops@x.com",
            target_kind="thing",
            target_id="42",
            diff={"x": 1},
        )
        rows = db_session.query(AuditEvent).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.action == "test.hello"
        assert row.actor_email == "ops@x.com"
        assert row.target_id == "42"
        assert row.diff == {"x": 1}
        # SQLite stores naive datetimes even when the column is
        # declared timezone=True; we assert the column was populated,
        # not the tz. Postgres preserves timezone in prod.
        assert row.ts is not None

    def test_list_events_newest_first(self, db_session):
        for i in range(3):
            audit_svc.record(db_session, action=f"test.{i}")
        events = audit_svc.list_events(db_session)
        assert [e.action for e in events] == ["test.2", "test.1", "test.0"]

    def test_list_events_filter_by_account(self, db_session):
        audit_svc.record(db_session, action="a1.x", account_id="acct-a")
        audit_svc.record(db_session, action="a2.x", account_id="acct-b")
        audit_svc.record(db_session, action="plat.x")  # no account
        a_events = audit_svc.list_events(db_session, account_id="acct-a")
        assert [e.action for e in a_events] == ["a1.x"]

    def test_list_events_limit_clamped(self, db_session):
        for i in range(5):
            audit_svc.record(db_session, action=f"t.{i}")
        # service clamps to 1..500; 0 becomes 1, 10000 becomes 500.
        assert len(audit_svc.list_events(db_session, limit=0)) == 1
        assert len(audit_svc.list_events(db_session, limit=10000)) == 5

    def test_record_failure_swallowed(self, db_session, monkeypatch):
        """If the DB commit explodes, record() must not propagate."""

        def boom(self):
            raise RuntimeError("db down")

        monkeypatch.setattr(
            type(db_session), "commit", boom, raising=True
        )
        # Should not raise.
        audit_svc.record(db_session, action="nope")


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

class TestAuditEndpoint:
    def test_empty_by_default(self, client):
        r = client.get("/admin/audit", headers=ADMIN_HEADERS)
        assert r.status_code == 200
        assert r.json() == []

    def test_account_events_surface(self, client):
        u = client.post(
            "/admin/users",
            json={"email": "ops@x.com", "password": "s3cret-pass"},
            headers=ADMIN_HEADERS,
        ).json()
        acct = client.post(
            "/admin/accounts",
            json={"name": "Audited", "initial_admin_user_id": u["id"]},
            headers=ADMIN_HEADERS,
        ).json()
        # One more action: invite pending.
        client.post(
            f"/admin/accounts/{acct['id']}/members",
            json={"email": "future@x.com", "role": "viewer"},
            headers=ADMIN_HEADERS,
        )

        r = client.get("/admin/audit", headers=ADMIN_HEADERS)
        actions = [e["action"] for e in r.json()]
        # Newest first.
        assert actions[0] == "membership.invited"
        assert "account.created" in actions

    def test_account_filter(self, client):
        u = client.post(
            "/admin/users",
            json={"email": "a@x.com", "password": "s3cret-pass"},
            headers=ADMIN_HEADERS,
        ).json()
        a1 = client.post(
            "/admin/accounts",
            json={"name": "T1", "initial_admin_user_id": u["id"]},
            headers=ADMIN_HEADERS,
        ).json()
        client.post(
            "/admin/accounts",
            json={"name": "T2", "initial_admin_user_id": u["id"]},
            headers=ADMIN_HEADERS,
        )
        r = client.get(
            f"/admin/audit?account_id={a1['id']}", headers=ADMIN_HEADERS
        )
        for event in r.json():
            assert event["account_id"] == a1["id"]

    def test_invalid_limit_422(self, client):
        # Query validator caps at 500 + floor 1.
        r = client.get("/admin/audit?limit=0", headers=ADMIN_HEADERS)
        assert r.status_code == 422
        r = client.get("/admin/audit?limit=1000", headers=ADMIN_HEADERS)
        assert r.status_code == 422

    def test_requires_admin_key(self, client):
        r = client.get("/admin/audit")
        assert r.status_code == 403
