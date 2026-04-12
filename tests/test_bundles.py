"""Unit tests for the bundle extraction + storage module."""

import io
import tarfile
import zipfile

import pytest

from mcp_server import bundles
from mcp_server.bundles import (
    BundleError,
    BundleFile,
    MAX_BUNDLE_BYTES,
    build_targz,
    detect_format,
    extract_archive,
    list_bundle,
    store_bundle,
)
from mcp_server.models import Skill


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in entries.items():
            zf.writestr(path, content)
    return buf.getvalue()


def _make_tar(entries: dict[str, bytes], mode: str = "w:gz") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for path, content in entries.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _persist_skill(db_session) -> Skill:
    s = Skill(id="skill-a", name="A", description="", version="1.0.0", is_latest=True, metadata_={})
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

class TestDetectFormat:
    def test_zip(self):
        assert detect_format(_make_zip({"a.txt": b"x"})) == "zip"

    def test_tar_gz(self):
        assert detect_format(_make_tar({"a.txt": b"x"}, mode="w:gz")) == "tar.gz"

    def test_tar_bz2(self):
        assert detect_format(_make_tar({"a.txt": b"x"}, mode="w:bz2")) == "tar.bz2"

    def test_tar_xz(self):
        assert detect_format(_make_tar({"a.txt": b"x"}, mode="w:xz")) == "tar.xz"

    def test_plain_tar(self):
        assert detect_format(_make_tar({"a.txt": b"x"}, mode="w:")) == "tar"

    def test_garbage_rejected(self):
        with pytest.raises(BundleError):
            detect_format(b"not an archive at all")

    def test_tiny_input_rejected(self):
        with pytest.raises(BundleError):
            detect_format(b"")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

class TestExtract:
    def test_zip_roundtrip(self):
        data = _make_zip({"SKILL.md": b"# hi", "scripts/run.py": b"print(1)"})
        files = extract_archive(data)
        paths = {f.path for f in files}
        assert paths == {"SKILL.md", "scripts/run.py"}

    def test_targz_roundtrip(self):
        data = _make_tar({"a.txt": b"hello"}, mode="w:gz")
        files = extract_archive(data)
        assert files[0].path == "a.txt"
        assert files[0].content == b"hello"

    def test_tar_xz_roundtrip(self):
        data = _make_tar({"a.txt": b"x"}, mode="w:xz")
        assert extract_archive(data)[0].content == b"x"

    def test_strips_common_prefix_opt_in(self):
        """The caller can opt in to stripping a wrapper dir (e.g. GitHub tarballs)."""
        data = _make_tar(
            {"skills-main/pdf/SKILL.md": b"hi", "skills-main/pdf/ref.md": b"ref"},
            mode="w:gz",
        )
        files = extract_archive(data, strip_common_prefix=True)
        paths = {f.path for f in files}
        assert paths == {"pdf/SKILL.md", "pdf/ref.md"}

    def test_no_auto_strip_single_file(self):
        """A single-entry archive with a directory component must keep it."""
        data = _make_zip({"scripts/run.py": b"print(1)"})
        files = extract_archive(data)
        assert files[0].path == "scripts/run.py"

    def test_path_traversal_rejected(self):
        data = _make_tar({"../evil": b"x"}, mode="w:gz")
        with pytest.raises(BundleError):
            extract_archive(data)

    def test_absolute_path_rejected(self):
        data = _make_tar({"/etc/passwd": b"x"}, mode="w:gz")
        with pytest.raises(BundleError):
            extract_archive(data)

    def test_symlink_rejected(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(name="link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tf.addfile(info)
        with pytest.raises(BundleError):
            extract_archive(buf.getvalue())

    def test_empty_archive_rejected(self):
        data = _make_zip({})
        with pytest.raises(BundleError):
            extract_archive(data)

    def test_bad_zip_rejected(self):
        with pytest.raises(BundleError):
            extract_archive(b"PK\x03\x04junk")

    def test_unsupported_format_rejected(self):
        with pytest.raises(BundleError):
            extract_archive(b"hello world, not an archive")


# ---------------------------------------------------------------------------
# Size limits
# ---------------------------------------------------------------------------

class TestLimits:
    def test_too_many_files(self, monkeypatch):
        monkeypatch.setattr(bundles, "MAX_FILE_COUNT", 3)
        data = _make_zip({f"f{i}.txt": b"x" for i in range(5)})
        with pytest.raises(BundleError):
            extract_archive(data)

    def test_over_total_size(self, monkeypatch):
        monkeypatch.setattr(bundles, "MAX_BUNDLE_BYTES", 10)
        data = _make_zip({"big.bin": b"x" * 50})
        with pytest.raises(BundleError):
            extract_archive(data)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestStore:
    def test_put_list_delete_roundtrip(self, db_session):
        skill = _persist_skill(db_session)
        files = [BundleFile("a.txt", b"hello"), BundleFile("b/c.txt", b"world")]
        stats = store_bundle(db_session, skill.pk, files)
        assert stats.file_count == 2
        assert stats.total_size == len(b"hello") + len(b"world")

        listed = list_bundle(db_session, skill.pk)
        assert [f.path for f in listed] == ["a.txt", "b/c.txt"]
        assert all(f.sha256 for f in listed)

    def test_put_replaces_existing(self, db_session):
        skill = _persist_skill(db_session)
        store_bundle(db_session, skill.pk, [BundleFile("old", b"1")])
        store_bundle(db_session, skill.pk, [BundleFile("new", b"2")])
        listed = list_bundle(db_session, skill.pk)
        assert [f.path for f in listed] == ["new"]

    def test_build_targz(self, db_session):
        skill = _persist_skill(db_session)
        store_bundle(
            db_session, skill.pk,
            [BundleFile("SKILL.md", b"# hello"), BundleFile("scripts/run.py", b"print(1)")],
        )
        data = build_targz(db_session, skill.pk)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            names = sorted(m.name for m in tf.getmembers())
            assert names == ["SKILL.md", "scripts/run.py"]
