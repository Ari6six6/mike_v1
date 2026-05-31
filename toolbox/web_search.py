"""Web search — Brave Search API (with key) or DuckDuckGo fallback."""
from __future__ import annotations
TOOL_TAGS = ["model", "build"]

import json
import os
from html.parser import HTMLParser

import httpx

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web and return the top results with titles, URLs, and text "
            "snippets. Uses Brave Search API if BRAVE_API_KEY is set in the environment "
            "or config; otherwise falls back to DuckDuckGo. Use for research, finding "
            "documentation, looking up APIs, or any web information gathering. "
            "Auto-executes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 8, max 20)",
                },
            },
            "required": ["query"],
        },
    },
}

_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"


# ── Brave Search ──────────────────────────────────────────────────────────────

def _brave_search(query: str, max_results: int, api_key: str) -> list[dict[str, str]]:
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(max_results, 20)},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
        )
        resp.raise_for_status()
    data = resp.json()
    results = []
    for item in data.get("web", {}).get("results", [])[:max_results]:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("description", ""),
        })
    return results


# ── DuckDuckGo Lite ───────────────────────────────────────────────────────────

class _DDGParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._cur: dict[str, str] = {}
        self._state = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        amap = dict(attrs)
        cls = amap.get("class") or ""
        if tag == "a" and "result-link" in cls:
            self._state = "title"
            self._cur = {"url": amap.get("href", ""), "title": "", "snippet": ""}
        elif tag == "td" and "result-snippet" in cls:
            self._state = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._state == "title":
            self._state = ""
        elif tag == "td" and self._state == "snippet":
            self._state = ""
            if self._cur.get("title"):
                self.results.append(dict(self._cur))
                self._cur = {}

    def handle_data(self, data: str) -> None:
        data = data.strip()
        if not data:
            return
        if self._state == "title":
            self._cur["title"] = (self._cur.get("title", "") + " " + data).strip()
        elif self._state == "snippet":
            self._cur["snippet"] = (self._cur.get("snippet", "") + " " + data).strip()


def _ddg_search(query: str, max_results: int) -> list[dict[str, str]]:
    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    with httpx.Client(follow_redirects=True, timeout=15.0) as client:
        resp = client.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": query},
            headers=headers,
        )
        resp.raise_for_status()
    parser = _DDGParser()
    parser.feed(resp.text)
    return parser.results[:max_results]


# ── Config key lookup ─────────────────────────────────────────────────────────

def _get_brave_key() -> str:
    key = os.environ.get("BRAVE_API_KEY", "")
    if key:
        return key
    # Try ~/.michael/config.json
    config_path = os.path.expanduser("~/.michael/config.json")
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        return cfg.get("brave_api_key", "")
    except Exception:
        return ""


# ── Public entry point ────────────────────────────────────────────────────────

def web_search(query: str, max_results: int = 8) -> str:
    max_results = min(max(1, max_results), 20)
    lines = [f"=== web_search: {query!r} ===\n"]

    api_key = _get_brave_key()
    backend = "Brave" if api_key else "DuckDuckGo"
    lines.append(f"[backend: {backend}]\n")

    try:
        if api_key:
            results = _brave_search(query, max_results, api_key)
        else:
            results = _ddg_search(query, max_results)

        if not results:
            lines.append("No results found.")
            return "\n".join(lines)

        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r.get('title', '(no title)')}")
            lines.append(f"   {r.get('url', '')}")
            snippet = r.get("snippet", "")
            if snippet:
                lines.append(f"   {snippet}")
            lines.append("")

    except Exception as exc:
        lines.append(f"ERROR: {exc}")

    return "\n".join(lines)
