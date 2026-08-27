"""
Live, per-question URL search for helpdesk questions.
--------------------------------------------------------

Flow: pre-filter tracked List URLs by keyword match -> inspect survivors
via a self-hosted SearXNG instance (searxng_client.py) -> AI selects every
candidate URL/domain (usually one, but up to MAX_SELECTED_URLS when more
than one genuinely helps) with reasoning -> a bounded iterative
search-refine loop scoped to each selected domain (run concurrently)
finds the specific page that actually answers the question -> content
from all selected domains is merged -> generate one answer from that
combined content -> if nothing usable turns up at any step, the caller
(helpdesk_answer.answer_question()) falls through to the existing index
search unchanged.

Search backend: this used to call the free, unofficial DuckDuckGo scraper
(the ddgs package) directly. Switched to a self-hosted SearXNG instance
(see searxng-app/) after a live incident showed ddgs was unreliable under
real load — the exact same query returned results from one machine and
nothing from another moments later, and heavy testing triggered extended
DDG-side rate-limiting with no way to mitigate it from our side. SearXNG
is our own infrastructure (no third-party rate limit to hit) and
aggregates several real engines (Brave, Google CSE, Wikipedia, ...)
rather than depending on one unofficial scraper.

This exists alongside the index-based RAG path (helpdesk_answer.py) as an
earlier-tried option: a tracked List URL's *current* live content can
answer a question the index hasn't caught up to yet (not yet crawled, or
changed since the last crawl), without waiting for the next scheduled
crawl. It deliberately reuses generate_structured_response() rather than
duplicating its OpenAI-calling logic.

The iterative_domain_search() step (search -> observe -> think -> select/
refine/give_up, bounded at MAX_SEARCH_ITERATIONS rounds) replaced an
earlier approach that just fuzzy-matched the selected page's own outbound
links: fuzzy-matching link anchor text against the question turned out to
reliably rank thin/generic pages (homepage, tag-archive listings) above
genuinely relevant ones with more specific but differently-worded titles.
Site-scoped search naturally surfaces the specific page a search engine
already considers most relevant to the query, which sidesteps that whole
class of ranking problem. Verified this still holds against SearXNG: a
"site:domain question" query returns correctly domain-scoped, relevant
results the same way it did against DDG.

Every step is designed to fail soft — a bad search lookup, a dead URL, a
malformed AI response — falls through to "nothing found here" rather than
raising, since this whole module is a bonus path ahead of the reliable
index fallback.
"""

import asyncio
import json
import logging
import os
import re
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
import trafilatura
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from searxng_client import searxng_search
from sharepoint_client import get_url_candidates, SharePointConfigError, GraphAPIError

logger = logging.getLogger("live_url_search")

# Recalibrated after normalizing URL/title text before scoring (see
# _normalize_for_matching): tokenizing slugs into real words raises scores
# across the board — including for genuinely irrelevant candidates, whose
# "noise floor" moved from ~25-28 to ~34-36 — so the threshold needed to
# move with it to keep filtering out plausible-looking-but-unrelated pages.
PREFILTER_MIN_SCORE = int(os.environ.get("PREFILTER_MIN_SCORE", "38"))
# Measured against real search/HTTP: the full flow can run several
# seconds to low tens of seconds. Kept bounded since this whole path
# already only runs for questions the ticket index found nothing for
# (see helpdesk_answer.answer_question) — worth keeping fast rather than
# exhaustive. SearXNG is our own infrastructure, not a rate-limited
# third party, so this is smaller than the old DDG-era default (which
# had to leave room for repeated retries against an unreliable scraper).
SEARCH_QUERY_TIMEOUT_SECONDS = int(os.environ.get("SEARCH_QUERY_TIMEOUT_SECONDS", "8"))
# One retry on an empty/failed search result — covers an ordinary
# transient blip (a slow upstream engine, a brief network hiccup) without
# needing the aggressive multi-attempt backoff the old ddgs integration
# needed to work around DuckDuckGo's own rate-limiting.
SEARCH_MAX_ATTEMPTS = int(os.environ.get("SEARCH_MAX_ATTEMPTS", "2"))
SEARCH_RETRY_DELAY_SECONDS = float(os.environ.get("SEARCH_RETRY_DELAY_SECONDS", "0.5"))
LIVE_FETCH_TIMEOUT_SECONDS = int(os.environ.get("LIVE_FETCH_TIMEOUT_SECONDS", "5"))
SELECT_REQUEST_TIMEOUT_SECONDS = int(os.environ.get("ANSWER_REQUEST_TIMEOUT_SECONDS", "30"))
# Bounded ReAct-style search-refine loop (see iterative_domain_search) —
# hard cap, no open-ended agent framework.
MAX_SEARCH_ITERATIONS = int(os.environ.get("MAX_SEARCH_ITERATIONS", "3"))
# How many candidate URLs the AI may select together when more than one
# genuinely helps answer the question (e.g. two different tracked sites
# each have independently relevant content). Usually only one is selected;
# capped since each selected URL runs its own full iterative_domain_search
# + sub-link exploration downstream, and this bounds the total work fanned
# out from one question.
MAX_SELECTED_URLS = int(os.environ.get("MAX_SELECTED_URLS", "2"))
# Inner sub-link exploration: after iterative_domain_search resolves a
# page, also pull in a few of ITS most relevant outbound links for extra
# grounding content (e.g. a Wikipedia article's own "see also" links).
MAX_SUBLINKS = int(os.environ.get("MAX_SUBLINKS", "3"))
# A fetched sub-page shorter than this (or identical to the main page's
# own content) adds nothing — dropped rather than sent into generation.
MIN_SUBPAGE_CONTENT_CHARS = int(os.environ.get("MIN_SUBPAGE_CONTENT_CHARS", "150"))

_DISALLOWED_LINK_SCHEMES = {"mailto", "tel", "javascript", "data"}

# Social-media/navigation-utility links never contain question-relevant
# content, but their short generic anchor text ("Search", "Cart",
# "Facebook") can otherwise score deceptively close to genuinely relevant
# pages under fuzzy matching — excluded up front so they can't crowd out
# real content for one of the few sub-link slots.
_UTILITY_DOMAINS = {
    "facebook.com", "www.facebook.com", "instagram.com", "www.instagram.com",
    "youtube.com", "www.youtube.com", "twitter.com", "www.twitter.com", "x.com",
    "linkedin.com", "www.linkedin.com", "pinterest.com", "www.pinterest.com",
    "wa.me", "t.me",
}
_UTILITY_PATH_PREFIXES = ("/cart", "/search", "/account", "/login", "/checkout")

# MediaWiki (Wikipedia and similar) non-article namespaces — a "/wiki/"
# link whose title segment starts with one of these (e.g.
# "/wiki/Category_talk:Lenovo_laptops", "/wiki/Talk:Python") points at
# discussion, meta, or maintenance content, not encyclopedic article text.
# Found via live testing: a bare "Category:" listing page (itself thin —
# just a list of links, no prose) had its sub-link exploration pull in a
# Category_talk page and a same-title page in a different language edition
# as "relevant" sub-links purely on anchor-text similarity, diluting the
# already-thin content past the point the answer model could use it.
_MEDIAWIKI_NAMESPACE_PREFIXES = (
    "Talk:", "User:", "User_talk:", "Wikipedia:", "Wikipedia_talk:",
    "File:", "File_talk:", "Category_talk:", "Template:", "Template_talk:",
    "Help:", "Help_talk:", "Portal:", "Portal_talk:", "Draft:", "Module:", "Special:",
)


class LiveUrlSearchConfigError(Exception):
    """Raised when required configuration for the URL-selection call is missing."""


# --------------------------------------------------------------------------
# PART 2 — Pre-filter
# --------------------------------------------------------------------------

def _normalize_for_matching(text: str) -> str:
    """Collapses any run of non-alphanumeric characters (hyphens,
    underscores, slashes, punctuation) into a single space. rapidfuzz's
    token-based scorers only split on whitespace, so without this a URL
    slug like "used-gaming-laptop-overheat-throttle" is one unbroken,
    untokenizable blob that can never match individual question words —
    this was the root cause found via debug_live_url_search.py: short
    generic URLs ("/", "/cart") were consistently outscoring long,
    genuinely relevant ones purely because they had less untokenizable
    text to be penalized for.
    """
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _score_candidates(question: str, candidates: list[dict]) -> list[tuple[float, dict]]:
    """Fuzzy-match score (rapidfuzz token_set_ratio, question vs. each
    candidate's combined title+url text, both normalized so slugs and
    URLs actually tokenize into words) for every candidate, sorted
    highest-scoring first. Shared by prefilter_candidates() and the
    diagnostic logging/debug script around it, so every caller sees
    identical scores -- no separate reimplementation to drift out of sync.
    """
    normalized_question = _normalize_for_matching(question)
    scored = [
        (
            fuzz.token_set_ratio(
                normalized_question,
                _normalize_for_matching(f"{c.get('title', '')} {c.get('url', '')}"),
            ),
            c,
        )
        for c in candidates
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


def prefilter_candidates(question: str, candidates: list[dict], top_n: int = 5) -> list[dict]:
    """Fuzzy-matches the question against each candidate's combined
    title+url text (rapidfuzz) and returns the top_n highest scorers.

    The PREFILTER_MIN_SCORE gate only applies once the candidate pool is
    larger than top_n — its job is bounding search calls against a LARGE
    pool, not making a fine-grained relevance call a raw fuzzy score can't
    reliably make. Found via live testing: a short, generic title
    ("laptop") structurally outscores a more specific title ("Lenovo") on
    almost any laptop-adjacent question, so a brand question like
    "thinkpad laptops" scored Lenovo at 37.14 — just under the 38
    threshold — while "laptop" cleared it easily. Thresholding here didn't
    just risk losing a borderline candidate, it silently dropped the
    obviously-correct one from a multi-candidate field while a worse one
    survived alone. Search calls in inspect_candidates() run concurrently, so
    letting every candidate through in a small pool costs no serial time —
    the AI selection step (with real search snippets) is what actually
    judges relevance; a wasted lookup on a genuinely irrelevant candidate
    is cheap, losing the genuinely relevant one is not.
    """
    scored = _score_candidates(question, candidates)
    if len(candidates) <= top_n:
        return [candidate for _, candidate in scored]
    survivors = [candidate for score, candidate in scored if score >= PREFILTER_MIN_SCORE]
    return survivors[:top_n]


# --------------------------------------------------------------------------
# PART 3 — SearXNG inspection
# --------------------------------------------------------------------------

async def _search_with_retry(
    query: str, max_results: int,
    retry_delay_seconds: float = SEARCH_RETRY_DELAY_SECONDS,
    max_attempts: int = SEARCH_MAX_ATTEMPTS,
) -> list[dict]:
    """searxng_search() with one retry (short delay) on an empty/failed
    result — covers an ordinary transient blip (a slow upstream engine, a
    brief network hiccup against our own SearXNG instance) without the
    aggressive multi-attempt backoff the old ddgs integration needed to
    work around DuckDuckGo's own rate-limiting. Never raises: exhausts
    every attempt and returns [] rather than propagating (searxng_search
    itself already never raises, but the empty-result-triggers-retry loop
    lives here).
    """
    for attempt in range(max_attempts):
        results = await searxng_search(query)
        if results:
            return results[:max_results]
        if attempt < max_attempts - 1:
            await asyncio.sleep(retry_delay_seconds * (attempt + 1))
    logger.warning("SearXNG query %r returned no results on all %d attempt(s).", query, max_attempts)
    return []


async def _searxng_snippet(question: str, url: str) -> str:
    domain = urlsplit(url).netloc
    if not domain:
        return ""
    results = await _search_with_retry(f"site:{domain} {question}", max_results=1)
    logger.info("SearXNG lookup for domain %s: %d result(s)", domain, len(results))
    if not results:
        return ""
    return results[0].get("snippet", "") or ""


async def inspect_candidates(question: str, candidates: list[dict]) -> list[dict]:
    """Runs one SearXNG query per candidate concurrently (searxng_search is
    natively async, so no thread-offloading needed), enriching each
    candidate with a "snippet" field. A slow or failing lookup for one
    candidate is isolated by its own timeout/try-except and never blocks
    or crashes the inspection of the others.
    """
    async def _inspect_one(candidate: dict) -> dict:
        try:
            snippet = await asyncio.wait_for(
                _searxng_snippet(question, candidate["url"]),
                timeout=SEARCH_QUERY_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 — timeout or any other failure -> no snippet, not a crash
            snippet = ""
        return {**candidate, "snippet": snippet}

    return list(await asyncio.gather(*[_inspect_one(c) for c in candidates]))


# --------------------------------------------------------------------------
# PART 4 — AI URL selection
# --------------------------------------------------------------------------

def select_best_urls(question: str, inspected: list[dict], config: dict) -> Optional[dict]:
    """One cheap classification-style call (gpt-4o-mini by default) that
    picks every candidate URL that independently helps answer this
    question — usually just one, but sometimes two different tracked
    sites both have genuinely relevant content (e.g. one covers pricing,
    another covers technical specs). Returns {"selected_urls": [str, ...],
    "reasoning": str}, or None if nothing was selected (none of the
    candidates look relevant, every returned URL was invalid, or the call
    itself fails) — the caller's cue to fall through to the existing index
    search.

    Capped at MAX_SELECTED_URLS: each selected URL runs its own full
    iterative_domain_search + sub-link exploration downstream, so this
    bounds the total work fanned out from one question.
    """
    if not inspected:
        return None

    endpoint = config.get("endpoint", "")
    api_key = config.get("api_key", "")
    api_version = config.get("api_version", "2024-08-01-preview")
    deployment = config.get("select_deployment", "gpt-4o-mini")
    if not endpoint or not api_key:
        raise LiveUrlSearchConfigError("Azure OpenAI endpoint/key must be set for URL selection.")

    candidates_block = "\n\n".join(
        f"URL: {c.get('url', '')}\nTitle: {c.get('title', '')}\n"
        f"Search snippet: {c.get('snippet') or '(no snippet found)'}"
        for c in inspected
    )
    system_prompt = (
        "You are selecting which candidate URL(s) are relevant enough to help "
        "answer a helpdesk question, from a short list of candidate URLs found "
        "via search. Each candidate includes its title and a search snippet.\n\n"
        "Respond with ONLY a JSON object matching exactly this schema:\n"
        '{"selected_urls": [string, ...], "reasoning": string}\n\n'
        "Rules:\n"
        "- Usually only one candidate is relevant — select just that one.\n"
        "- Select more than one ONLY if multiple candidates independently "
        "contain information that helps answer the question (e.g. one covers "
        "pricing and a different site covers technical specs). Every URL you "
        "include must genuinely help on its own — never add a second URL just "
        "to pad the list.\n"
        f"- Select at most {MAX_SELECTED_URLS} URL(s).\n"
        "- Set selected_urls to an empty list if none of the candidates "
        "actually look like they would answer the question — do not guess "
        "just to pick one.\n\n"
        f"Candidates:\n{candidates_block}"
    )

    url = (
        f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/chat/completions"
        f"?api-version={api_version}"
    )
    headers = {"api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        "temperature": 0.0,
        # max_completion_tokens, not the older max_tokens — newer models
        # (e.g. gpt-5.1) reject max_tokens outright ("Unsupported
        # parameter"), and max_completion_tokens works against gpt-4o-mini
        # too (confirmed directly), so there's no need for a per-model
        # conditional here.
        "max_completion_tokens": 300,
        "response_format": {"type": "json_object"},
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=SELECT_REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        result = json.loads(content)
    except Exception:  # noqa: BLE001 — a failed selection call means "nothing found here", not a crash
        logger.exception("select_best_urls call failed for question: %s", question[:100])
        return None

    raw_selected = result.get("selected_urls")
    if not raw_selected or not isinstance(raw_selected, list):
        return None

    valid_urls = {c.get("url") for c in inspected}
    selected_urls: list[str] = []
    for candidate_url in raw_selected:
        if candidate_url not in valid_urls:
            # The model is only ever shown the candidates in `inspected` —
            # a URL outside that set means it hallucinated or malformed
            # one. Trusting it blindly would send iterative_domain_search
            # off to search a domain never actually vetted by the
            # fuzzy-match/search-snippet steps.
            logger.warning(
                "select_best_urls: model returned a URL not in the candidate list, discarding: %s",
                candidate_url,
            )
            continue
        if candidate_url not in selected_urls:  # dedupe in case the model repeated one
            selected_urls.append(candidate_url)

    selected_urls = selected_urls[:MAX_SELECTED_URLS]
    if not selected_urls:
        return None
    return {"selected_urls": selected_urls, "reasoning": result.get("reasoning", "")}


# --------------------------------------------------------------------------
# PART 5 — Live fetch, plain HTTP
# --------------------------------------------------------------------------

def _fallback_title_from_url(url: str) -> str:
    """Used when a link has no visible anchor text (e.g. an image-only
    link) — the last path segment, with hyphens/underscores turned into
    spaces, still gives fuzzy-matching real words to score against instead
    of an untokenizable URL slug.
    """
    path = urlsplit(url).path.rstrip("/")
    last_segment = path.rsplit("/", 1)[-1] if path else ""
    return last_segment.replace("-", " ").replace("_", " ")


def _extract_page_links(html: str, base_url: str) -> list[dict]:
    """Extracts every same-scheme link from a page's HTML, each with its
    visible anchor text (falling back to a de-hyphenated URL path segment
    when there isn't any) — the actual
    signal prefilter_candidates() needs to score against. A bare URL slug
    has no whitespace for rapidfuzz's token_set_ratio to split on, so
    scoring against the URL alone was unreliable: see
    debug_live_url_search.py's findings, where a specific, genuinely
    relevant slug like ".../used-gaming-laptop-overheat-throttle" lost to
    the site's own homepage purely because the homepage URL is shorter.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    links: list[dict] = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href:
            continue
        absolute = urljoin(base_url, href)
        scheme = urlsplit(absolute).scheme.lower()
        if scheme not in ("http", "https") or scheme in _DISALLOWED_LINK_SCHEMES:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        anchor_text = tag.get_text(strip=True)
        links.append({"url": absolute, "title": anchor_text or _fallback_title_from_url(absolute)})
    return links


def _extract_page_title(html: str) -> str:
    tag = BeautifulSoup(html, "html.parser").find("title")
    return tag.get_text(strip=True) if tag else ""


def _strip_fragment(url: str) -> str:
    """The canonical (fragment-free) form of a URL — several distinct
    #anchors can point at the same actual page, and a fragment is never
    sent to the server anyway, so this is the right key for both "is this
    the same page as the one I'm already on" and "have I already queued
    this page" comparisons.
    """
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _is_utility_link(url: str) -> bool:
    """Social-media profile links, site-navigation utility pages (cart,
    search, login, checkout), and MediaWiki non-article namespace/action
    links — never useful as question-answering grounding content,
    regardless of what their anchor text fuzzy-matches against.
    """
    parts = urlsplit(url)
    if parts.netloc.lower() in _UTILITY_DOMAINS:
        return True
    path = parts.path.lower().rstrip("/")
    if any(path == prefix or path.startswith(prefix + "/") for prefix in _UTILITY_PATH_PREFIXES):
        return True
    if "action=" in parts.query.lower():
        # e.g. "?action=edit" / "?action=history" — a MediaWiki edit/raw
        # view, not readable content.
        return True
    if parts.path.startswith("/wiki/"):
        title_segment = parts.path[len("/wiki/"):]
        if title_segment.startswith(_MEDIAWIKI_NAMESPACE_PREFIXES):
            return True
    return False


def _build_sublink_candidates(links: list[dict], current_url: str) -> list[dict]:
    """Filters raw extracted links down to genuine sub-link candidates:
    drops utility/social-media/MediaWiki-namespace links, drops links to a
    different domain (a sub-link should dig deeper into the same site the
    main page is on, not wander to an interlanguage or otherwise related
    site — found via live testing: a Wikipedia article's own interlanguage
    link to the same category on pt.wikipedia.org fuzzy-matched the
    question just as well as genuine same-site sub-pages), drops same-page
    anchor fragments (same page, just a different #section), and dedupes
    by fragment-stripped (canonical) URL, since several distinct anchors
    can point at the same actual page.
    """
    current_canonical = _strip_fragment(current_url)
    current_domain = urlsplit(current_url).netloc.lower()
    seen: set[str] = set()
    candidates: list[dict] = []
    for link in links:
        if urlsplit(link["url"]).netloc.lower() != current_domain:
            continue
        if _is_utility_link(link["url"]):
            continue
        canonical = _strip_fragment(link["url"])
        if canonical == current_canonical or canonical in seen:
            continue
        seen.add(canonical)
        candidates.append({"url": canonical, "title": link.get("title", "")})
    return candidates


def prefilter_sublink_candidates(question: str, candidates: list[dict], top_n: int) -> list[dict]:
    """Same score+threshold approach as prefilter_candidates(), but also
    enforces URL path-family diversity: at most one candidate per path
    prefix (first 3 path segments) is kept. Without this, several
    near-duplicate pages under the same tag/archive section (e.g. a blog's
    ".../tagged/*-gaming-laptop" pages — thin, largely-interchangeable
    listing pages) can sweep every sub-link slot on score alone, crowding
    out a more substantively different and often more useful page
    elsewhere on the site.
    """
    scored = _score_candidates(question, candidates)
    seen_families: set[str] = set()
    picked: list[dict] = []
    for score, candidate in scored:
        if score < PREFILTER_MIN_SCORE:
            break
        family = "/".join(urlsplit(candidate["url"]).path.strip("/").split("/")[:3])
        if family in seen_families:
            continue
        seen_families.add(family)
        picked.append(candidate)
        if len(picked) >= top_n:
            break
    return picked


async def _fetch_page_and_sublinks(question: str, url: str, max_sublinks: int) -> list[dict]:
    """Fetches one page, then its most question-relevant inner sub-links
    (fuzzy-matched + path-family-diversified, capped at max_sublinks —
    not all links, since this runs in the request path). Returns the main
    page first, then its sub-pages, filtering out any that are empty, too
    thin to be useful, or an exact duplicate of the main page's content.
    """
    main_page = await asyncio.to_thread(fetch_page_plain, url)
    main_content = main_page.get("content", "")
    if not main_content:
        return []

    pages = [{"title": main_page.get("title") or url, "url": url, "content": main_content}]

    raw_links = main_page.get("links", [])
    link_candidates = _build_sublink_candidates(raw_links, url)
    if not link_candidates or max_sublinks <= 0:
        logger.info("Sub-link selection for %s: %d raw link(s), none pursued", url, len(raw_links))
        return pages

    top_links = prefilter_sublink_candidates(question, link_candidates, top_n=max_sublinks)
    logger.info(
        "Sub-link selection for %s: %d/%d candidate(s) survived (%d raw link(s) before "
        "utility/same-page/duplicate filtering)",
        url, len(top_links), len(link_candidates), len(raw_links),
    )
    if not top_links:
        return pages

    sub_pages = await asyncio.gather(*[asyncio.to_thread(fetch_page_plain, c["url"]) for c in top_links])
    dropped = 0
    for page in sub_pages:
        content = page.get("content", "")
        if not content or len(content) < MIN_SUBPAGE_CONTENT_CHARS or content == main_content:
            dropped += 1
            continue
        pages.append({"title": page.get("title") or page.get("url", ""), "url": page.get("url", ""), "content": content})
    if dropped:
        logger.info("Sub-link fetch for %s: dropped %d/%d fetched sub-page(s) as empty/too thin/duplicate",
                    url, dropped, len(top_links))
    return pages


def fetch_page_plain(url: str) -> dict:
    """Plain (non-JS-rendered) HTTP fetch — deliberately simpler and much
    faster than the crawler's Playwright-based fetch_service, since this
    runs synchronously in the /api/ask request path where a full headless
    browser render would be too slow. Never raises: any fetch/parse
    failure just yields empty content so the caller skips this page.
    """
    try:
        resp = requests.get(url, timeout=LIVE_FETCH_TIMEOUT_SECONDS, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        html = resp.text
        content = trafilatura.extract(html, url=url, include_comments=False, include_tables=False) or ""
        links = _extract_page_links(html, url)
        logger.info(
            "fetch_page_plain(%s): %d char(s) of content extracted, %d raw link(s) found",
            url, len(content), len(links),
        )
        return {
            "url": url,
            "title": _extract_page_title(html) or url,
            "content": content,
            "links": links,
        }
    except Exception:  # noqa: BLE001 — never crash the request path over one bad page
        logger.warning("Live fetch failed for %s", url, exc_info=True)
        return {"url": url, "content": "", "links": []}


_SITE_PREFIX_RE = re.compile(r"^\s*(site:\S+\s*)+", re.IGNORECASE)


def _strip_site_prefix(query: str) -> str:
    """Defensive backstop for a model-returned refined_query that includes
    its own "site:domain" prefix despite the prompt saying not to (seen in
    practice: the "Query:" lines in the history shown to the model already
    include it, and a model can end up mimicking that format). Without
    this, the caller's own `f"site:{domain} {query}"` would double up into
    a malformed "site:x site:x actual terms" query.
    """
    return _SITE_PREFIX_RE.sub("", query).strip()


def _observe_and_think(question: str, search_history: list[dict], config: dict) -> Optional[dict]:
    """One cheap call (gpt-4o-mini by default) given the full search
    history so far (every query tried this session and its results),
    deciding whether to select a specific result URL, refine the query for
    another round, or give up. Returns the parsed
    {"action", "selected_url", "refined_query", "reasoning"} dict, or None
    if the call itself fails — the loop treats that the same as give_up.
    """
    endpoint = config.get("endpoint", "")
    api_key = config.get("api_key", "")
    api_version = config.get("api_version", "2024-08-01-preview")
    deployment = config.get("select_deployment", "gpt-4o-mini")
    if not endpoint or not api_key:
        raise LiveUrlSearchConfigError("Azure OpenAI endpoint/key must be set for iterative search.")

    history_block = "\n\n".join(
        f"Iteration {i + 1}:\nQuery: {turn['query']}\nResults:\n"
        + (
            "\n".join(
                f"{j + 1}. {r.get('title', '')} — {r.get('url', '')}\n   {r.get('snippet', '')}"
                for j, r in enumerate(turn["results"])
            )
            if turn["results"] else "(no results)"
        )
        for i, turn in enumerate(search_history)
    )
    system_prompt = (
        "You are iteratively searching ONE specific website to find the exact "
        "page that answers a helpdesk question. Each iteration gives you one "
        "site-scoped search and its results; you can see every query tried so "
        "far this session and what it returned.\n\n"
        "Respond with ONLY a JSON object matching exactly this schema:\n"
        '{"action": "select" | "refine" | "give_up", "selected_url": string or '
        'null, "refined_query": string or null, "reasoning": string}\n\n'
        "Rules:\n"
        '- "select": PREFER THIS whenever any result plausibly relates to the '
        "question, even partially — you do NOT need full certainty, just a "
        "reasonable match. A specific, on-topic result beats another round of "
        "searching. Set selected_url to that exact URL.\n"
        '- "refine": ONLY if every result so far is about something clearly '
        "unrelated, AND you have a genuinely different search angle to try — "
        "not a minor rewording. refined_query must be meaningfully different "
        "from every query already listed above; do not repeat or "
        'near-repeat one. refined_query must be ONLY the new search terms '
        '— never include "site:..." yourself, it is prepended '
        "automatically each round (the \"Query:\" lines shown to you below "
        "already include it for your reference, but your own answer must "
        "not).\n"
        '- "give_up": nothing useful has turned up after a real attempt and '
        "further searching this site is unlikely to help.\n\n"
        'Default to "select" over "refine" when in doubt — a reasonable pick '
        "grounded in an actual result beats repeated hedging.\n\n"
        f"Search history so far:\n{history_block}"
    )

    url = (
        f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/chat/completions"
        f"?api-version={api_version}"
    )
    headers = {"api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        "temperature": 0.0,
        # max_completion_tokens, not the older max_tokens — newer models
        # (e.g. gpt-5.1) reject max_tokens outright ("Unsupported
        # parameter"), and max_completion_tokens works against gpt-4o-mini
        # too (confirmed directly), so there's no need for a per-model
        # conditional here.
        "max_completion_tokens": 300,
        "response_format": {"type": "json_object"},
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=SELECT_REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception:  # noqa: BLE001 — a failed think-step means "give up", not a crash
        logger.exception("iterative search think-step failed for question: %s", question[:100])
        return None


async def iterative_domain_search(
    question: str, selected_url: str, config: dict, max_iterations: int = MAX_SEARCH_ITERATIONS
) -> Optional[dict]:
    """Bounded ReAct-style search-refine loop, scoped to selected_url's own
    domain: ACT (site-scoped SearXNG search) -> OBSERVE + THINK (one AI call
    reviewing the full search history) -> select a specific result / refine
    the query for another round / give up. Plain functions, hard-capped at
    max_iterations rounds — no open-ended agent framework.

    Replaced the earlier approach of fuzzy-matching the selected page's own
    outbound links: that reliably ranked thin/generic pages (homepage,
    tag-archive listings) above genuinely relevant ones with differently-
    worded titles. Site-scoped search sidesteps that by using the search
    engine's own relevance ranking instead of a bespoke fuzzy-match score.

    On "select": fetches the chosen URL, plus a few of ITS most
    question-relevant inner sub-links for extra grounding content (see
    _fetch_page_and_sublinks — e.g. Wikipedia's own cross-referenced
    articles). Returns {"content_pieces": [{"title","url","content"}, ...],
    "search_history": [...]} on success, main page first.

    An explicit "give_up" is always honored as-is — returns None, no
    second-guessing. But if the model instead keeps choosing "refine"
    without ever committing (including repeating/near-repeating a query
    already tried, which isn't real refinement), the loop stops early and
    falls back to SearXNG's own top-ranked result from the most recent
    iteration that returned any — real search results shouldn't be
    discarded just because the model hedged rather than deciding. Only
    returns None with nothing to show for it if the fetch after a
    selection/fallback fails, or no iteration ever returned any results.
    """
    domain = urlsplit(selected_url).netloc
    search_history: list[dict] = []
    tried_queries = {question.strip().lower()}
    query = question

    async def _fetch_result(result_url: str, reason: str) -> Optional[dict]:
        pages = await _fetch_page_and_sublinks(question, result_url, MAX_SUBLINKS)
        if not pages:
            logger.info(
                "Iterative search for %s: %s URL %s fetched no usable content, giving up.",
                domain, reason, result_url,
            )
            return None
        return {"content_pieces": pages, "search_history": search_history}

    for iteration in range(1, max_iterations + 1):
        site_query = f"site:{domain} {query}"
        try:
            results = await asyncio.wait_for(
                _search_with_retry(site_query, max_results=5), timeout=SEARCH_QUERY_TIMEOUT_SECONDS
            )
        except Exception:  # noqa: BLE001 — timeout or any failure -> no results this round, not a crash
            results = []
        search_history.append({"query": site_query, "results": results})

        decision = _observe_and_think(question, search_history, config)
        if decision is None:
            logger.info(
                "Iterative search %d/%d for %s: think-step failed, giving up.",
                iteration, max_iterations, domain,
            )
            return None

        action = decision.get("action")
        logger.info(
            "Iterative search %d/%d for %s: query=%r, %d result(s), action=%s, reasoning=%s",
            iteration, max_iterations, domain, site_query, len(results), action,
            decision.get("reasoning", ""),
        )

        if action == "select" and decision.get("selected_url"):
            return await _fetch_result(decision["selected_url"], "selected")

        if action == "give_up":
            return None

        refined_query = decision.get("refined_query")
        if refined_query:
            refined_query = _strip_site_prefix(refined_query)
        if not refined_query or refined_query.strip().lower() in tried_queries:
            # No real refinement offered, or the model just repeated a query
            # already tried (not genuine progress) — further looping won't
            # help. Fall back to the best result seen so far instead of
            # discarding real search results just because the model hedged.
            logger.info(
                "Iterative search for %s: no new refinement after %d iteration(s) "
                "(refined_query=%r already tried or empty) — falling back to top result.",
                domain, iteration, refined_query,
            )
            break
        tried_queries.add(refined_query.strip().lower())
        query = refined_query
    else:
        logger.info(
            "Iterative search for %s: max_iterations (%d) exhausted without a selection — "
            "falling back to top result.",
            domain, max_iterations,
        )

    # Reached only via the "no progress" break or natural loop exhaustion
    # (an explicit "select" or "give_up" always returns directly above) —
    # try SearXNG's own top-ranked result from the most recent
    # iteration that actually returned one.
    for turn in reversed(search_history):
        if turn["results"]:
            return await _fetch_result(turn["results"][0]["url"], "fallback")
    return None


# --------------------------------------------------------------------------
# PART 6 — Orchestration
# --------------------------------------------------------------------------

async def live_url_answer(question: str, config: dict) -> Optional[dict]:
    """Runs the full live-URL-search flow for one question. Returns a
    structured answer dict (same shape as helpdesk_answer._generate_and_format's
    output, with source="live_url") on success, or None at the first point
    nothing usable is found — the caller falls through to the existing
    index search exactly as before in that case.
    """
    # Deferred import: both this and helpdesk_answer's own import of
    # live_url_answer are deferred (see helpdesk_answer.answer_question)
    # specifically to avoid a module-level circular import between the two
    # files. By call time both modules are fully loaded.
    from helpdesk_answer import generate_structured_response

    logger.info("live_url_answer: STARTING for question: %s", question[:100])

    try:
        candidates = get_url_candidates()
    except (SharePointConfigError, GraphAPIError) as exc:
        logger.warning("Failed to fetch URL candidates for live URL search: %s", exc)
        return None
    logger.info("live_url_answer: STAGE get_url_candidates -> %d candidate(s)", len(candidates))
    if not candidates:
        return None

    prefiltered = prefilter_candidates(question, candidates)
    logger.info("live_url_answer: STAGE prefilter_candidates -> %d survivor(s)", len(prefiltered))
    if not prefiltered:
        return None

    logger.info("live_url_answer: STAGE inspect_candidates -> starting %d SearXNG lookup(s)", len(prefiltered))
    inspected = await inspect_candidates(question, prefiltered)
    logger.info("live_url_answer: STAGE inspect_candidates -> done, %d snippet(s) non-empty",
                sum(1 for c in inspected if c.get("snippet")))

    selection = select_best_urls(question, inspected, config)
    logger.info("live_url_answer: STAGE select_best_urls -> %s", selection)
    if not selection:
        return None
    selected_urls = selection["selected_urls"]
    titles_by_url = {c.get("url"): c.get("title") for c in inspected}

    logger.info(
        "live_url_answer: STAGE iterative_domain_search -> starting for %d url(s): %s",
        len(selected_urls), ", ".join(selected_urls),
    )
    search_results = await asyncio.gather(
        *[iterative_domain_search(question, u, config) for u in selected_urls]
    )
    logger.info(
        "live_url_answer: STAGE iterative_domain_search -> done, %d/%d url(s) returned content",
        sum(1 for r in search_results if r), len(selected_urls),
    )

    # Merge content from every selected URL that actually returned
    # something — one selected domain failing (dead link, nothing found on
    # that site) shouldn't block a still-successful answer built from the
    # other(s).
    pages = []
    sources = []
    for selected_url, search_result in zip(selected_urls, search_results):
        if not search_result:
            continue
        pages.extend(search_result["content_pieces"])
        sources.append({
            "title": titles_by_url.get(selected_url, selected_url),
            "url": selected_url,
            "source_type": "live_url",
        })
    if not pages:
        return None

    chunks = [
        {"title": p["title"], "url": p["url"], "content": p["content"], "source_type": "live_url"}
        for p in pages
    ]
    logger.info(
        "live_url_answer: %d content piece(s) from %d domain(s) going into generation: %s",
        len(chunks), len(sources), ", ".join(c["url"] for c in chunks),
    )

    try:
        structured = generate_structured_response(question, chunks)
    except Exception:  # noqa: BLE001 — a failed generation means "nothing found here", not a crash
        logger.exception("Live URL answer generation failed for question: %s", question[:100])
        return None

    if structured.get("not_found"):
        return None

    return {
        "subject": structured.get("subject", ""),
        "description": structured.get("description", ""),
        "status": "answered",
        "answer": structured.get("answer", ""),
        "category": structured.get("category", ""),
        "subcategory": structured.get("sub_category", ""),
        "source": "live_url",
        "sources": sources,
        "follow_up_questions": structured.get("follow_up_questions", []),
    }
