import asyncio
import json
import os
import shlex
import subprocess
import threading
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from zoneinfo import ZoneInfo

from .agent import Agent
from .config import SETTINGS
from .llm import LLMClient
from .mcp_client import MCPClient, ensure_go_server
from .reports import (
    generate_league_summary,
    generate_starting_xi_report,
    generate_trades_report,
    generate_waiver_report,
    save_report,
)

os.makedirs(SETTINGS.reports_dir, exist_ok=True)
_CACHE_SCHEDULER: Optional[BackgroundScheduler] = None
_CHAT_SESSIONS: Dict[str, Agent] = {}

@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:  # type: ignore[misc]
    """FastAPI lifespan handler: start services on startup, clean up on shutdown.

    Replaces the deprecated ``@app.on_event("startup"/"shutdown")`` decorators.
    """
    # ── Startup ────────────────────────────────────────────────────────────────
    ensure_go_server()
    _start_cache_scheduler()
    if SETTINGS.refresh_on_start:
        threading.Thread(target=run_cache_refresh, daemon=True).start()

    yield  # application runs here

    # ── Shutdown ───────────────────────────────────────────────────────────────
    global _CACHE_SCHEDULER
    if _CACHE_SCHEDULER:
        _CACHE_SCHEDULER.shutdown()
        _CACHE_SCHEDULER = None


app = FastAPI(lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/reports", StaticFiles(directory=SETTINGS.reports_dir), name="reports")
app.mount("/ui", StaticFiles(directory=SETTINGS.web_dir, html=True), name="web")


def _mcp() -> MCPClient:
    return MCPClient(SETTINGS.mcp_url, SETTINGS.mcp_api_key)


def _cache_refresh_cmd() -> List[str]:
    if SETTINGS.refresh_cmd:
        return shlex.split(SETTINGS.refresh_cmd)
    cmd = f"go run ./apps/mcp-server/cmd/dev --refresh=scheduled --refresh-now --league {SETTINGS.league_id}"
    if SETTINGS.refresh_fast:
        cmd += " --fast"
    return shlex.split(cmd)


def run_cache_refresh() -> None:
    cmd = _cache_refresh_cmd()
    try:
        proc = subprocess.run(
            cmd,
            cwd=SETTINGS.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            print("[cache-refresh] failed", proc.returncode)
            if proc.stdout:
                print(proc.stdout)
            if proc.stderr:
                print(proc.stderr)
        else:
            print("[cache-refresh] complete")
            if proc.stdout:
                print(proc.stdout)
    except Exception as exc:
        print(f"[cache-refresh] error: {exc}")


def _parse_refresh_time() -> tuple[int, int]:
    raw = (SETTINGS.refresh_time or "19:00").strip()
    try:
        hour_str, minute_str = raw.split(":", 1)
        return int(hour_str), int(minute_str)
    except ValueError:
        return 19, 0


def _start_cache_scheduler() -> None:
    global _CACHE_SCHEDULER
    if not SETTINGS.refresh_daily:
        return
    if _CACHE_SCHEDULER:
        return
    hour, minute = _parse_refresh_time()
    tz = ZoneInfo(SETTINGS.timezone)
    scheduler = BackgroundScheduler(timezone=tz)
    scheduler.add_job(run_cache_refresh, CronTrigger(hour=hour, minute=minute))
    scheduler.start()
    _CACHE_SCHEDULER = scheduler


def _parse_chat_payload(raw: str) -> Dict[str, Any]:
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "message" in data:
            return data
    except Exception:
        pass
    return {"message": raw}



@app.get("/health")
def health() -> Dict[str, str]:
    """Liveness probe endpoint.  Returns ``{"status": "ok"}`` when the server is up."""
    return {"status": "ok"}


@app.get("/")
def root() -> Dict[str, str]:
    """Root endpoint — returns links to the UI and reports API."""
    return {"ui": "/ui", "reports": "/reports"}


@app.get("/tools")
def tools() -> Dict[str, Any]:
    """List all MCP tools exposed by the Go server.

    Returns:
        Dict with a ``"tools"`` list where each item is the serialised
        representation of an MCP tool (name, description, input schema).
    """
    client = _mcp()
    return {"tools": [t.__dict__ for t in client.list_tools()]}


@app.get("/capabilities")
def capabilities() -> Dict[str, Any]:
    """Return server capabilities metadata.

    Describes what tools are available, which scheduled reports are generated,
    and whether real-time data access is supported.

    Returns:
        Dict with ``"realtime"``, ``"data_source"``, ``"tools"``, ``"reports"``,
        and ``"note"`` keys.
    """
    client = _mcp()
    tools = [t.__dict__ for t in client.list_tools()]
    return {
        "realtime": False,
        "data_source": "local_cache",
        "tools": tools,
        "reports": [
            {"name": "waiver_recommendations", "description": "Personalized waiver adds + drops", "files": ["waiver_recommendations.md", "waiver_recommendations.json"]},
            {"name": "league_summary", "description": "Weekly league recap", "files": ["league_summary.md", "league_summary.json"]},
            {"name": "transactions", "description": "Trades/waivers summary", "files": ["trades_summary.md", "trades_summary.json"]},
            {"name": "starting_xi", "description": "Fixture-only starting XI", "files": ["starting_xi.md", "starting_xi.json"]},
        ],
        "note": "Set realtime=true only if the MCP server is modified to fetch live data directly; currently it reads data/raw and data/derived.",
    }


@app.post("/chat")
def chat(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Main chat endpoint.

    Accepts a JSON payload with ``"message"`` (required) and an optional
    ``"session_id"`` to continue an existing conversation.  Maintains one
    :class:`Agent` instance per session so context is preserved across turns.

    Args (in JSON body):
        message:    The user's natural-language query.
        session_id: Optional session identifier.  A new UUID is created if
                    omitted or unknown.
        *rest*:     Any additional keys are forwarded as session context to
                    ``Agent.run`` (e.g. ``league_id``, ``entry_id``, ``gw``).

    Returns:
        Dict with ``"content"`` (reply text), ``"tool_events"`` (call log),
        and ``"session_id"`` (the session identifier for subsequent requests).
    """
    user_message = payload.get("message", "")
    session_id = str(payload.get("session_id") or "").strip()
    agent = _CHAT_SESSIONS.get(session_id) if session_id else None
    if agent is None:
        client = _mcp()
        llm = LLMClient()
        agent = Agent(client, llm)
        session_id = session_id or str(uuid.uuid4())
        _CHAT_SESSIONS[session_id] = agent
    result = agent.run(user_message, context=payload)
    result["session_id"] = session_id
    return result


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """WebSocket chat endpoint.

    Accepts a WebSocket connection at ``/ws``.  Each received JSON message
    should have the same shape as the ``/chat`` POST body.  Replies are sent
    back as JSON objects with the same structure as the ``/chat`` response.
    """
    await ws.accept()
    client = _mcp()
    llm = LLMClient()
    agent = Agent(client, llm)
    try:
        while True:
            raw = await ws.receive_text()
            payload = _parse_chat_payload(raw)
            msg = payload.get("message", "")
            await ws.send_text(json.dumps({"type": "user", "message": msg}))
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, agent.run, msg, 4, payload)
            for ev in result.get("tool_events", []):
                await ws.send_text(json.dumps({"type": ev["type"], **ev}))
            await ws.send_text(json.dumps({"type": "final", "content": result.get("content", "")}))
    except WebSocketDisconnect:
        return


def _current_gw(client: MCPClient, league_id: int) -> int:
    summary = client.call_tool("league_summary", {"league_id": league_id, "gw": 0})
    if not isinstance(summary, dict):
        return 0
    return int(summary.get("gameweek", 0) or 0)


def _resolve_entry_id(client: MCPClient, league_id: int, entry_id: int, entry_name: str) -> int:
    if entry_id:
        return entry_id
    name = (entry_name or "").strip()
    if not name:
        return 0
    data = client.call_tool("league_entries", {"league_id": league_id})
    if not isinstance(data, dict):
        return 0
    matches = []
    for t in data.get("teams", []):
        entry_name_val = t.get("entry_name") or ""
        short_name = t.get("short_name") or ""
        if entry_name_val.lower() == name.lower() or short_name.lower() == name.lower():
            matches.append(t)
    if len(matches) == 1:
        return int(matches[0].get("entry_id", 0) or 0)
    return 0


@app.post("/api/reports/run")
def run_report(payload: Dict[str, Any]) -> Dict[str, Any]:
    client = _mcp()
    llm = LLMClient()
    report_type = payload.get("type")
    league_id_raw = payload.get("league_id")
    entry_id_raw = payload.get("entry_id")
    entry_name = payload.get("entry_name", "")
    league_id = int(league_id_raw) if league_id_raw not in (None, "") else 0
    entry_id = int(entry_id_raw) if entry_id_raw not in (None, "") else 0
    if league_id == 0:
        return {"error": "league_id is required"}
    if entry_id == 0 and entry_name:
        entry_id = _resolve_entry_id(client, league_id, entry_id, entry_name)
    gw = int(payload.get("gw", 0))
    waiver_weights = payload.get("waiver_weights") or {}
    xi_weights = payload.get("xi_weights") or {}
    if gw == 0:
        cur = _current_gw(client, league_id)
        if report_type == "waivers":
            gw = cur + 1
        else:
            gw = cur

    try:
        if report_type == "waivers":
            if entry_id == 0:
                return {"error": "entry_id or entry_name is required for waivers"}
            content = generate_waiver_report(client, llm, league_id, entry_id, gw, waiver_weights)
            paths = save_report(gw, "waiver_recommendations", content)
        elif report_type == "league_summary":
            content = generate_league_summary(client, llm, league_id, gw)
            paths = save_report(gw, "league_summary", content)
        elif report_type == "trades":
            content = generate_trades_report(client, llm, league_id, gw)
            paths = save_report(gw, "trades_summary", content)
        elif report_type == "starting_xi":
            if entry_id == 0:
                return {"error": "entry_id or entry_name is required for starting_xi"}
            content = generate_starting_xi_report(client, llm, league_id, entry_id, gw, xi_weights)
            paths = save_report(gw, "starting_xi", content)
        else:
            return {"error": "unknown report type"}
    except Exception as exc:
        return {"error": str(exc)}

    return {"ok": True, "paths": paths}
