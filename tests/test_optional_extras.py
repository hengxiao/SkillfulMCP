"""Optional-extras availability tests.

pyproject.toml declares `[s3]`, `[postgres]`, `[dev]`, `[examples]`
extras. The Dockerfile bundles `.[postgres,s3]` into the runtime
image; the catalog *refuses to run* with MCP_BUNDLE_STORE=s3 if
boto3 isn't installed — but the pre-Wave check doesn't trigger
until a bundle upload hits. Test the dev-venv invariant directly
so a future edit of the Dockerfile that drops `s3` is caught by
the suite, not by an operator 500 in production.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    "module,extra",
    [
        ("boto3", "s3"),
        ("botocore", "s3"),
        ("psycopg2", "postgres"),
    ],
)
def test_extras_import(module: str, extra: str):
    """The dev install (`pip install -e '.[dev]'`) brings in every
    optional extra. Assert each dep declared by its extra is
    importable so nothing in pyproject.toml rotted since the test
    was written."""
    try:
        importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - real failure path
        pytest.fail(
            f"{module!r} (part of extra {extra!r}) is not importable: {exc}"
        )


def test_bundle_store_factory_raises_cleanly_without_boto3(monkeypatch):
    """When an operator sets `MCP_BUNDLE_STORE=s3` but the runtime
    is missing boto3 (e.g. Dockerfile dropped the extra), the
    factory should raise a RuntimeError with an install hint at
    first use — NOT crash with ImportError somewhere random during
    request handling.

    Tested by faking boto3 as missing at import time.
    """
    import sys

    from mcp_server.bundles import reset_default_store
    from mcp_server.config import get_settings

    # Force a fresh factory + settings call — get_settings is
    # lru_cached, and any earlier test with a different
    # MCP_BUNDLE_STORE leaves a stale Settings object.
    reset_default_store()
    get_settings.cache_clear()

    # Hide boto3 from sys.modules + importlib so the factory's
    # deferred import raises ImportError.
    saved = {}
    for name in list(sys.modules):
        if name == "boto3" or name.startswith("boto3."):
            saved[name] = sys.modules.pop(name)
    monkeypatch.setitem(
        sys.modules, "boto3", None
    )  # None triggers ImportError on `import boto3`

    monkeypatch.setenv("MCP_BUNDLE_STORE", "s3")
    monkeypatch.setenv("MCP_BUNDLE_S3_BUCKET", "x")
    try:
        from mcp_server.bundles import get_default_store

        with pytest.raises(RuntimeError, match="boto3"):
            get_default_store()
    finally:
        # Restore so other tests keep working.
        sys.modules.pop("boto3", None)
        for k, v in saved.items():
            sys.modules[k] = v
        reset_default_store()
        get_settings.cache_clear()
