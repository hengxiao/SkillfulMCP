"""Integration tests for bundle upload/list/download endpoints."""

import io
import tarfile
import zipfile

import pytest

from tests.conftest import (
    ADMIN_HEADERS,
    bearer,
    get_token,
    make_agent,
    make_skill,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in entries.items():
            zf.writestr(path, content)
    return buf.getvalue()


def _targz_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, content in entries.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _agent_headers(client, **kwargs):
    make_agent(client, id="api-agent", scope=["read"], **kwargs)
    return bearer(get_token(client, "api-agent"))


def _upload(client, skill_id: str, version: str, archive: bytes, name: str):
    return client.post(
        f"/skills/{skill_id}/versions/{version}/bundle",
        files={"file": (name, archive, "application/octet-stream")},
        headers=ADMIN_HEADERS,
    )


# ---------------------------------------------------------------------------
# POST /skills/{id}/versions/{ver}/bundle
# ---------------------------------------------------------------------------

class TestUpload:
    def test_upload_zip(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        r = _upload(
            client, "skill-a", "1.0.0",
            _zip_bytes({"SKILL.md": b"# hi", "ref.md": b"ref"}),
            "bundle.zip",
        )
        assert r.status_code == 201
        body = r.json()
        assert body["file_count"] == 2
        assert body["total_size"] == len(b"# hi") + len(b"ref")

    def test_upload_targz(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        r = _upload(
            client, "skill-a", "1.0.0",
            _targz_bytes({"a.txt": b"hello"}),
            "bundle.tar.gz",
        )
        assert r.status_code == 201

    def test_upload_replaces_existing(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        _upload(client, "skill-a", "1.0.0", _zip_bytes({"old.txt": b"x"}), "a.zip")
        _upload(client, "skill-a", "1.0.0", _zip_bytes({"new.txt": b"y"}), "b.zip")
        headers = _agent_headers(client, skills=["skill-a"])
        r = client.get("/skills/skill-a/versions/1.0.0/files", headers=headers)
        paths = [f["path"] for f in r.json()]
        assert paths == ["new.txt"]

    def test_upload_requires_admin(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        r = client.post(
            "/skills/skill-a/versions/1.0.0/bundle",
            files={"file": ("b.zip", _zip_bytes({"a": b"x"}), "application/zip")},
        )
        assert r.status_code == 403

    def test_upload_unknown_skill_returns_404(self, client):
        r = _upload(client, "no-such", "1.0.0", _zip_bytes({"a": b"x"}), "b.zip")
        assert r.status_code == 404

    def test_upload_bad_archive_returns_400(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        r = _upload(client, "skill-a", "1.0.0", b"not an archive", "b.bin")
        assert r.status_code == 400

    def test_upload_path_traversal_returns_400(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        r = _upload(
            client, "skill-a", "1.0.0",
            _targz_bytes({"../evil": b"x"}),
            "bad.tar.gz",
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# GET /skills/{id}/versions/{ver}/files (JWT)
# ---------------------------------------------------------------------------

class TestListFiles:
    def test_authorized_agent_lists(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        _upload(client, "skill-a", "1.0.0", _zip_bytes({"a.txt": b"x", "b.txt": b"y"}), "b.zip")
        headers = _agent_headers(client, skills=["skill-a"])
        r = client.get("/skills/skill-a/versions/1.0.0/files", headers=headers)
        assert r.status_code == 200
        assert sorted(f["path"] for f in r.json()) == ["a.txt", "b.txt"]

    def test_unauthorized_agent_403(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        _upload(client, "skill-a", "1.0.0", _zip_bytes({"a": b"x"}), "b.zip")
        headers = _agent_headers(client)  # no skills granted
        r = client.get("/skills/skill-a/versions/1.0.0/files", headers=headers)
        assert r.status_code == 403

    def test_no_jwt_rejected(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        r = client.get("/skills/skill-a/versions/1.0.0/files")
        assert r.status_code in (401, 403)

    def test_unknown_version_404(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        headers = _agent_headers(client, skills=["skill-a"])
        r = client.get("/skills/skill-a/versions/9.9.9/files", headers=headers)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /skills/{id}/versions/{ver}/files/{path}  (JWT)
# ---------------------------------------------------------------------------

class TestGetFile:
    def test_download_file(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        _upload(client, "skill-a", "1.0.0", _zip_bytes({"dir/a.txt": b"hello"}), "b.zip")
        headers = _agent_headers(client, skills=["skill-a"])
        r = client.get("/skills/skill-a/versions/1.0.0/files/dir/a.txt", headers=headers)
        assert r.status_code == 200
        assert r.content == b"hello"
        assert r.headers.get("X-Content-SHA256")

    def test_unknown_file_404(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        _upload(client, "skill-a", "1.0.0", _zip_bytes({"a": b"x"}), "b.zip")
        headers = _agent_headers(client, skills=["skill-a"])
        r = client.get("/skills/skill-a/versions/1.0.0/files/no-such", headers=headers)
        assert r.status_code == 404

    def test_skillset_grants_access(self, client):
        """JWT granted via skillset should also unlock bundle reads."""
        client.post(
            "/skillsets",
            json={"id": "ss-1", "name": "SS1"},
            headers=ADMIN_HEADERS,
        ).raise_for_status()
        make_skill(client, id="skill-a", version="1.0.0", skillset_ids=["ss-1"])
        _upload(client, "skill-a", "1.0.0", _zip_bytes({"a.txt": b"x"}), "b.zip")
        headers = _agent_headers(client, skillsets=["ss-1"])
        r = client.get("/skills/skill-a/versions/1.0.0/files/a.txt", headers=headers)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# GET /skills/{id}/versions/{ver}/bundle (JWT, full download)
# ---------------------------------------------------------------------------

class TestDownloadBundle:
    def test_download_rebuilt_targz(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        _upload(client, "skill-a", "1.0.0", _zip_bytes({"SKILL.md": b"hi", "x/y": b"z"}), "b.zip")
        headers = _agent_headers(client, skills=["skill-a"])
        r = client.get("/skills/skill-a/versions/1.0.0/bundle", headers=headers)
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/gzip"
        with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
            names = sorted(m.name for m in tf.getmembers())
        assert names == ["SKILL.md", "x/y"]


# ---------------------------------------------------------------------------
# DELETE /skills/{id}/versions/{ver}/bundle (admin)
# ---------------------------------------------------------------------------

class TestCopyBundle:
    def test_copy_same_skill_different_version(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        make_skill(client, id="skill-a", version="2.0.0")
        _upload(
            client, "skill-a", "1.0.0",
            _zip_bytes({"SKILL.md": b"hi", "x/y.txt": b"bye"}),
            "b.zip",
        )
        r = client.post(
            "/skills/skill-a/versions/2.0.0/bundle/copy-from/skill-a/1.0.0",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201
        body = r.json()
        assert body["file_count"] == 2

        headers = _agent_headers(client, skills=["skill-a"])
        listing = client.get("/skills/skill-a/versions/2.0.0/files", headers=headers).json()
        assert sorted(f["path"] for f in listing) == ["SKILL.md", "x/y.txt"]

    def test_copy_across_skills_clone(self, client):
        """Cross-skill copy: used by the clone-skill flow."""
        make_skill(client, id="skill-src", version="1.0.0")
        make_skill(client, id="skill-clone", version="1.0.0")
        _upload(
            client, "skill-src", "1.0.0",
            _zip_bytes({"SKILL.md": b"hi"}),
            "b.zip",
        )
        r = client.post(
            "/skills/skill-clone/versions/1.0.0/bundle/copy-from/skill-src/1.0.0",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201
        headers = _agent_headers(client, skills=["skill-clone"])
        listing = client.get(
            "/skills/skill-clone/versions/1.0.0/files", headers=headers
        ).json()
        assert [f["path"] for f in listing] == ["SKILL.md"]

    def test_copy_requires_admin(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        make_skill(client, id="skill-a", version="2.0.0")
        r = client.post(
            "/skills/skill-a/versions/2.0.0/bundle/copy-from/skill-a/1.0.0"
        )
        assert r.status_code == 403

    def test_copy_unknown_src_404(self, client):
        make_skill(client, id="skill-a", version="2.0.0")
        r = client.post(
            "/skills/skill-a/versions/2.0.0/bundle/copy-from/skill-a/9.9.9",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 404


class TestDeleteBundle:
    def test_delete_empties_bundle(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        _upload(client, "skill-a", "1.0.0", _zip_bytes({"a": b"x"}), "b.zip")
        r = client.delete(
            "/skills/skill-a/versions/1.0.0/bundle", headers=ADMIN_HEADERS
        )
        assert r.status_code == 204
        headers = _agent_headers(client, skills=["skill-a"])
        listing = client.get(
            "/skills/skill-a/versions/1.0.0/files", headers=headers
        ).json()
        assert listing == []

    def test_delete_skill_version_cascades_bundle(self, client):
        make_skill(client, id="skill-a", version="1.0.0")
        make_skill(client, id="skill-a", version="2.0.0")
        _upload(client, "skill-a", "1.0.0", _zip_bytes({"a": b"x"}), "b.zip")
        r = client.delete("/skills/skill-a?version=1.0.0", headers=ADMIN_HEADERS)
        assert r.status_code == 204
        # v2.0.0 survives, v1.0.0 and its bundle are gone.
        headers = _agent_headers(client, skills=["skill-a"])
        vs = client.get("/skills/skill-a/versions", headers=headers).json()
        assert [v["version"] for v in vs] == ["2.0.0"]
