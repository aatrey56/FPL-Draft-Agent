import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

from .config import SETTINGS


@dataclass
class MCPTool:
    name: str
    description: str


class MCPClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.session_id: Optional[str] = None
        self._session = requests.Session()

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def ensure_session(self) -> None:
        if self.session_id:
            return
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "python-backend", "version": "1.0"},
            },
        }
        resp = self._session.post(self.base_url, headers=self._headers(), data=json.dumps(payload))
        resp.raise_for_status()
        self.session_id = resp.headers.get("Mcp-Session-Id")
        # notify initialized
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self._session.post(self.base_url, headers=self._headers(), data=json.dumps(notif))

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        self.ensure_session()
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        resp = self._session.post(self.base_url, headers=self._headers(), data=json.dumps(payload))
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(data["error"])
        content = data.get("result", {}).get("content", [])
        if not content:
            return None
        # tool responses are JSON strings
        text = content[0].get("text", "")
        if isinstance(text, str) and text.lower().startswith("error:"):
            raise RuntimeError(text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def list_tools(self) -> List[MCPTool]:
        url = self.base_url.replace("/mcp", "/tools")
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        resp = self._session.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        tools = resp.json().get("tools", [])
        return [MCPTool(name=t["name"], description=t["description"]) for t in tools]


_GO_PROCESS: Optional[subprocess.Popen] = None


def ensure_go_server() -> None:
    global _GO_PROCESS
    if not SETTINGS.start_go_server:
        return
    if is_server_healthy():
        return
    env = os.environ.copy()
    if SETTINGS.mcp_api_key:
        env["FPL_MCP_API_KEY"] = SETTINGS.mcp_api_key
    cmd = SETTINGS.go_server_cmd.split()
    _GO_PROCESS = subprocess.Popen(
        cmd,
        env=env,
        cwd=SETTINGS.repo_root,
        stdout=None,
        stderr=None,
    )
    # wait for health
    for _ in range(20):
        if is_server_healthy():
            return
        time.sleep(0.5)
    raise RuntimeError("Go MCP server failed to start or become healthy.")


def is_server_healthy() -> bool:
    health_url = SETTINGS.mcp_url.replace("/mcp", "/health")
    headers = {"X-API-Key": SETTINGS.mcp_api_key} if SETTINGS.mcp_api_key else {}
    try:
        resp = requests.get(health_url, headers=headers, timeout=2)
        return resp.status_code == 200
    except Exception:
        return False
