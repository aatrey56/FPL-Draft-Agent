import json
from typing import Any, Dict, List, Tuple

from openai import OpenAI

from .config import SETTINGS


def _extract_text(resp: Any) -> str:
    if hasattr(resp, "output_text"):
        return resp.output_text
    if hasattr(resp, "output"):
        texts = []
        for item in resp.output:
            if getattr(item, "type", "") == "message":
                for c in item.content:
                    if getattr(c, "type", "") in ("output_text", "text"):
                        texts.append(getattr(c, "text", ""))
        return "\n".join(texts).strip()
    return ""


class LLMClient:
    def __init__(self) -> None:
        if not SETTINGS.openai_api_key:
            self.client = None
        else:
            self.client = OpenAI(api_key=SETTINGS.openai_api_key)

    def available(self) -> bool:
        return self.client is not None

    def generate(self, system: str, user: str) -> str:
        if not self.client:
            return ""
        # Support both new Responses API and older Chat Completions.
        if hasattr(self.client, "responses"):
            resp = self.client.responses.create(
                model=SETTINGS.openai_model,
                instructions=system,
                input=user,
            )
            return _extract_text(resp)

        resp = self.client.chat.completions.create(
            model=SETTINGS.openai_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""


def try_parse_json(text: str) -> Tuple[Dict[str, Any], bool]:
    try:
        return json.loads(text), True
    except Exception:
        return {}, False
