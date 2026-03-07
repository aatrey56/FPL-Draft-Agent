"""Tests for backend.mcp_client — session ID validation."""

import sys
import types
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub heavy optional dependencies before any backend module is imported.
# ---------------------------------------------------------------------------
for _mod in ("openai", "apscheduler", "apscheduler.schedulers.background",
             "apscheduler.triggers.cron"):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

# Stub requests.Session so MCPClient can be imported without a real HTTP library.
# Other test files may have already stubbed the module without a Session attribute;
# add it if missing regardless of whether the module was just created or pre-exists.
if "requests" not in sys.modules:
    _requests_mod = types.ModuleType("requests")
    sys.modules["requests"] = _requests_mod
if not hasattr(sys.modules["requests"], "Session"):
    sys.modules["requests"].Session = MagicMock  # type: ignore[attr-defined]

_apscheduler_bg = sys.modules["apscheduler.schedulers.background"]
if not hasattr(_apscheduler_bg, "BackgroundScheduler"):
    _apscheduler_bg.BackgroundScheduler = MagicMock  # type: ignore[attr-defined]

_apscheduler_cron = sys.modules["apscheduler.triggers.cron"]
if not hasattr(_apscheduler_cron, "CronTrigger"):
    _apscheduler_cron.CronTrigger = MagicMock  # type: ignore[attr-defined]

sys.modules["openai"].OpenAI = MagicMock  # type: ignore[attr-defined]

if "zoneinfo" not in sys.modules:
    _zi = types.ModuleType("zoneinfo")
    _zi.ZoneInfo = MagicMock  # type: ignore[attr-defined]
    sys.modules["zoneinfo"] = _zi

import pytest  # noqa: E402
from backend.mcp_client import MCPClient  # noqa: E402


def _mock_session(session_id_header: str | None, status_code: int = 200):
    """Return a requests.Session mock whose POST returns the given session ID header."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status.return_value = None
    resp.headers = {}
    if session_id_header is not None:
        resp.headers["Mcp-Session-Id"] = session_id_header

    session = MagicMock()
    session.post.return_value = resp
    return session


class TestEnsureSession:
    """Verify MCPClient.ensure_session() validates the returned session ID."""

    def test_valid_session_id_stored(self):
        """When the server returns a valid session ID it should be stored."""
        client = MCPClient("http://localhost:8080/mcp", "")
        client._session = _mock_session("sess-abc-123")
        client.ensure_session()
        assert client.session_id == "sess-abc-123"

    def test_missing_session_id_raises_runtime_error(self):
        """If the server omits the Mcp-Session-Id header, a RuntimeError must be raised
        rather than silently setting session_id to None and continuing.
        Before the fix, ensure_session() would store None and subsequent calls
        would send malformed headers."""
        client = MCPClient("http://localhost:8080/mcp", "")
        client._session = _mock_session(None)  # header absent
        with pytest.raises(RuntimeError, match="Mcp-Session-Id"):
            client.ensure_session()

    def test_already_has_session_skips_init(self):
        """If session_id is already set, ensure_session() must be a no-op."""
        client = MCPClient("http://localhost:8080/mcp", "")
        client.session_id = "existing"
        mock_session = MagicMock()
        client._session = mock_session
        client.ensure_session()
        mock_session.post.assert_not_called()
