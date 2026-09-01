"""
Live, per-question URL search for helpdesk questions — direct-crawl
approach.
------------------------------------------------------------------------

Replaces the SearXNG-backed search-then-select flow (live_url_search.py —
kept in the repo but no longer called from helpdesk_answer.py, per an
explicit "don't delete, just stop using" request) with a simpler design
that needs no external search engine at all: crawl ONLY the tracked seed
URLs (from the SharePoint List) and their own same-domain sub-links,
fuzzy-rank all of them by title against the question, fetch the real
content of the best match(es), and generate the answer strictly from that
fetched content — via the SAME generate_structured_response() used for
tickets/library docs, so this path gets the same citation-based source
attribution and answerable-follow-up guarantees, rather than a second,
separate answering mechanism.

Adapted from a prototype (see test_bing_grounded_search/crawl_search.py)
built specifically because DuckDuckGo (ddgs) and then a self-hosted
SearXNG instance both proved unreliable under real load — either one can
rate-limit or block us, since both depend on a third-party search engine.
This design has no such dependency: it only ever talks to the tracked
sites directly over plain HTTP.

Every step is designed to fail soft — a bad fetch, a dead link, a
malformed AI response — falls through to "nothing found here" rather than
raising, since this whole module is a bonus path ahead of the reliable
index fallback (see helpdesk_answer.answer_question()).
"""

import asyncio
import logging
import os
import re
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from sharepoint_client import get_url_candidates, SharePointConfigError, GraphAPIError

logger = logging.getLogger("crawl_url_search")

REQUEST_TIMEOUT_SECONDS = int(os.environ.get("CRAWL_REQUEST_TIMEOUT_SECONDS", "10"))
# Sub-links only ever contribute cheap signal (their anchor text) at the
# pool-building stage — real content is fetched later, only for whichever
# candidates actually rank highest — so a generous cap here costs little.
MAX_SUBLINKS_PER_SEED = int(os.environ.get("CRAWL_MAX_SUBLINKS_PER_SEED", "30"))
MAX_CONTENT_CHARS = int(os.environ.get("CRAWL_MAX_CONTENT_CHARS", "6000"))
MAX_PAGES_FOR_ANSWER = int(os.environ.get("CRAWL_MAX_PAGES_FOR_ANSWER", "2"))
# A self-identifying bot UA gets hard-blocked (403) by some tracked sites'
# bot protection (confirmed live against Lenovo's Akamai-fronted site) even
# though the same request succeeds instantly with a normal browser UA and
# returns genuine, useful page content — not a JS-rendering issue, purely
# UA-based blocking. Only the tracked URLs the client has explicitly
# configured are ever fetched, at low volume, so presenting as a normal
# browser here is standard, low-risk practice rather than broad scraping.
USER_AGENT = os.environ.get(
    "CRAWL_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)

# Social-media/navigation-utility domains and paths — same list
# live_url_search.py uses, kept in sync for the same reason: their short,
# generic anchor text ("Search", "Cart", "Facebook") can otherwise score
# deceptively close to genuinely relevant pages under fuzzy matching.
_UTILITY_DOMAINS = {
    "facebook.com", "www.facebook.com", "instagram.com", "www.instagram.com",
    "youtube.com", "www.youtube.com", "twitter.com", "www.twitter.com", "x.com",
    "linkedin.com", "www.linkedin.com", "pinterest.com", "www.pinterest.com",
    "wa.me", "t.me",
}
_UTILITY_PATH_PREFIXES = ("/cart", "/search", "/account", "/login", "/checkout")


def _is_utility_link(url: str) -> bool:
    """Filters out navigation/admin chrome and social-media links: any URL
    with a query string (?action=..., ?ref=...), or whose last path
    segment contains a colon (MediaWiki's own convention for Special:,
    Talk:, Category: pages — found via live testing against Wikipedia: a
    bare category-listing page's own sub-links pulled in a Talk page and a
    raw edit-action URL as if they were real article content), or a known
    social-media domain / utility path — these otherwise pollute the
    candidate pool with generic titles that can out-rank real content.
    """
    parts = urlsplit(url)
    if parts.query:
        return True
    last_segment = parts.path.rsplit("/", 1)[-1]
    if ":" in last_segment:
        return True
    if parts.netloc.lower() in _UTILITY_DOMAINS:
        return True
    path = parts.path.lower().rstrip("/")
    if any(path == prefix or path.startswith(prefix + "/") for prefix in _UTILITY_PATH_PREFIXES):
        return True
    return False


def _strip_fragment(url: str) -> str:
    """The canonical (fragment-free) form of a URL — several distinct
    #anchors can point at the same actual page, and a fragment is never
    sent to the server anyway.
    """
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


_FETCH_HEADERS = {
    "User-Agent": USER_AGENT,
    # A browser User-Agent alone is not enough — confirmed live against
    # Lenovo's Akamai-fronted site: identical requests with only User-Agent
    # set got 403 Access Denied every time, while adding these two (which
    # any real browser always sends) got 200 every time. Bot detection here
    # checks for a complete, realistic header set, not just the UA string.
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_html(url: str) -> str:
    resp = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS, headers=_FETCH_HEADERS)
    resp.raise_for_status()
    return resp.text


def extract_title(html: str, fallback: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    return fallback


def extract_links(html: str, base_url: str) -> list[dict]:
    """Same-domain links only — this is what keeps the crawl inside the
    tracked site instead of wandering off across the open web. Runs every
    link through _is_utility_link before keeping it.
    """
    soup = BeautifulSoup(html, "html.parser")
    base_domain = urlsplit(base_url).netloc
    base_canonical = _strip_fragment(base_url)
    seen: set[str] = set()
    links: list[dict] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:", "data:")):
            continue
        full_url = _strip_fragment(urljoin(base_url, href))
        if urlsplit(full_url).netloc != base_domain:
            continue
        if full_url in seen or full_url == base_canonical or _is_utility_link(full_url):
            continue
        seen.add(full_url)
        links.append({"url": full_url, "link_text": a.get_text(strip=True)})
    return links


def extract_text(html: str, max_chars: int = MAX_CONTENT_CHARS) -> str:
    """Deliberately crude — strips nav/script/style chrome, collapses
    whitespace, then truncates. A blind character cutoff can cut a page
    off mid-sentence; MAX_CONTENT_CHARS is generous enough in practice
    that this hasn't been observed to matter, but a real chunking pass
    (chunking.py) would be a more correct choice if it ever does.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:max_chars]


def _fetch_page(url: str) -> Optional[dict]:
    """Fetches one URL and returns {"url", "title", "html"} — None on any
    failure (dead link, timeout, non-2xx), never raises, matching this
    project's fail-soft convention for network calls.
    """
    try:
        html = fetch_html(url)
    except Exception:  # noqa: BLE001 — a bad seed/candidate must not crash the whole question
        logger.warning("Failed to fetch %s", url, exc_info=True)
        return None
    return {"url": url, "title": extract_title(html, url), "html": html}


async def build_candidate_pool(seed_urls: list[str], max_sublinks_per_seed: int = MAX_SUBLINKS_PER_SEED) -> list[dict]:
    """Fetches every seed URL concurrently, then lists each seed itself
    plus its own same-domain sub-links as candidates — the full pool this
    question is allowed to consider. A seed that fails to fetch is simply
    skipped, not fatal to the others.
    """
    fetched = await asyncio.gather(*[asyncio.to_thread(_fetch_page, url) for url in seed_urls])

    candidates: list[dict] = []
    seen_urls: set[str] = set()
    for seed_url, page in zip(seed_urls, fetched):
        if not page:
            continue
        canonical_seed = _strip_fragment(seed_url)
        if canonical_seed not in seen_urls:
            candidates.append({"url": canonical_seed, "title": page["title"], "seed": seed_url})
            seen_urls.add(canonical_seed)

        for link in extract_links(page["html"], seed_url)[:max_sublinks_per_seed]:
            if link["url"] in seen_urls:
                continue
            seen_urls.add(link["url"])
            candidates.append({
                "url": link["url"],
                "title": link["link_text"] or link["url"],
                "seed": seed_url,
            })

    return candidates


def _normalize_for_matching(text: str) -> str:
    """Collapses any run of non-alphanumeric characters into a single
    space before fuzzy scoring. Without this, a short, specific title like
    "Lenovo" scores WORSE against a filler-word-heavy question ("tell me
    about lenovo laptops") than an unrelated title like "Computer data
    storage" — confirmed directly: 29.4 vs 36.7 unnormalized, 100.0 vs
    36.7 after normalizing. Same fix already proven in live_url_search.py
    for the same underlying rapidfuzz behavior; ported here rather than
    re-discovering it the hard way in production.
    """
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def rank_candidates(question: str, candidates: list[dict], top_n: int = MAX_PAGES_FOR_ANSWER) -> list[tuple[float, dict]]:
    """Scores each candidate's title against the question — the single
    most important signal in this pipeline, since it's what decides which
    page(s) actually get fetched and answered from.
    """
    normalized_question = _normalize_for_matching(question)
    scored = [
        (fuzz.token_set_ratio(normalized_question, _normalize_for_matching(c["title"])), c)
        for c in candidates
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[:top_n]


async def crawl_url_answer(
    question: str, config: dict, conversation_history: Optional[list[dict]] = None
) -> Optional[dict]:
    """Runs the full crawl-based live-URL-search flow for one question.
    Returns a structured answer dict (same shape as
    helpdesk_answer._generate_and_format's output, source="live_url") on
    success, or None at the first point nothing usable is found — the
    caller falls through to the existing index search exactly as before
    in that case. `config` is accepted for call-site symmetry with the
    SearXNG-based live_url_answer() (which needs Azure OpenAI config
    explicitly) — this path gets its own config from environment
    variables via generate_structured_response(), so it's unused here.
    conversation_history is passed straight through to
    generate_structured_response() so follow-up questions answered from a
    live URL get the same prior-turn context as the ticket/library paths.
    """
    # Deferred import: avoids a module-level circular import with
    # helpdesk_answer.py (same reasoning as live_url_search.py), and avoids
    # paying helpdesk_answer's own import weight on requests that never
    # reach this branch.
    from helpdesk_answer import generate_structured_response, resolve_cited_chunks, resolve_valid_follow_ups

    logger.info("crawl_url_answer: STARTING for question: %s", question[:100])

    try:
        tracked = get_url_candidates()
    except (SharePointConfigError, GraphAPIError) as exc:
        logger.warning("Failed to fetch tracked URLs for crawl-based search: %s", exc)
        return None
    seed_urls = [c["url"] for c in tracked if c.get("url")]
    logger.info("crawl_url_answer: STAGE get_url_candidates -> %d seed(s)", len(seed_urls))
    if not seed_urls:
        return None

    pool = await build_candidate_pool(seed_urls)
    logger.info(
        "crawl_url_answer: STAGE build_candidate_pool -> %d candidate(s) from %d seed(s)",
        len(pool), len(seed_urls),
    )
    if not pool:
        return None

    ranked = rank_candidates(question, pool)
    logger.info(
        "crawl_url_answer: STAGE rank_candidates -> %s",
        [(round(score, 1), c["url"]) for score, c in ranked],
    )

    fetched_pages = await asyncio.gather(*[asyncio.to_thread(_fetch_page, c["url"]) for _, c in ranked])
    pages = []
    for (score, candidate), page in zip(ranked, fetched_pages):
        if not page:
            continue
        pages.append({"title": candidate["title"], "url": candidate["url"], "content": extract_text(page["html"])})
    logger.info("crawl_url_answer: STAGE fetch top candidates -> %d/%d page(s) fetched", len(pages), len(ranked))
    if not pages:
        return None

    chunks = [{**p, "source_type": "live_url"} for p in pages]

    try:
        structured = generate_structured_response(question, chunks, conversation_history)
    except Exception:  # noqa: BLE001 — a failed generation means "nothing found here", not a crash
        logger.exception("Crawl-based URL answer generation failed for question: %s", question[:100])
        return None

    if structured.get("not_found"):
        return None

    cited = resolve_cited_chunks(chunks, structured.get("answer_reference_numbers"))
    sources = (
        [{"title": c["title"], "url": c["url"], "source_type": "live_url"} for c in cited]
        or [{"title": chunks[0]["title"], "url": chunks[0]["url"], "source_type": "live_url"}]
    )

    return {
        "subject": structured.get("subject", ""),
        "description": structured.get("description", ""),
        "status": "answered",
        "answer": structured.get("answer", ""),
        "category": structured.get("category", ""),
        "subcategory": structured.get("sub_category", ""),
        "source": "live_url",
        "sources": sources,
        "follow_up_questions": resolve_valid_follow_ups(chunks, structured.get("follow_up_questions")),
    }
