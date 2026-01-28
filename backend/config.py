import os
from dataclasses import dataclass


@dataclass
class Settings:
    mcp_url: str = os.getenv("MCP_URL", "http://localhost:8080/mcp")
    mcp_api_key: str = os.getenv("FPL_MCP_API_KEY", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1")
    reports_dir: str = os.getenv("REPORTS_DIR", "reports")
    timezone: str = os.getenv("REPORTS_TZ", "America/New_York")
    start_go_server: bool = os.getenv("START_GO_SERVER", "true").lower() in ("1", "true", "yes")
    go_server_cmd: str = os.getenv("GO_SERVER_CMD", "go run ./fpl-server --addr :8080 --path /mcp")


SETTINGS = Settings()
