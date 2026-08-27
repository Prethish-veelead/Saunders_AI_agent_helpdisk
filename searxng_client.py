"""
Client for the self-hosted SearXNG instance (searxng-app/) used by
live_url_search.py for its live, per-question web search.

Replaces the free/unofficial DuckDuckGo scraper (ddgs) that proved
unreliable under real load — an identical query returned 5 results from a
local machine and 0 from the Azure Function's outbound IP moments apart,
and repeated heavy testing triggered extended DDG-side rate-limiting with
no way to work around it from our side. SearXNG is our own infrastructure:
no third-party rate limit to hit, and it aggregates several real search
engines (Brave, Google CSE, Wikipedia, ...) rather than depending on one
unofficial scraper.
"""

import logging
import os

import httpx

logger = logging.getLogger("searxng_client")

SEARXNG_URL = os.environ.get("SEARXNG_URL", "").rstrip("/")
SEARXNG_QUERY_TIMEOUT_SECONDS = int(os.environ.get("SEARXNG_QUERY_TIMEOUT_SECONDS", "10"))


async def searxng_search(query: str, timeout_seconds: int = SEARXNG_QUERY_TIMEOUT_SECONDS) -> list[dict]:
    """Queries the self-hosted SearXNG instance's JSON API and returns a
    normalized list of {"title", "url", "snippet"} dicts, highest-ranked
    first (SearXNG's own result order, already merged/scored across its
    upstream engines).

    Fail-soft, same contract as every other fetch function in this
    project: logs a warning and returns [] on any failure (missing
    config, timeout, non-200, malformed JSON) rather than raising — a bad
    search lookup should never crash the caller.
    """
    if not SEARXNG_URL:
        logger.warning("searxng_search: SEARXNG_URL is not configured, returning no results.")
        return []

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.get(f"{SEARXNG_URL}/search", params={"q": query, "format": "json"})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001 — a bad lookup means "no results", not a crash
        logger.warning("searxng_search query %r failed: %s: %s", query, type(exc).__name__, exc)
        return []

    results = data.get("results")
    if not isinstance(results, list):
        logger.warning("searxng_search query %r: response had no usable 'results' list.", query)
        return []

    normalized = []
    for r in results:
        if not isinstance(r, dict):
            continue
        url = r.get("url")
        if not url:
            continue
        normalized.append({
            "title": r.get("title") or "",
            "url": url,
            "snippet": r.get("content") or "",
        })
    return normalized
