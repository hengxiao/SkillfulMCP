"""Bundle signing tests (item J).

Unit: canonical digest, sign + verify round-trip.
HTTP: attach + clear signature, `verified` surfaces on
`GET /admin/skills/{id}` when the kid is in the trust store.
"""

from __future__ import annotations

import base64
import io
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_server import bundle_signing
from mcp_server.bundles import BundleFile, store_bundle
from mcp_server.schemas import SkillCreate
from mcp_server import catalog as cat_svc

from tests.conftest import ADMIN_HEADERS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("utf-8")


def _gen_keypair() -> tuple[str, str]:
    """Return (private_b64, public_b64) as url-safe base64 strings."""
    private = Ed25519PrivateKey.generate()
    raw_priv = private.private_bytes_raw()
    raw_pub = private.public_key().public_bytes_raw()
    return _b64u(raw_priv), _b64u(raw_pub)


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------

class TestDigest:
    def test_digest_is_stable_for_same_contents(self, db_session):
        s = cat_svc.create_skill(
            db_session,
            SkillCreate(id="d1", name="D1", version="1.0.0"),
        )
        store_bundle(db_session, s.pk, [
            BundleFile("SKILL.md", b"one"),
            BundleFile("a/b.txt", b"two"),
        ])
        d1 = bundle_signing.compute_bundle_digest(db_session, s.pk)

        # Rebuild bundle in a different insert order → same digest.
        store_bundle(db_session, s.pk, [
            BundleFile("a/b.txt", b"two"),
            BundleFile("SKILL.md", b"one"),
        ])
        d2 = bundle_signing.compute_bundle_digest(db_session, s.pk)
        assert d1 == d2

    def test_digest_changes_on_content_change(self, db_session):
        s = cat_svc.create_skill(
            db_session,
            SkillCreate(id="d2", name="D2", version="1.0.0"),
        )
        store_bundle(db_session, s.pk, [BundleFile("f", b"a")])
        d1 = bundle_signing.compute_bundle_digest(db_session, s.pk)
        store_bundle(db_session, s.pk, [BundleFile("f", b"b")])
        d2 = bundle_signing.compute_bundle_digest(db_session, s.pk)
        assert d1 != d2


# ---------------------------------------------------------------------------
# sign_digest / verify_signature
# ---------------------------------------------------------------------------

class TestSignVerify:
    def test_round_trip(self, monkeypatch, db_session):
        priv_b64, pub_b64 = _gen_keypair()
        monkeypatch.setenv(
            "MCP_BUNDLE_SIGNING_PUBLIC_KEYS",
            json.dumps({"author-1": pub_b64}),
        )
        digest = b"\x00" * 32
        sig = bundle_signing.sign_digest(digest, priv_b64)
        assert bundle_signing.verify_signature(digest, sig, "author-1")

    def test_unknown_kid_rejected(self, monkeypatch):
        priv_b64, pub_b64 = _gen_keypair()
        monkeypatch.setenv(
            "MCP_BUNDLE_SIGNING_PUBLIC_KEYS",
            json.dumps({"known": pub_b64}),
        )
        digest = b"d" * 32
        sig = bundle_signing.sign_digest(digest, priv_b64)
        assert bundle_signing.verify_signature(digest, sig, "unknown-kid") is False

    def test_tampered_digest_rejected(self, monkeypatch):
        priv_b64, pub_b64 = _gen_keypair()
        monkeypatch.setenv(
            "MCP_BUNDLE_SIGNING_PUBLIC_KEYS",
            json.dumps({"k": pub_b64}),
        )
        sig = bundle_signing.sign_digest(b"original-digest-padding-32-bytes", priv_b64)
        assert bundle_signing.verify_signature(
            b"tampered-digest-padding-32-bytes!", sig, "k"
        ) is False

    def test_empty_trust_store_rejects(self, monkeypatch):
        monkeypatch.delenv("MCP_BUNDLE_SIGNING_PUBLIC_KEYS", raising=False)
        assert bundle_signing.verify_signature(b"d" * 32, "zzz", "k") is False

    def test_malformed_json_rejected(self, monkeypatch):
        monkeypatch.setenv("MCP_BUNDLE_SIGNING_PUBLIC_KEYS", "not-json")
        assert bundle_signing.verify_signature(b"d" * 32, "zzz", "k") is False

    def test_malformed_signature_rejected(self, monkeypatch):
        _, pub_b64 = _gen_keypair()
        monkeypatch.setenv(
            "MCP_BUNDLE_SIGNING_PUBLIC_KEYS",
            json.dumps({"k": pub_b64}),
        )
        assert bundle_signing.verify_signature(
            b"d" * 32, "!!!not-base64!!!", "k"
        ) is False


# ---------------------------------------------------------------------------
# verify_skill + HTTP surface
# ---------------------------------------------------------------------------

class TestAttachSignatureHTTP:
    def test_attach_and_verify_end_to_end(self, client, monkeypatch):
        # Generate keys + trust the public half.
        priv_b64, pub_b64 = _gen_keypair()
        monkeypatch.setenv(
            "MCP_BUNDLE_SIGNING_PUBLIC_KEYS",
            json.dumps({"author-1": pub_b64}),
        )
        # Create a skill + bundle via the HTTP surface.
        client.post(
            "/skills",
            json={"id": "signed", "name": "Signed", "version": "1.0.0"},
            headers=ADMIN_HEADERS,
        )
        # Upload a bundle via multipart — we use the raw tar path.
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = b"hello world"
            info = tarfile.TarInfo(name="SKILL.md")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        r = client.post(
            "/skills/signed/versions/1.0.0/bundle",
            files={"file": ("bundle.tar.gz", buf.getvalue(),
                            "application/gzip")},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201, r.text

        # Sign the canonical digest using our private key — we
        # need to reach into the DB to compute it.
        factory = client.app.state.session_factory
        with factory() as db:
            from mcp_server.models import Skill
            s = db.query(Skill).filter_by(id="signed").first()
            digest = bundle_signing.compute_bundle_digest(db, s.pk)
        sig = bundle_signing.sign_digest(digest, priv_b64)

        r = client.post(
            "/skills/signed/versions/1.0.0/signature",
            json={"signature": sig, "kid": "author-1"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200, r.text

        # GET with verified: True.
        r = client.get("/admin/skills/signed", headers=ADMIN_HEADERS)
        body = r.json()
        assert body["bundle_signature_kid"] == "author-1"
        assert body["verified"] is True

        # Mutating the bundle invalidates the signature.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = b"hello MARS"
            info = tarfile.TarInfo(name="SKILL.md")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        client.post(
            "/skills/signed/versions/1.0.0/bundle",
            files={"file": ("bundle.tar.gz", buf.getvalue(),
                            "application/gzip")},
            headers=ADMIN_HEADERS,
        )
        r = client.get("/admin/skills/signed", headers=ADMIN_HEADERS)
        assert r.json()["verified"] is False

    def test_missing_signature_400(self, client):
        client.post(
            "/skills",
            json={"id": "unsigned", "name": "Unsigned", "version": "1.0.0"},
            headers=ADMIN_HEADERS,
        )
        r = client.post(
            "/skills/unsigned/versions/1.0.0/signature",
            json={"signature": "", "kid": "k"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 400

    def test_unknown_version_404(self, client):
        r = client.post(
            "/skills/ghost/versions/9.9.9/signature",
            json={"signature": "abc", "kid": "k"},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 404

    def test_delete_signature_clears(self, client):
        priv_b64, pub_b64 = _gen_keypair()
        client.post(
            "/skills",
            json={"id": "to-clear", "name": "Clear",
                  "version": "1.0.0"},
            headers=ADMIN_HEADERS,
        )
        client.post(
            "/skills/to-clear/versions/1.0.0/signature",
            json={"signature": "abc", "kid": "k"},
            headers=ADMIN_HEADERS,
        )
        r = client.delete(
            "/skills/to-clear/versions/1.0.0/signature",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 204
        r = client.get("/admin/skills/to-clear", headers=ADMIN_HEADERS)
        body = r.json()
        assert body["bundle_signature_kid"] is None
        assert body["verified"] is False
