import glob
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from .config import SETTINGS
from .constants import GW_PATTERN

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")


@dataclass
class RAGDoc:
    path: str
    title: str
    text: str
    tokens: Set[str]
    meta: Dict[str, Any]


class RAGIndex:
    def __init__(
        self,
        reports_dir: str,
        summary_root: str,
        max_docs: int = 120,
        max_chars: int = 2000,
        refresh_interval: int = 60,
    ) -> None:
        self.reports_dir = reports_dir
        self.summary_root = summary_root
        self.max_docs = max_docs
        self.max_chars = max_chars
        self.refresh_interval = refresh_interval
        self._docs: List[RAGDoc] = []
        self._last_refresh = 0.0

    def _tokenize(self, text: str) -> Set[str]:
        return set(_TOKEN_RE.findall(text.lower()))

    def _read_text(self, path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read(self.max_chars + 1)
        except Exception:
            return ""
        if len(text) > self.max_chars:
            text = text[: self.max_chars] + "..."
        return text.strip()

    def _parse_meta(self, path: str) -> Dict[str, Any]:
        meta: Dict[str, Any] = {"path": path}
        norm = path.replace("\\", "/")
        report_match = re.search(r"reports/gw_(\d+)/([^/]+)\.md$", norm)
        if report_match:
            meta["gw"] = int(report_match.group(1))
            meta["type"] = report_match.group(2)
        summary_match = re.search(r"summary/([^/]+)/(\d+)/gw/(\d+)\.json$", norm)
        if summary_match:
            meta["type"] = summary_match.group(1)
            meta["league_id"] = int(summary_match.group(2))
            meta["gw"] = int(summary_match.group(3))
        return meta

    def _title_from_meta(self, meta: Dict[str, Any]) -> str:
        parts = []
        if meta.get("type"):
            parts.append(str(meta["type"]))
        if meta.get("gw") is not None:
            parts.append(f"GW{meta['gw']}")
        if meta.get("league_id"):
            parts.append(f"league {meta['league_id']}")
        return " ".join(parts) if parts else "cached_doc"

    def _collect_paths(self) -> List[str]:
        paths: List[str] = []
        paths.extend(glob.glob(os.path.join(self.reports_dir, "gw_*", "*.md")))
        for kind in ("league", "transactions", "standings", "lineup_efficiency", "matchup"):
            paths.extend(
                glob.glob(os.path.join(self.summary_root, kind, "*", "gw", "*.json"))
            )
        return paths

    def refresh(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last_refresh) < self.refresh_interval and self._docs:
            return
        paths = self._collect_paths()
        paths = sorted(paths, key=lambda p: os.path.getmtime(p), reverse=True)
        paths = paths[: self.max_docs]
        docs: List[RAGDoc] = []
        for path in paths:
            text = self._read_text(path)
            if not text:
                continue
            meta = self._parse_meta(path)
            title = self._title_from_meta(meta)
            docs.append(RAGDoc(path=path, title=title, text=text, tokens=self._tokenize(text), meta=meta))
        self._docs = docs
        self._last_refresh = now

    def _extract_gw(self, text: str) -> Optional[int]:
        match = GW_PATTERN.search(text)
        if match:
            return int(match.group(1))
        return None

    def search(self, query: str, k: int = 3) -> List[RAGDoc]:
        if not query:
            return []
        self.refresh()
        q_tokens = self._tokenize(query)
        q_gw = self._extract_gw(query)
        q_lower = query.lower()
        scored: List[tuple[int, RAGDoc]] = []
        for doc in self._docs:
            score = len(q_tokens & doc.tokens)
            if q_gw is not None and doc.meta.get("gw") == q_gw:
                score += 5
            doc_type = str(doc.meta.get("type") or "")
            if doc_type and doc_type in q_lower:
                score += 2
            if doc.meta.get("league_id") and str(doc.meta["league_id"]) in q_lower:
                score += 1
            if score > 0:
                scored.append((score, doc))
        scored.sort(key=lambda s: s[0], reverse=True)
        return [d for _, d in scored[:k]]


_INDEX: Optional[RAGIndex] = None


def get_rag_index() -> RAGIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = RAGIndex(
            reports_dir=SETTINGS.reports_dir,
            summary_root=os.path.join(SETTINGS.data_dir, "derived", "summary"),
        )
    return _INDEX


def format_rag_docs(docs: List[RAGDoc]) -> str:
    if not docs:
        return ""
    chunks: List[str] = []
    for doc in docs:
        chunks.append(f"[{doc.title}]\n{doc.text}")
    return "\n\n".join(chunks)
