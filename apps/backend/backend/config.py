from dataclasses import dataclass, field
import os

from dotenv import load_dotenv


def _walk_up(start: str) -> str | None:
    cur = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _resolve_repo_root() -> str:
    env_root = os.getenv("REPO_ROOT")
    if env_root:
        return os.path.abspath(env_root)
    for base in (os.getcwd(), os.path.dirname(__file__)):
        found = _walk_up(base)
        if found:
            return found
    return os.path.abspath(os.getcwd())


_INITIAL_ROOT = _resolve_repo_root()


def _resolve_dir(raw: str, repo_root: str) -> tuple[str, str]:
    value = (raw or "").strip() or "."
    if os.path.isabs(value):
        abs_path = os.path.abspath(value)
        if abs_path.startswith(repo_root + os.sep):
            rel_path = os.path.relpath(abs_path, repo_root)
        else:
            rel_path = abs_path
        return abs_path, rel_path
    abs_path = os.path.abspath(os.path.join(repo_root, value))
    return abs_path, value


def _require_int_env(key: str) -> int:
    """Read a required integer environment variable.

    Raises ValueError at startup if the variable is unset or not a valid integer,
    so misconfiguration is caught immediately rather than silently producing
    results for the wrong league or manager.
    """
    value = os.getenv(key, "").strip()
    if not value:
        raise ValueError(
            f"{key} environment variable is required but not set. "
            f"Add it to your .env file (e.g. {key}=12345)."
        )
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{key} must be an integer, got: {value!r}") from None


# Load .env from repo root if present
load_dotenv(os.path.join(_INITIAL_ROOT, ".env"))

REPO_ROOT = os.path.abspath(os.getenv("REPO_ROOT", _INITIAL_ROOT))
if REPO_ROOT != _INITIAL_ROOT:
    load_dotenv(os.path.join(REPO_ROOT, ".env"))

_REPORTS_DIR_RAW = os.getenv("REPORTS_DIR", "reports")
_REPORTS_DIR_ABS, _REPORTS_DIR_REL = _resolve_dir(_REPORTS_DIR_RAW, REPO_ROOT)
_DATA_DIR_ABS, _DATA_DIR_REL = _resolve_dir("data", REPO_ROOT)
_WEB_DIR_ABS, _WEB_DIR_REL = _resolve_dir("apps/web", REPO_ROOT)

@dataclass
class Settings:
    repo_root: str = REPO_ROOT
    mcp_url: str = os.getenv("MCP_URL", "http://localhost:8080/mcp")
    mcp_api_key: str = os.getenv("FPL_MCP_API_KEY", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1")
    reports_dir: str = _REPORTS_DIR_ABS
    reports_rel: str = _REPORTS_DIR_REL
    data_dir: str = _DATA_DIR_ABS
    data_rel: str = _DATA_DIR_REL
    web_dir: str = _WEB_DIR_ABS
    timezone: str = os.getenv("REPORTS_TZ", "America/New_York")
    league_id: int = field(default_factory=lambda: _require_int_env("LEAGUE_ID"))
    entry_id: int = field(default_factory=lambda: _require_int_env("ENTRY_ID"))
    start_go_server: bool = os.getenv("START_GO_SERVER", "true").lower() in ("1", "true", "yes")
    go_server_cmd: str = os.getenv("GO_SERVER_CMD", "go run ./apps/mcp-server/fpl-server --addr :8080 --path /mcp")
    refresh_cmd: str = os.getenv("CACHE_REFRESH_CMD", "")
    refresh_on_start: bool = os.getenv("CACHE_REFRESH_ON_START", "true").lower() in ("1", "true", "yes")
    refresh_daily: bool = os.getenv("CACHE_REFRESH_DAILY", "true").lower() in ("1", "true", "yes")
    refresh_time: str = os.getenv("CACHE_REFRESH_TIME", "19:00")
    refresh_fast: bool = os.getenv("CACHE_REFRESH_FAST", "true").lower() in ("1", "true", "yes")


SETTINGS = Settings()
