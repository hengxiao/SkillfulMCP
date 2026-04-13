import os
from functools import lru_cache


class Settings:
    jwt_secret: str
    jwt_issuer: str
    jwt_algorithm: str
    admin_key: str
    database_url: str
    rate_limit_per_minute: int
    max_request_body_mb: int

    def __init__(self) -> None:
        secret = os.environ.get("MCP_JWT_SECRET", "")
        if not secret:
            raise RuntimeError(
                "MCP_JWT_SECRET environment variable is required. "
                "Copy .env.example to .env and set a strong secret."
            )
        self.jwt_secret = secret
        self.jwt_issuer = os.environ.get("MCP_JWT_ISSUER", "mcp-server")
        self.jwt_algorithm = os.environ.get("MCP_JWT_ALGORITHM", "HS256")
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
