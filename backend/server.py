import asyncio
import json
from typing import Any, Dict

import os
from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

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

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/reports", StaticFiles(directory=SETTINGS.reports_dir), name="reports")
app.mount("/ui", StaticFiles(directory="web", html=True), name="web")


def _mcp() -> MCPClient:
    return MCPClient(SETTINGS.mcp_url, SETTINGS.mcp_api_key)


@app.on_event("startup")
def _startup() -> None:
    ensure_go_server()


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> Dict[str, str]:
    return {"ui": "/ui", "reports": "/reports"}


@app.get("/tools")
def tools() -> Dict[str, Any]:
    client = _mcp()
    return {"tools": [t.__dict__ for t in client.list_tools()]}


@app.post("/chat")
def chat(payload: Dict[str, Any]) -> Dict[str, Any]:
    user_message = payload.get("message", "")
    client = _mcp()
    llm = LLMClient()
    agent = Agent(client, llm)
    result = agent.run(user_message)
    return result


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    client = _mcp()
    llm = LLMClient()
    agent = Agent(client, llm)
    try:
        while True:
            msg = await ws.receive_text()
            await ws.send_text(json.dumps({"type": "user", "message": msg}))
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, agent.run, msg)
            for ev in result.get("tool_events", []):
                await ws.send_text(json.dumps({"type": ev["type"], **ev}))
            await ws.send_text(json.dumps({"type": "final", "content": result.get("content", "")}))
    except WebSocketDisconnect:
        return


def _current_gw(client: MCPClient) -> int:
    summary = client.call_tool("league_summary", {"league_id": 14204, "gw": 0})
    return int(summary.get("gameweek", 0) or 0)


@app.post("/api/reports/run")
def run_report(payload: Dict[str, Any]) -> Dict[str, Any]:
    client = _mcp()
    llm = LLMClient()
    report_type = payload.get("type")
    league_id = int(payload.get("league_id", 14204))
    entry_id = int(payload.get("entry_id", 286192))
    gw = int(payload.get("gw", 0))
    if gw == 0:
        cur = _current_gw(client)
        if report_type == "waivers":
            gw = cur + 1
        else:
            gw = cur

    try:
        if report_type == "waivers":
            content = generate_waiver_report(client, llm, league_id, entry_id, gw)
            paths = save_report(gw, "waiver_recommendations", content)
        elif report_type == "league_summary":
            content = generate_league_summary(client, llm, league_id, gw)
            paths = save_report(gw, "league_summary", content)
        elif report_type == "trades":
            content = generate_trades_report(client, llm, league_id, gw)
            paths = save_report(gw, "trades_summary", content)
        elif report_type == "starting_xi":
            content = generate_starting_xi_report(client, llm, league_id, entry_id, gw)
            paths = save_report(gw, "starting_xi", content)
        else:
            return {"error": "unknown report type"}
    except Exception as exc:
        return {"error": str(exc)}

    return {"ok": True, "paths": paths}
