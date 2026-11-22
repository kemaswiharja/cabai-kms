"""Web search fallback. Uses Tavily if TAVILY_API_KEY is set (free tier, more
reliable from cloud servers); otherwise DuckDuckGo via the open-source `ddgs`
package, which needs no key."""
import os

import httpx


def web_search(query: str, n=5):
    key = os.getenv("TAVILY_API_KEY")
    try:
        if key:
            r = httpx.post("https://api.tavily.com/search",
                           json={"api_key": key, "query": query, "max_results": n}, timeout=20)
            r.raise_for_status()
            return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
                    for x in r.json().get("results", [])]
        from ddgs import DDGS
        return [{"title": x.get("title", ""), "url": x.get("href", ""), "snippet": x.get("body", "")}
                for x in DDGS().text(query, max_results=n, region="id-id")]
    except Exception:  # noqa: BLE001 — web search is a best-effort fallback
        return []
