"""Exception-handler envelope tests (delivery.md §1 — structured errors).

test_observability.py already exercises the happy-path envelope for
4xx. This file fills in the unhandled-exception path and pins the
X-Request-ID round-trip on every error class.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mcp_server.main import create_app
from mcp_server.middleware import HEADER as REQUEST_ID_HEADER


@pytest.fixture()
def error_app():
    """Fresh app with extra routes that raise on demand."""
    app = create_app(database_url="sqlite:///:memory:")

    @app.get("/_fail/unhandled")
    def _unhandled():
        raise RuntimeError("oops, internal")

    @app.get("/_fail/http/{code}")
    def _http(code: int):
        raise HTTPException(status_code=code, detail=f"custom-{code}")

    # raise_server_exceptions=False lets the registered
    # unhandled_exception_handler run instead of the TestClient
    # re-raising the original RuntimeError.
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


class TestUnhandledException:
    def test_returns_500_with_generic_detail(self, error_app):
        r = error_app.get("/_fail/unhandled")
        assert r.status_code == 500
        body = r.json()
        # The internal exception text is NOT leaked.
        assert body["detail"] == "Internal Server Error"
        assert body["code"] == "INTERNAL_ERROR"
        assert body["request_id"] == r.headers[REQUEST_ID_HEADER]
        assert "oops" not in body["detail"]


class TestHTTPExceptionEnvelope:
    def test_code_derived_from_status(self, error_app):
        r = error_app.get("/_fail/http/404")
        body = r.json()
        assert r.status_code == 404
        assert body["detail"] == "custom-404"
        assert body["code"] == "HTTP_404"
        assert body["request_id"] == r.headers[REQUEST_ID_HEADER]

    def test_codes_for_403_preserved(self, error_app):
        r = error_app.get("/_fail/http/403")
        assert r.status_code == 403
        assert r.json()["code"] == "HTTP_403"


class TestValidationEnvelope:
    def test_code_is_validation_error(self, client):
        # Sending empty JSON to /token triggers Pydantic validation.
        r = client.post("/token", json={}, headers={"X-Admin-Key": "test-admin-key"})
        assert r.status_code == 422
        body = r.json()
        assert body["code"] == "VALIDATION_ERROR"
        # detail is a list-of-errors shape preserved by jsonable_encoder.
        assert isinstance(body["detail"], list)
        assert body["request_id"] == r.headers[REQUEST_ID_HEADER]


class TestRequestIdRoundTrip:
    def test_inbound_id_echoed_on_error(self, error_app):
        r = error_app.get(
            "/_fail/http/400",
            headers={"X-Request-Id": "caller-provided-id"},
        )
        assert r.status_code == 400
        assert r.headers[REQUEST_ID_HEADER] == "caller-provided-id"
        assert r.json()["request_id"] == "caller-provided-id"

    def test_server_generated_id_when_absent(self, error_app):
        r = error_app.get("/_fail/http/400")
        rid = r.headers[REQUEST_ID_HEADER]
        assert rid and len(rid) >= 16  # uuid hex prefix; 32 chars in practice
        assert r.json()["request_id"] == rid
