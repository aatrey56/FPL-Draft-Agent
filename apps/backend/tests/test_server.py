"""Tests for backend.server — run_report input validation and _parse_chat_payload."""

import sys
import types
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Stub heavy optional dependencies before any backend module is imported.
# ---------------------------------------------------------------------------
for _mod in ("requests", "openai", "apscheduler", "apscheduler.schedulers.background",
             "apscheduler.triggers.cron"):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

_apscheduler_bg = sys.modules["apscheduler.schedulers.background"]
if not hasattr(_apscheduler_bg, "BackgroundScheduler"):
    _apscheduler_bg.BackgroundScheduler = MagicMock  # type: ignore[attr-defined]

_apscheduler_cron = sys.modules["apscheduler.triggers.cron"]
if not hasattr(_apscheduler_cron, "CronTrigger"):
    _apscheduler_cron.CronTrigger = MagicMock  # type: ignore[attr-defined]

sys.modules["openai"].OpenAI = MagicMock  # type: ignore[attr-defined]

# Provide a ZoneInfo stub (Python 3.9+ ships it, but ensure it exists).
if "zoneinfo" not in sys.modules:
    _zi = types.ModuleType("zoneinfo")
    _zi.ZoneInfo = MagicMock  # type: ignore[attr-defined]
    sys.modules["zoneinfo"] = _zi


def _identity_decorator(*args, **kwargs):
    """Return the decorated function unchanged — used to neuter FastAPI app.post/get/websocket."""
    def _decorator(fn):
        return fn
    return _decorator


# Build a minimal FastAPI stub that does NOT wrap decorated functions with MagicMock.
_fastapi_app_stub = MagicMock()
_fastapi_app_stub.post.side_effect = _identity_decorator
_fastapi_app_stub.get.side_effect = _identity_decorator
_fastapi_app_stub.websocket.side_effect = _identity_decorator
_fastapi_app_stub.add_middleware.return_value = None
_fastapi_app_stub.mount.return_value = None

_fastapi_cls = MagicMock(return_value=_fastapi_app_stub)

_fastapi_mod = types.ModuleType("fastapi")
_fastapi_mod.FastAPI = _fastapi_cls  # type: ignore[attr-defined]
_fastapi_mod.WebSocket = MagicMock  # type: ignore[attr-defined]

_fastapi_middleware = types.ModuleType("fastapi.middleware.cors")
_fastapi_middleware.CORSMiddleware = MagicMock  # type: ignore[attr-defined]

_fastapi_static = types.ModuleType("fastapi.staticfiles")
_fastapi_static.StaticFiles = MagicMock  # type: ignore[attr-defined]

_starlette_ws = types.ModuleType("starlette.websockets")
_starlette_ws.WebSocketDisconnect = Exception  # type: ignore[attr-defined]

for _key, _mod in [
    ("fastapi", _fastapi_mod),
    ("fastapi.middleware.cors", _fastapi_middleware),
    ("fastapi.staticfiles", _fastapi_static),
    ("starlette.websockets", _starlette_ws),
]:
    sys.modules[_key] = _mod  # type: ignore[assignment]


# Import with startup side-effects patched out.
with (
    patch("backend.server.ensure_go_server"),
    patch("backend.server.os.makedirs"),
):
    import backend.server as server_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_client(tool_response=None):
    """Return a minimal MCPClient mock."""
    client = MagicMock()
    client.call_tool.return_value = tool_response
    return client


# ---------------------------------------------------------------------------
# run_report — league_id / entry_id / gw input validation
# ---------------------------------------------------------------------------

class TestRunReportInputValidation:
    """Verify that non-integer API inputs are rejected gracefully (no 500 crash)."""

    def _call(self, payload):
        with (
            patch("backend.server._mcp", return_value=_make_mock_client()),
            patch("backend.server.LLMClient"),
        ):
            return server_module.run_report(payload)

    def test_missing_league_id_returns_error(self):
        resp = self._call({"type": "league_summary"})
        assert isinstance(resp, dict), f"Expected dict, got {type(resp)}"
        assert "error" in resp
        assert resp["error"] == "league_id is required"

    def test_nonnumeric_league_id_returns_error_not_500(self):
        """int('abc') must not bubble up as ValueError; must return JSON error dict."""
        resp = self._call({"type": "league_summary", "league_id": "abc"})
        assert isinstance(resp, dict), f"Expected dict, got {type(resp)}"
        assert "error" in resp
        assert "integer" in resp["error"].lower()

    def test_nonnumeric_entry_id_returns_error_not_500(self):
        resp = self._call({"type": "waivers", "league_id": "999", "entry_id": "not-a-number"})
        assert isinstance(resp, dict), f"Expected dict, got {type(resp)}"
        assert "error" in resp
        assert "integer" in resp["error"].lower()

    def test_valid_integer_strings_accepted(self):
        """String '123' should be accepted and converted to int without error."""
        with (
            patch("backend.server._mcp", return_value=_make_mock_client({"gameweek": 25})),
            patch("backend.server.LLMClient"),
            patch("backend.server.generate_league_summary", return_value={"md": "", "json": {}}),
            patch("backend.server.save_report", return_value={"md": "/tmp/r.md", "json": "/tmp/r.json"}),
        ):
            resp = server_module.run_report({"type": "league_summary", "league_id": "999", "gw": "10"})
        assert isinstance(resp, dict), f"Expected dict, got {type(resp)}"
        assert "error" not in resp
        assert resp.get("ok") is True

    def test_nonnumeric_gw_falls_back_to_zero_not_crash(self):
        """A non-numeric gw string should fall back to gw=0 (auto-detect), not crash."""
        with (
            patch("backend.server._mcp", return_value=_make_mock_client({"gameweek": 25})),
            patch("backend.server.LLMClient"),
            patch("backend.server.generate_league_summary", return_value={"md": "", "json": {}}),
            patch("backend.server.save_report", return_value={"md": "/tmp/r.md", "json": "/tmp/r.json"}),
        ):
            resp = server_module.run_report(
                {"type": "league_summary", "league_id": "999", "gw": "current"}
            )
        assert isinstance(resp, dict), f"Expected dict, got {type(resp)}"
        # Must not contain an "integer" error — gw falls back silently to 0.
        assert "integer" not in resp.get("error", "")

    def test_unknown_report_type_returns_error(self):
        resp = self._call({"type": "unknown_type", "league_id": "999"})
        assert isinstance(resp, dict), f"Expected dict, got {type(resp)}"
        assert "error" in resp
        assert "unknown" in resp["error"].lower()


# ---------------------------------------------------------------------------
# _parse_chat_payload
# ---------------------------------------------------------------------------

class TestParseChatPayload:
    def test_valid_json_with_message_key(self):
        result = server_module._parse_chat_payload('{"message": "hello", "session_id": "abc"}')
        assert result["message"] == "hello"
        assert result["session_id"] == "abc"

    def test_invalid_json_falls_back_to_raw(self):
        result = server_module._parse_chat_payload("not json at all")
        assert result == {"message": "not json at all"}

    def test_valid_json_without_message_key_falls_back_to_raw(self):
        result = server_module._parse_chat_payload('{"foo": "bar"}')
        assert result == {"message": '{"foo": "bar"}'}

    def test_empty_string_falls_back(self):
        result = server_module._parse_chat_payload("")
        assert result == {"message": ""}


# ---------------------------------------------------------------------------
# Refresh status + trigger endpoints
# ---------------------------------------------------------------------------

class TestRefreshEndpoints:
    """Verify GET /api/refresh/status and POST /api/refresh behaviour."""

    def setup_method(self):
        """Reset _REFRESH_STATUS to idle before each test."""
        server_module._REFRESH_STATUS.update({
            "state": "idle",
            "started_at": None,
            "last_completed": None,
            "last_error": None,
        })

    def test_status_returns_idle_initially(self):
        result = server_module.get_refresh_status()
        assert result["state"] == "idle"
        assert result["last_completed"] is None
        assert result["last_error"] is None

    def test_trigger_refresh_starts_thread_and_returns_ok(self):
        """POST /api/refresh must return ok=true when no refresh is running."""
        with patch("backend.server.run_startup_refresh") as mock_refresh:
            result = server_module.trigger_refresh()
        assert result["ok"] is True
        assert "started" in result["message"].lower()
        # run_startup_refresh is called in a daemon thread; give it a moment.
        import time
        time.sleep(0.05)
        mock_refresh.assert_called_once()

    def test_trigger_refresh_blocked_when_running(self):
        """POST /api/refresh must return ok=false when state is already 'running'."""
        server_module._REFRESH_STATUS["state"] = "running"
        result = server_module.trigger_refresh()
        assert result["ok"] is False
        assert "already" in result["message"].lower()
