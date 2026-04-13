"""Liveness / readiness probe tests (delivery.md §4 routes)."""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.exc import OperationalError



class TestLivenessProbes:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_livez_ok(self, client):
        r = client.get("/livez")
        assert r.status_code == 200
        assert r.json() == {"status": "alive"}


class TestReadyz:
    def test_readyz_reports_healthy(self, client):
        r = client.get("/readyz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ready"
        assert body["components"]["settings"] == "ok"
        assert body["components"]["db"] == "ok"

    def test_readyz_reports_db_failure(self, client):
        """If the DB round-trip explodes, /readyz returns 503 with the
        failure reason broken out per-component."""
        from mcp_server.dependencies import get_db

        class _BadSession:
            def execute(self, *_a, **_kw):
                raise OperationalError("probe", {}, Exception("boom"))

            def close(self):  # pragma: no cover - trivial
                return None

        def _bad_get_db(request: Request):
            yield _BadSession()

        app = client.app
        app.dependency_overrides[get_db] = _bad_get_db
        try:
            r = client.get("/readyz")
        finally:
            app.dependency_overrides.pop(get_db, None)

        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "not_ready"
        assert body["components"]["settings"] == "ok"
        assert body["components"]["db"].startswith("fail: OperationalError")

    def test_readyz_reports_settings_failure(self, client, monkeypatch):
        """When settings construction raises, the probe calls it out
        instead of crashing."""
        from mcp_server.routers import health as health_mod

        def _boom():
            raise RuntimeError("settings broken")

        monkeypatch.setattr(health_mod, "get_settings", _boom, raising=False)
        # The router imports get_settings at call-time; patch where it
        # lives.
        import mcp_server.config as config_mod

        monkeypatch.setattr(config_mod, "get_settings", _boom)
        r = client.get("/readyz")
        assert r.status_code == 503
        body = r.json()
        assert body["components"]["settings"].startswith("fail: RuntimeError")
