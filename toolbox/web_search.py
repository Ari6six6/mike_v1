"""Web search — Brave Search API (with key) or DuckDuckGo fallback.

Pass fetch_content=N to also retrieve and strip the full text of the top N
result pages inline, eliminating a separate fetch_url round-trip.
"""
from __future__ import annotations
TOOL_TAGS = ["model", "build"]

import json
import os
import re
from html.parser import HTMLParser

import httpx

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web and return the top results with titles, URLs, and snippets. "
            "Set fetch_content=N to also fetch and return the full readable text of the "
            "top N pages in one call — no separate fetch_url needed. "
            "Uses Brave Search API if BRAVE_API_KEY is set in env or config; "
            "otherwise falls back to DuckDuckGo. Auto-executes."
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
                    "description": "Number of results to return (default 8, max 20)",
                },
                "fetch_content": {
                    "type": "integer",
                    "description": (
                        "Also fetch and return the full page text for the top N results "
                        "(default 0 = snippets only). Use 1-3 when you need the actual "
                        "content of the pages, not just their titles and descriptions."
                    ),
                },
                "content_limit": {
                    "type": "integer",
                    "description": (
                        "Max characters of page text to include per result when "
                        "fetch_content > 0 (default 3000)"
                    ),
                },
            },
            "required": ["query"],
        },
    },
}

_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"


# ── HTML → plain text ─────────────────────────────────────────────────────────

def _html_to_text(html: str) -> str:
    # Drop script/style blocks wholesale
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # Drop all remaining tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Decode common entities
    for ent, ch in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                    ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(ent, ch)
    return re.sub(r"\s+", " ", text).strip()


def _fetch_page(url: str, char_limit: int) -> str:
    try:
        with httpx.Client(follow_redirects=True, timeout=10.0,
                          headers={"User-Agent": _UA}) as client:
            resp = client.get(url)
            resp.raise_for_status()
        text = _html_to_text(resp.text)
        return text[:char_limit] + ("…" if len(text) > char_limit else "")
    except Exception as exc:
        return f"[fetch failed: {exc}]"


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
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("description", ""),
        }
        for item in data.get("web", {}).get("results", [])[:max_results]
    ]


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
    with httpx.Client(follow_redirects=True, timeout=15.0) as client:
        resp = client.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": query},
            headers={
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
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
    try:
        with open(os.path.expanduser("~/.michael/config.json")) as f:
            return json.load(f).get("brave_api_key", "")
    except Exception:
        return ""


# ── Public entry point ────────────────────────────────────────────────────────

def web_search(
    query: str,
    max_results: int = 8,
    fetch_content: int = 0,
    content_limit: int = 3000,
) -> str:
    max_results = min(max(1, max_results), 20)
    fetch_content = min(max(0, fetch_content), max_results)
    content_limit = min(max(500, content_limit), 10000)

    lines = [f"=== web_search: {query!r} ===\n"]

    api_key = _get_brave_key()
    lines.append(f"[backend: {'Brave' if api_key else 'DuckDuckGo'}]\n")

    try:
        results = _brave_search(query, max_results, api_key) if api_key else _ddg_search(query, max_results)
    except Exception as exc:
        lines.append(f"ERROR: {exc}")
        return "\n".join(lines)

    if not results:
        lines.append("No results found.")
        return "\n".join(lines)

    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title', '(no title)')}")
        lines.append(f"   {r.get('url', '')}")
        snippet = r.get("snippet", "")
        if snippet:
            lines.append(f"   {snippet}")

        if fetch_content and i <= fetch_content:
            url = r.get("url", "")
            if url:
                lines.append(f"\n   [page content — {url}]")
                lines.append(f"   {_fetch_page(url, content_limit)}")

        lines.append("")

    return "\n".join(lines)
