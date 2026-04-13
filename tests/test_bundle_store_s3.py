"""
Wave 5 tests — S3BundleStore against a moto-mocked S3.

Covers put / read / list / delete / copy round-trips, the index-and-bytes
invariants (SkillFile rows + S3 objects stay in sync), and the factory
`build_store_from_settings` mapping.

Inline store behavior is already covered by the pre-existing
`tests/test_bundles.py` + `tests/test_api_bundles.py` — they run the same
suite through the module-level shims, which now delegate to whichever
default store is configured. We re-verify that path here for
backwards-compat clarity.
"""

from __future__ import annotations

import io
import tarfile
import zipfile

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mcp_server.bundles import (
    BundleFile,
    BundleStats,
    InlineBundleStore,
    S3BundleStore,
    build_store_from_settings,
    get_default_store,
    reset_default_store,
    set_default_store,
)
from mcp_server.models import Base, Skill


BUCKET = "mcp-test-bundles"


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    Base.metadata.drop_all(engine)


def _make_skill(db, *, skill_id, version="1.0.0", is_latest=True) -> Skill:
    s = Skill(
        id=skill_id, name=skill_id, description="",
        version=version, is_latest=is_latest, metadata_={},
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class TestFactory:
    def test_inline_by_default(self, monkeypatch):
        monkeypatch.delenv("MCP_BUNDLE_STORE", raising=False)
        from mcp_server.config import get_settings
        get_settings.cache_clear()
        try:
            store = build_store_from_settings(get_settings())
            assert isinstance(store, InlineBundleStore)
            assert store.backend_name == "inline"
        finally:
            get_settings.cache_clear()

    def test_s3_requires_bucket(self, monkeypatch):
        monkeypatch.setenv("MCP_BUNDLE_STORE", "s3")
        monkeypatch.setenv("MCP_BUNDLE_S3_BUCKET", "")
        from mcp_server.config import get_settings
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="bucket"):
                build_store_from_settings(get_settings())
        finally:
            get_settings.cache_clear()

    def test_unknown_backend_rejected(self, monkeypatch):
        monkeypatch.setenv("MCP_BUNDLE_STORE", "telegraph")
        from mcp_server.config import get_settings
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="Unknown MCP_BUNDLE_STORE"):
                build_store_from_settings(get_settings())
        finally:
            get_settings.cache_clear()


# ---------------------------------------------------------------------------
# S3BundleStore round-trips — all run under a single moto mock.
# ---------------------------------------------------------------------------

@mock_aws
class TestS3BundleStore:
    def _store_and_skill(self, db_session) -> tuple[S3BundleStore, Skill]:
        """Create a bucket and a skill row, return (store, skill)."""
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        store = S3BundleStore(bucket=BUCKET, prefix="bundles", client=s3)
        skill = _make_skill(db_session, skill_id="skill-a")
        return store, skill

    def test_put_and_read_round_trip(self, db_session):
        store, skill = self._store_and_skill(db_session)
        stats = store.put_files(
            db_session, skill.pk,
            [
                BundleFile("SKILL.md", b"# hi"),
                BundleFile("scripts/run.py", b"print('x')\n"),
            ],
        )
        assert stats == BundleStats(file_count=2, total_size=len(b"# hi") + len(b"print('x')\n"))

        # List reads from the DB index.
        listing = store.list_files(db_session, skill.pk)
        assert [f.path for f in listing] == ["SKILL.md", "scripts/run.py"]

        # Per-file read fetches from S3.
        md = store.read_file(db_session, skill.pk, "SKILL.md")
        assert md is not None and md.content == b"# hi"
        py = store.read_file(db_session, skill.pk, "scripts/run.py")
        assert py is not None and py.content == b"print('x')\n"

    def test_unknown_path_returns_none(self, db_session):
        store, skill = self._store_and_skill(db_session)
        assert store.read_file(db_session, skill.pk, "nope.txt") is None

    def test_put_replaces_existing(self, db_session):
        """Replace semantics must wipe old S3 objects AND old rows."""
        store, skill = self._store_and_skill(db_session)
        store.put_files(db_session, skill.pk, [BundleFile("old.txt", b"stale")])
        store.put_files(db_session, skill.pk, [BundleFile("new.txt", b"fresh")])

        # Only new.txt in the index.
        listing = store.list_files(db_session, skill.pk)
        assert [f.path for f in listing] == ["new.txt"]

        # Only new.txt in S3 (no orphan old.txt object).
        s3 = store.client
        resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=f"bundles/pk{skill.pk}/")
        keys = [obj["Key"] for obj in resp.get("Contents", [])]
        assert keys == [f"bundles/pk{skill.pk}/new.txt"]

    def test_delete_removes_rows_and_objects(self, db_session):
        store, skill = self._store_and_skill(db_session)
        store.put_files(
            db_session, skill.pk,
            [BundleFile("a.txt", b"1"), BundleFile("b/c.txt", b"2")],
        )
        n = store.delete_all(db_session, skill.pk)
        assert n == 2
        assert store.list_files(db_session, skill.pk) == []
        resp = store.client.list_objects_v2(Bucket=BUCKET, Prefix=f"bundles/pk{skill.pk}/")
        assert resp.get("Contents", []) == []

    def test_copy_across_skills(self, db_session):
        store, src = self._store_and_skill(db_session)
        dst = _make_skill(db_session, skill_id="skill-b")

        store.put_files(
            db_session, src.pk,
            [BundleFile("SKILL.md", b"hi"), BundleFile("x/y", b"z")],
        )
        stats = store.copy_all(db_session, src.pk, dst.pk)
        assert stats.file_count == 2

        dst_listing = store.list_files(db_session, dst.pk)
        assert [f.path for f in dst_listing] == ["SKILL.md", "x/y"]

        # Source is untouched.
        src_listing = store.list_files(db_session, src.pk)
        assert [f.path for f in src_listing] == ["SKILL.md", "x/y"]

        # dst S3 objects exist independently.
        dst_md = store.read_file(db_session, dst.pk, "SKILL.md")
        assert dst_md is not None and dst_md.content == b"hi"

    def test_build_targz_rebuilds_archive(self, db_session):
        store, skill = self._store_and_skill(db_session)
        store.put_files(
            db_session, skill.pk,
            [BundleFile("SKILL.md", b"# hi"), BundleFile("x/y.txt", b"z")],
        )
        data = store.build_targz(db_session, skill.pk)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            names = sorted(m.name for m in tf.getmembers())
        assert names == ["SKILL.md", "x/y.txt"]

    def test_missing_object_with_row_present_returns_none(self, db_session):
        """Row/object drift — if S3 lost the object but the row survives,
        read_file should surface this as None rather than crashing."""
        store, skill = self._store_and_skill(db_session)
        store.put_files(db_session, skill.pk, [BundleFile("a.txt", b"data")])
        # Manually delete the S3 object behind the store's back.
        store.client.delete_object(
            Bucket=BUCKET, Key=f"bundles/pk{skill.pk}/a.txt"
        )
        assert store.read_file(db_session, skill.pk, "a.txt") is None


# ---------------------------------------------------------------------------
# Default-store routing — module shims honor set_default_store
# ---------------------------------------------------------------------------

@mock_aws
class TestDefaultStoreRouting:
    def test_set_default_store_routes_module_shims(self, db_session):
        from mcp_server.bundles import store_bundle, list_bundle, get_file

        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        store = S3BundleStore(bucket=BUCKET, prefix="bundles", client=s3)
        set_default_store(store)
        try:
            skill = _make_skill(db_session, skill_id="skill-a")
            store_bundle(db_session, skill.pk, [BundleFile("f", b"v")])
            listing = list_bundle(db_session, skill.pk)
            assert [f.path for f in listing] == ["f"]
            got = get_file(db_session, skill.pk, "f")
            assert got is not None and got.content == b"v"
        finally:
            reset_default_store()

    def test_get_default_store_is_inline_when_unset(self):
        # Autouse fixture resets between tests, but make sure.
        reset_default_store()
        store = get_default_store()
        assert store.backend_name == "inline"
