"""
Smoke-level fuzz for `bundles.extract_archive`. Stdlib only — no
Hypothesis. The bar is low: for every input we try, the function must
either return a valid list of BundleFile records OR raise BundleError.
A raw `Exception` — ValueError from deep in the zipfile module, a
segfault, an infinite loop — is a bug.

Why: bundle extraction is the highest-exposure code path we accept from
untrusted input (user uploads an archive). Every archive library we
wrap has footguns (zip slip, quine bombs, malformed members). This
suite doesn't claim to catch every footgun; it establishes a floor.

Runs in a few seconds per N=200. Bumped up in CI without thinking about
the size — all bounded-time inputs.
"""

from __future__ import annotations

import io
import random
import tarfile
import zipfile

import pytest

from mcp_server.bundles import (
    BundleError,
    extract_archive,
)


N = 200


def _random_bytes(rng: random.Random, min_len: int = 0, max_len: int = 2048) -> bytes:
    return bytes(rng.randint(0, 255) for _ in range(rng.randint(min_len, max_len)))


# ---------------------------------------------------------------------------
# Plain garbage input
# ---------------------------------------------------------------------------

class TestGarbageInput:
    def test_random_bytes_never_raise_unexpected(self):
        rng = random.Random(0xDEADBEEF)
        for i in range(N):
            data = _random_bytes(rng, 0, 4096)
            try:
                extract_archive(data)
            except BundleError:
                pass
            except Exception as exc:  # noqa: BLE001 — this is the failure mode
                pytest.fail(
                    f"iter {i}: unexpected {type(exc).__name__} on {len(data)} bytes: {exc!r}"
                )

    def test_every_size_under_8_is_rejected(self):
        """`detect_format` needs at least a few magic bytes. Zero-length
        through 7-byte inputs must all cleanly raise BundleError."""
        for n in range(8):
            with pytest.raises(BundleError):
                extract_archive(b"\x00" * n)

    def test_magic_byte_prefix_without_real_archive(self):
        """Bytes that look like magic but aren't followed by valid structure."""
        prefixes = [
            b"PK\x03\x04" + b"garbage" * 100,
            b"\x1f\x8b" + b"notreallygzip" * 100,
            b"BZh" + b"notbz2" * 100,
            b"\xfd7zXZ\x00" + b"notxz" * 100,
        ]
        for p in prefixes:
            with pytest.raises(BundleError):
                extract_archive(p)


# ---------------------------------------------------------------------------
# Malicious path shapes
# ---------------------------------------------------------------------------

class TestMaliciousPaths:
    @pytest.mark.parametrize("path", [
        "../evil",
        "../../etc/passwd",
        "dir/../../outside",
        "a/b/../../../../../etc/shadow",
        "/absolute/path",
        "/",
        "/etc/passwd",
    ])
    def test_path_traversal_or_absolute_rejected_zip(self, path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(path, b"x")
        with pytest.raises(BundleError):
            extract_archive(buf.getvalue())

    @pytest.mark.parametrize("path", [
        "../evil",
        "/etc/passwd",
        "./../../out",
    ])
    def test_path_traversal_or_absolute_rejected_tar(self, path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(name=path)
            info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))
        with pytest.raises(BundleError):
            extract_archive(buf.getvalue())

    def test_tar_symlink_rejected(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(name="link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tf.addfile(info)
        with pytest.raises(BundleError):
            extract_archive(buf.getvalue())

    def test_tar_hardlink_rejected(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            # Put a real file first so the hardlink has a target name.
            fi = tarfile.TarInfo(name="real.txt")
            fi.size = 1
            tf.addfile(fi, io.BytesIO(b"x"))
            li = tarfile.TarInfo(name="ln")
            li.type = tarfile.LNKTYPE
            li.linkname = "real.txt"
            tf.addfile(li)
        with pytest.raises(BundleError):
            extract_archive(buf.getvalue())


# ---------------------------------------------------------------------------
# Size / count caps
# ---------------------------------------------------------------------------

class TestLimits:
    def test_too_many_files_rejected(self, monkeypatch):
        # Monkey-patch the cap so we don't have to generate 10k files.
        from mcp_server import bundles
        monkeypatch.setattr(bundles, "MAX_FILE_COUNT", 5)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(10):
                zf.writestr(f"f{i}", b"x")
        with pytest.raises(BundleError):
            extract_archive(buf.getvalue())

    def test_oversize_single_file_rejected(self, monkeypatch):
        from mcp_server import bundles
        monkeypatch.setattr(bundles, "MAX_BUNDLE_BYTES", 100)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("big", b"x" * 200)
        with pytest.raises(BundleError):
            extract_archive(buf.getvalue())

    def test_oversize_cumulative_rejected(self, monkeypatch):
        from mcp_server import bundles
        monkeypatch.setattr(bundles, "MAX_BUNDLE_BYTES", 150)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("a", b"x" * 100)
            zf.writestr("b", b"x" * 100)
        with pytest.raises(BundleError):
            extract_archive(buf.getvalue())


# ---------------------------------------------------------------------------
# Valid archives — every legitimate input should round-trip
# ---------------------------------------------------------------------------

class TestValidArchivesRoundTrip:
    def test_random_zip_archives_extract_cleanly(self):
        rng = random.Random(42)
        for _ in range(N // 4):
            n = rng.randint(1, 6)
            entries = {
                f"file{i}_{rng.randint(0, 999)}.txt": _random_bytes(rng, 0, 256)
                for i in range(n)
            }
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                for p, c in entries.items():
                    zf.writestr(p, c)
            files = extract_archive(buf.getvalue())
            # All entries that made it through are non-empty paths,
            # content-matched to what we put in (ignoring dedup order).
            got = {f.path: f.content for f in files}
            for p, c in entries.items():
                if not p:  # empty path; excluded by normalize
                    continue
                assert got.get(p) == c, f"mismatch on {p!r}"

    def test_random_tar_gz_archives_extract_cleanly(self):
        rng = random.Random(43)
        for _ in range(N // 4):
            n = rng.randint(1, 6)
            entries = {
                f"t{i}/{rng.randint(0, 999)}.bin": _random_bytes(rng, 0, 256)
                for i in range(n)
            }
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                for p, c in entries.items():
                    info = tarfile.TarInfo(name=p)
                    info.size = len(c)
                    tf.addfile(info, io.BytesIO(c))
            files = extract_archive(buf.getvalue())
            got = {f.path: f.content for f in files}
            for p, c in entries.items():
                assert got.get(p) == c
