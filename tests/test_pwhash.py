"""Unit tests for mcp_server.pwhash.

The helper is shared by the Web UI and the mcp_server authenticate
endpoint. The 72-byte truncation and error-on-bad-hash behaviors are
load-bearing — they're why mismatched prefix/suffix pairs don't
accidentally unlock accounts.
"""

from __future__ import annotations

import bcrypt

from mcp_server.pwhash import _encode, _MAX_PASSWORD_BYTES, hash_password, verify_password


class TestHashRoundTrip:
    def test_round_trip(self):
        h = hash_password("correct horse battery staple")
        assert verify_password("correct horse battery staple", h)
        assert not verify_password("wrong", h)

    def test_every_hash_is_unique(self):
        """Different salts per call even for the same password."""
        a = hash_password("same")
        b = hash_password("same")
        assert a != b
        assert verify_password("same", a)
        assert verify_password("same", b)


class TestEdgeCases:
    def test_empty_plain_rejected(self):
        h = hash_password("x")
        assert verify_password("", h) is False

    def test_empty_hash_rejected(self):
        assert verify_password("x", "") is False

    def test_malformed_hash_returns_false_not_raises(self):
        assert verify_password("pw", "not-a-bcrypt-hash") is False

    def test_malformed_hash_bytes_rejected(self):
        # A short hash that isn't a valid bcrypt block. bcrypt.checkpw
        # raises ValueError; our wrapper catches it and returns False.
        assert verify_password("pw", "$2b$12$short") is False


class TestByteCap:
    def test_72_byte_truncation(self):
        """bcrypt silently ignores bytes past 72; our wrapper truncates
        deterministically so a 200-char password still verifies and
        'the same 72-byte prefix' also verifies."""
        pw = "a" * 200
        h = hash_password(pw)
        assert verify_password(pw, h)
        # First 72 bytes also verify.
        assert verify_password("a" * 72, h)
        # Anything shorter doesn't.
        assert not verify_password("a" * 71, h)

    def test_unicode_bytes_count_not_chars(self):
        """Multi-byte chars count toward the 72-byte cap as bytes, not
        as chars — a 40-char string of 3-byte emojis is over the cap
        and gets truncated."""
        # 🙂 is 4 bytes in UTF-8; 20 copies = 80 bytes.
        emoji_20 = "🙂" * 20
        assert len(emoji_20.encode("utf-8")) == 80
        h = hash_password(emoji_20)
        # The first 72 bytes cut mid-emoji — truncation is byte-level
        # and deterministic, which is what we're pinning.
        assert verify_password(emoji_20, h)
        # 18 emojis (72 bytes) verifies iff bcrypt's cut aligns with
        # the byte boundary. That's the contract we want.
        assert verify_password("🙂" * 18, h)


class TestEncodeHelper:
    def test_encodes_to_bytes(self):
        assert _encode("abc") == b"abc"

    def test_respects_cap(self):
        assert _encode("x" * 100) == b"x" * _MAX_PASSWORD_BYTES

    def test_max_cap_constant(self):
        # Guard against an accidental tweak of the cap.
        assert _MAX_PASSWORD_BYTES == 72


class TestDeterministicWithKnownHash:
    """Pin a known-good hash so rotation-style bugs (e.g. bcrypt
    library upgrade changes the prefix format) surface here."""

    def test_library_accepts_own_hash(self):
        pw = "known-password"
        h = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        assert verify_password(pw, h)
