import json
from typing import Any, Dict, List

from .llm import LLMClient, try_parse_json
from .mcp_client import MCPClient


SYSTEM_PROMPT = """You are a data-accurate assistant. You MUST call tools for any factual data.
Return ONLY a JSON object in one of these forms:
1) {"action":"tool","name":"tool_name","arguments":{...}}
2) {"action":"final","content":"..."}
If you need more data, choose action=tool. Do not guess.
"""


class Agent:
    def __init__(self, mcp: MCPClient, llm: LLMClient) -> None:
        self.mcp = mcp
        self.llm = llm

    def run(self, user_message: str, max_steps: int = 4) -> Dict[str, Any]:
        tools = self.mcp.list_tools()
        tool_list = "\n".join([f"- {t.name}: {t.description}" for t in tools])
        context = f"Available tools:\n{tool_list}\n"
        messages: List[str] = [context, user_message]
        tool_events: List[Dict[str, Any]] = []

        if not self.llm.available():
            return {
                "content": "OPENAI_API_KEY not set; unable to use LLM. Please set it and retry.",
                "tool_events": tool_events,
            }

        for _ in range(max_steps):
            prompt = "\n\n".join(messages)
            raw = self.llm.generate(SYSTEM_PROMPT, prompt)
            data, ok = try_parse_json(raw)
            if not ok:
                return {"content": raw, "tool_events": tool_events}

            if data.get("action") == "final":
                return {"content": data.get("content", ""), "tool_events": tool_events}

            if data.get("action") == "tool":
                name = data.get("name", "")
                args = data.get("arguments", {})
                tool_events.append({"type": "tool_call", "name": name, "arguments": args})
                result = self.mcp.call_tool(name, args)
                tool_events.append({"type": "tool_result", "name": name, "result": result})
                messages.append(f"Tool {name} result:\n{json.dumps(result)[:6000]}")
                continue

            return {"content": raw, "tool_events": tool_events}

        return {"content": "Max steps reached.", "tool_events": tool_events}
