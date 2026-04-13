import os
from functools import lru_cache


class Settings:
    jwt_secret: str
    jwt_keys_raw: str
    jwt_active_kid: str
    jwt_issuer: str
    jwt_algorithm: str
    max_token_lifetime_seconds: int
    admin_key: str
    database_url: str
    rate_limit_per_minute: int
    max_request_body_mb: int

    def __init__(self) -> None:
        # Either the legacy single-secret OR a multi-key JSON blob must be
        # present. keyring.build_keyring() will pick the right mode.
        secret = os.environ.get("MCP_JWT_SECRET", "")
        keys_raw = os.environ.get("MCP_JWT_KEYS", "").strip()
        if not secret and not keys_raw:
            raise RuntimeError(
                "Either MCP_JWT_SECRET or MCP_JWT_KEYS must be set. "
                "Copy .env.example to .env and set a strong secret."
            )
        self.jwt_secret = secret
        self.jwt_keys_raw = keys_raw
        # Active kid — only meaningful in multi-key mode. "primary" matches
        # the kid used for legacy-mode tokens so both modes can coexist
        # during rotation.
        self.jwt_active_kid = os.environ.get("MCP_JWT_ACTIVE_KID", "primary")
        self.jwt_issuer = os.environ.get("MCP_JWT_ISSUER", "mcp-server")
        self.jwt_algorithm = os.environ.get("MCP_JWT_ALGORITHM", "HS256")
        # Server-side ceiling on token lifetime. Clients requesting a longer
        # expires_in get clamped. Default 24h.
        self.max_token_lifetime_seconds = int(
            os.environ.get("MCP_MAX_TOKEN_LIFETIME_SECONDS", str(24 * 3600))
        )
        # If empty, admin key check is skipped (dev mode only)
        self.admin_key = os.environ.get("MCP_ADMIN_KEY", "")
        self.database_url = os.environ.get(
            "MCP_DATABASE_URL", "sqlite:///./skillful_mcp.db"
        )
        # Rate limit: requests per minute per client key (IP).
        # 0 or negative disables the limiter entirely.
        self.rate_limit_per_minute = int(
            os.environ.get("MCP_RATE_LIMIT_PER_MINUTE", "600")
        )
        # Request body cap (MB). The bundle endpoint has its own 100 MB
        # business-rule cap; this is the app-level safety net sitting
        # above it.
        self.max_request_body_mb = int(
            os.environ.get("MCP_MAX_REQUEST_BODY_MB", "101")
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
