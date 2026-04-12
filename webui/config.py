import os
from functools import lru_cache


class Settings:
    mcp_server_url: str
    admin_key: str
    host: str
    port: int

    def __init__(self) -> None:
        self.mcp_server_url = os.environ.get("MCP_SERVER_URL", "http://localhost:8000")
        self.admin_key = os.environ.get("MCP_ADMIN_KEY", "")
        self.host = os.environ.get("WEBUI_HOST", "127.0.0.1")
        self.port = int(os.environ.get("WEBUI_PORT", "8080"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
