import json
import os
from functools import lru_cache


class Settings:
    mcp_server_url: str
    admin_key: str
    host: str
    port: int
    # Wave 6a — operator auth
    session_secret: str
    operators_raw: str
    csrf_enabled: bool

    def __init__(self) -> None:
        self.mcp_server_url = os.environ.get("MCP_SERVER_URL", "http://localhost:8000")
        self.admin_key = os.environ.get("MCP_ADMIN_KEY", "")
        self.host = os.environ.get("WEBUI_HOST", "127.0.0.1")
        self.port = int(os.environ.get("WEBUI_PORT", "8080"))

        # Session secret for the signed cookie. A stable per-deployment
        # value; rotating it invalidates every active session.
        self.session_secret = os.environ.get(
            "MCP_WEBUI_SESSION_SECRET", ""
        )
        # JSON operator list. Example:
        #   MCP_WEBUI_OPERATORS='[{"email":"alice@example.com","password_hash":"$2b$..."}]'
        self.operators_raw = os.environ.get("MCP_WEBUI_OPERATORS", "").strip()
        # Allow tests to build an app without going through login on every
        # request — the integration test suite uses a real login through the
        # HTTP path, but narrow unit tests can turn this off.
        self.csrf_enabled = os.environ.get(
            "MCP_WEBUI_CSRF_ENABLED", "1"
        ).lower() not in ("0", "false", "no")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def parse_operators(raw: str) -> dict[str, str]:
    """Parse the operator list JSON into `{email: password_hash}`.

    Format:
        [{"email": "...", "password_hash": "$2b$..."}, ...]

    An empty / missing string returns `{}` — the app still starts but every
    login attempt fails.
    """
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, list):
        raise RuntimeError("MCP_WEBUI_OPERATORS must be a JSON list")
    out: dict[str, str] = {}
    for entry in data:
        if not isinstance(entry, dict):
            raise RuntimeError("each operator entry must be an object")
        email = str(entry.get("email", "")).strip().lower()
        pwhash = str(entry.get("password_hash", ""))
        if not email or not pwhash:
            raise RuntimeError("operator entry requires email + password_hash")
        out[email] = pwhash
    return out
