"""
Local tests for the live, per-question URL search flow (live_url_search.py).
No real network needed — SearXNG (searxng_search), HTTP fetches, and the
OpenAI calls are all mocked.

Usage:
    python test_live_url_search.py
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import helpdesk_answer
import live_url_search

FAILURES = []


def check(label: str, condition: bool, detail: str = ""):
    if condition:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label} {detail}")
        FAILURES.append(label)


def run(coro):
    return asyncio.run(coro)


FAKE_CONFIG = {
    "endpoint": "https://fake.openai.azure.com",
    "api_key": "fake-key",
    "api_version": "2024-08-01-preview",
    "select_deployment": "gpt-4o-mini",
}


# --------------------------------------------------------------------------
# (a) No keyword-matching candidates -> short-circuits before any search call
# --------------------------------------------------------------------------

def test_no_matching_candidates_skips_search_entirely():
    print("\n--- (a) No matching candidates in a LARGE pool -> search never called ---")

    # PREFILTER_MIN_SCORE's threshold gate only applies once the pool
    # exceeds prefilter_candidates' top_n (5 by default) -- see
    # prefilter_candidates' docstring. Use 6 irrelevant candidates so this
    # test actually exercises that gate, rather than the "small pool, let
    # everything through" path added to fix the Lenovo/"thinkpad" bug.
    question = "how do I fix my printer paper jam"
    irrelevant_candidates = [
        {"url": "https://example.com/finance/quarterly-report", "title": "Q3 Financial Report"},
        {"url": "https://example.com/hr/vacation-policy", "title": "Vacation Policy"},
        {"url": "https://example.com/careers/open-positions", "title": "Open Job Positions"},
        {"url": "https://example.com/marketing/brand-guidelines", "title": "Brand Guidelines"},
        {"url": "https://example.com/legal/privacy-policy", "title": "Privacy Policy"},
        {"url": "https://example.com/investor-relations/annual-report-2023", "title": "Annual Shareholder Report"},
    ]

    # Sanity check on the pure function first.
    prefiltered = live_url_search.prefilter_candidates(question, irrelevant_candidates)
    check("prefilter_candidates returns [] for a large pool of irrelevant candidates",
          prefiltered == [], f"got {prefiltered}")

    with patch.object(live_url_search, "get_url_candidates", return_value=irrelevant_candidates), \
         patch.object(live_url_search, "searxng_search", new=AsyncMock()) as mock_search_call, \
         patch.object(live_url_search, "select_best_urls") as mock_select:

        result = run(live_url_search.live_url_answer(question, FAKE_CONFIG))

    check("live_url_answer returns None", result is None, f"got {result}")
    check("searxng_search was never called", mock_search_call.call_count == 0,
          f"call_count={mock_search_call.call_count}")
    check("select_best_urls was never called", mock_select.call_count == 0)


def test_small_pool_lets_borderline_candidate_through():
    print("\n--- (a2) Small pool: a below-threshold-but-correct candidate still reaches search ---")

    # Reproduces the real bug found via live testing: "Lenovo" scored 37.14
    # (just under the 38 threshold) against "what companies make thinkpad
    # laptops", while a generic "laptop" candidate cleared it easily and
    # would otherwise have been the ONLY one to reach search/AI selection.
    question = "what companies make thinkpad laptops"
    candidates = [
        {"url": "https://intaglaptops.com/blogs/laptops-blogs", "title": "laptop"},
        {"url": "https://en.wikipedia.org/wiki/Lenovo", "title": "Lenovo"},
    ]

    scores = {c["title"]: s for s, c in live_url_search._score_candidates(question, candidates)}
    check("sanity: 'laptop' clears the threshold on its own", scores["laptop"] >= live_url_search.PREFILTER_MIN_SCORE)
    check("sanity: 'Lenovo' does NOT clear the threshold alone",
          scores["Lenovo"] < live_url_search.PREFILTER_MIN_SCORE, f"got {scores['Lenovo']}")

    prefiltered = live_url_search.prefilter_candidates(question, candidates)
    prefiltered_titles = {c["title"] for c in prefiltered}
    check("both candidates reach the next stage despite Lenovo being below threshold",
          prefiltered_titles == {"laptop", "Lenovo"}, f"got {prefiltered_titles}")


# --------------------------------------------------------------------------
# (b) select_best_urls returning null -> live_url_answer returns None
# --------------------------------------------------------------------------

def test_select_best_urls_null_propagates_to_none():
    print("\n--- (b) select_best_urls -> None propagates to live_url_answer -> None ---")

    question = "how do I reset my vpn"
    candidates = [{"url": "https://example.com/it/vpn-guide", "title": "How to fix my VPN"}]

    with patch.object(live_url_search, "get_url_candidates", return_value=candidates), \
         patch.object(live_url_search, "inspect_candidates", new=AsyncMock(
             return_value=[{"url": candidates[0]["url"], "title": candidates[0]["title"], "snippet": "some snippet"}]
         )), \
         patch.object(live_url_search, "select_best_urls", return_value=None) as mock_select, \
         patch.object(live_url_search, "iterative_domain_search", new=AsyncMock()) as mock_search:

        result = run(live_url_search.live_url_answer(question, FAKE_CONFIG))

    check("live_url_answer returns None when select_best_urls returns None", result is None, f"got {result}")
    check("select_best_urls was called", mock_select.call_count == 1)
    check("iterative_domain_search was never called (nothing selected)", mock_search.call_count == 0)


def test_select_best_urls_itself_returns_none_on_empty_selection():
    print("\n--- (b2) select_best_urls() itself: model's empty selected_urls -> None ---")

    fake_resp = MagicMock()
    fake_resp.raise_for_status.return_value = None
    fake_resp.json.return_value = {
        "choices": [{"message": {"content": '{"selected_urls": [], "reasoning": "none of these look relevant"}'}}]
    }
    with patch.object(live_url_search, "requests") as mock_requests:
        mock_requests.post.return_value = fake_resp
        result = live_url_search.select_best_urls(
            "unrelated question",
            [{"url": "https://example.com/a", "title": "A", "snippet": "s"}],
            FAKE_CONFIG,
        )

    check("select_best_urls returns None when the model sets selected_urls=[]", result is None, f"got {result}")


def test_select_best_urls_rejects_hallucinated_url():
    print("\n--- select_best_urls rejects a URL the model returned but wasn't in the candidate list ---")

    fake_resp = MagicMock()
    fake_resp.raise_for_status.return_value = None
    fake_resp.json.return_value = {
        "choices": [{"message": {"content":
            '{"selected_urls": ["https://not-a-real-candidate.example.com"], "reasoning": "..."}'}}]
    }
    with patch.object(live_url_search, "requests") as mock_requests:
        mock_requests.post.return_value = fake_resp
        result = live_url_search.select_best_urls(
            "some question",
            [{"url": "https://example.com/a", "title": "A", "snippet": "s"}],
            FAKE_CONFIG,
        )

    check(
        "select_best_urls returns None when its only pick isn't among the presented candidates (hallucination guard)",
        result is None, f"got {result}",
    )


def test_select_best_urls_keeps_valid_ones_and_drops_hallucinated():
    print("\n--- select_best_urls drops only the hallucinated URL, keeps the valid one ---")

    fake_resp = MagicMock()
    fake_resp.raise_for_status.return_value = None
    fake_resp.json.return_value = {
        "choices": [{"message": {"content":
            '{"selected_urls": ["https://example.com/a", "https://not-a-real-candidate.example.com"], '
            '"reasoning": "..."}'}}]
    }
    with patch.object(live_url_search, "requests") as mock_requests:
        mock_requests.post.return_value = fake_resp
        result = live_url_search.select_best_urls(
            "some question",
            [{"url": "https://example.com/a", "title": "A", "snippet": "s"}],
            FAKE_CONFIG,
        )

    check("the valid candidate survives despite the hallucinated one being dropped",
          result == {"selected_urls": ["https://example.com/a"], "reasoning": "..."}, f"got {result}")


def test_select_best_urls_returns_multiple_genuine_candidates():
    print("\n--- select_best_urls can return more than one URL when both genuinely help ---")

    fake_resp = MagicMock()
    fake_resp.raise_for_status.return_value = None
    fake_resp.json.return_value = {
        "choices": [{"message": {"content":
            '{"selected_urls": ["https://example.com/pricing", "https://other.com/specs"], '
            '"reasoning": "one has pricing, the other has specs"}'}}]
    }
    with patch.object(live_url_search, "requests") as mock_requests:
        mock_requests.post.return_value = fake_resp
        result = live_url_search.select_best_urls(
            "price and specs of the laptop",
            [
                {"url": "https://example.com/pricing", "title": "Pricing", "snippet": "s1"},
                {"url": "https://other.com/specs", "title": "Specs", "snippet": "s2"},
            ],
            FAKE_CONFIG,
        )

    check("both genuinely distinct candidates are returned, in order",
          result is not None and result["selected_urls"] == ["https://example.com/pricing", "https://other.com/specs"],
          f"got {result}")


def test_select_best_urls_caps_at_max_selected_urls():
    print(f"\n--- select_best_urls truncates to MAX_SELECTED_URLS ({live_url_search.MAX_SELECTED_URLS}) ---")

    candidates = [
        {"url": f"https://example{i}.com/page", "title": f"Page {i}", "snippet": f"s{i}"}
        for i in range(live_url_search.MAX_SELECTED_URLS + 2)
    ]
    all_urls = [c["url"] for c in candidates]
    fake_resp = MagicMock()
    fake_resp.raise_for_status.return_value = None
    fake_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"selected_urls": all_urls, "reasoning": "all relevant"})}}]
    }
    with patch.object(live_url_search, "requests") as mock_requests:
        mock_requests.post.return_value = fake_resp
        result = live_url_search.select_best_urls("a question", candidates, FAKE_CONFIG)

    check(f"truncated to exactly {live_url_search.MAX_SELECTED_URLS} URL(s)",
          result is not None and len(result["selected_urls"]) == live_url_search.MAX_SELECTED_URLS,
          f"got {result}")
    check("kept the first N in the model's own order",
          result is not None and result["selected_urls"] == all_urls[:live_url_search.MAX_SELECTED_URLS],
          f"got {result}")


# --------------------------------------------------------------------------
# (c) A search failure for one candidate doesn't block inspection of the others
# --------------------------------------------------------------------------

def test_search_failure_for_one_candidate_does_not_block_others():
    print("\n--- (c) One candidate's SearXNG lookup fails -> others still get inspected ---")

    question = "how do I fix my vpn"
    candidates = [
        {"url": "https://good.example.com/vpn-guide", "title": "VPN Guide"},
        {"url": "https://bad.example.com/vpn-guide", "title": "VPN Guide 2"},
        {"url": "https://also-good.example.com/vpn-guide", "title": "VPN Guide 3"},
    ]

    async def fake_searxng_snippet(question, url):
        if "bad.example.com" in url:
            raise RuntimeError("simulated SearXNG failure")
        return f"snippet for {url}"

    with patch.object(live_url_search, "_searxng_snippet", side_effect=fake_searxng_snippet):
        inspected = run(live_url_search.inspect_candidates(question, candidates))

    check("all 3 candidates are still returned despite one search failure", len(inspected) == 3,
          f"got {len(inspected)}")
    by_url = {c["url"]: c for c in inspected}
    check("the failing candidate got an empty snippet, not a crash",
          by_url["https://bad.example.com/vpn-guide"]["snippet"] == "",
          f"got {by_url['https://bad.example.com/vpn-guide']}")
    check("the good candidates got real snippets",
          by_url["https://good.example.com/vpn-guide"]["snippet"] == "snippet for https://good.example.com/vpn-guide"
          and by_url["https://also-good.example.com/vpn-guide"]["snippet"]
          == "snippet for https://also-good.example.com/vpn-guide")


# --------------------------------------------------------------------------
# (c2) _search_with_retry(): retries once on an empty result, gives up
# after max_attempts with nothing to show for it
# --------------------------------------------------------------------------

def test_search_with_retry_recovers_from_one_empty_attempt():
    print("\n--- (c2) _search_with_retry succeeds on the 2nd attempt after an empty 1st ---")

    calls = []

    async def fake_searxng_search(query):
        calls.append(query)
        if len(calls) == 1:
            return []  # simulated empty/transient result on the first attempt
        return [{"title": "t", "url": "https://example.com/x", "snippet": "s"}]

    with patch.object(live_url_search, "searxng_search", side_effect=fake_searxng_search), \
         patch.object(live_url_search.asyncio, "sleep", new=AsyncMock()) as mock_sleep:
        result = run(live_url_search._search_with_retry("a query", max_results=5))

    check("recovered a real result on the retry", result == [{"title": "t", "url": "https://example.com/x", "snippet": "s"}],
          f"got {result}")
    check("searxng_search was called exactly twice (1 empty + 1 retry)", len(calls) == 2, f"got {calls}")
    check("slept once between the empty attempt and the retry", mock_sleep.call_count == 1)


def test_search_with_retry_gives_up_after_max_attempts():
    print("\n--- (c2b) _search_with_retry gives up and returns [] after every attempt is empty ---")

    async def fake_searxng_search(query):
        return []

    with patch.object(live_url_search, "searxng_search", side_effect=fake_searxng_search) as mock_search, \
         patch.object(live_url_search.asyncio, "sleep", new=AsyncMock()):
        result = run(live_url_search._search_with_retry("a query", max_results=5))

    check("result is [] (never raises)", result == [], f"got {result}")
    check(f"tried exactly SEARCH_MAX_ATTEMPTS ({live_url_search.SEARCH_MAX_ATTEMPTS}) times",
          mock_search.call_count == live_url_search.SEARCH_MAX_ATTEMPTS, f"call_count={mock_search.call_count}")


def test_search_with_retry_truncates_to_max_results():
    print("\n--- (c2c) _search_with_retry truncates a larger result set to max_results ---")

    async def fake_searxng_search(query):
        return [{"title": f"t{i}", "url": f"https://example.com/{i}", "snippet": ""} for i in range(10)]

    with patch.object(live_url_search, "searxng_search", side_effect=fake_searxng_search):
        result = run(live_url_search._search_with_retry("a query", max_results=3))

    check("truncated to exactly 3 results", len(result) == 3, f"got {result}")


# --------------------------------------------------------------------------
# (d) Success path: source="live_url" and exactly one entry in "sources"
# --------------------------------------------------------------------------

def test_successful_live_url_answer_shape():
    print("\n--- (d) Successful live URL answer: source='live_url', exactly one source ---")

    question = "how do I fix my vpn"
    candidates = [{"url": "https://example.com/it/vpn-guide", "title": "How to fix my VPN"}]
    inspected = [{"url": candidates[0]["url"], "title": candidates[0]["title"], "snippet": "Restart your VPN client."}]
    selection = {"selected_urls": [candidates[0]["url"]], "reasoning": "This page directly covers VPN troubleshooting."}
    search_result = {
        "content_pieces": [
            {"title": "How to fix my VPN", "url": candidates[0]["url"], "content": "Restart the VPN client and reconnect."},
            {"title": "Sub Page", "url": "https://example.com/it/vpn-guide/details", "content": "More VPN details."},
        ],
        "search_history": [{"query": "site:example.com how do I fix my vpn", "results": []}],
    }
    structured = {
        "not_found": False,
        "subject": "VPN not working",
        "description": "The user's VPN connection is not working.",
        "answer": "Restart your VPN client and reconnect.",
        "category": "IT",
        "sub_category": "Network",
        "follow_up_questions": ["q1", "q2", "q3"],
    }

    with patch.object(live_url_search, "get_url_candidates", return_value=candidates), \
         patch.object(live_url_search, "inspect_candidates", new=AsyncMock(return_value=inspected)), \
         patch.object(live_url_search, "select_best_urls", return_value=selection), \
         patch.object(live_url_search, "iterative_domain_search", new=AsyncMock(return_value=search_result)), \
         patch.object(helpdesk_answer, "generate_structured_response", return_value=structured) as mock_generate:

        result = run(live_url_search.live_url_answer(question, FAKE_CONFIG))

    check("result is not None", result is not None, f"got {result}")
    check("status == answered", result.get("status") == "answered", f"got {result}")
    check("source == 'live_url'", result.get("source") == "live_url", f"got {result.get('source')}")
    check("exactly one entry in sources", len(result.get("sources", [])) == 1, f"got {result.get('sources')}")
    check(
        "the one source is the selected URL with source_type='live_url'",
        result["sources"][0] == {"title": "How to fix my VPN", "url": candidates[0]["url"], "source_type": "live_url"},
        f"got {result['sources']}",
    )
    check("category/subcategory come from the model's inferred fields", result.get("category") == "IT"
          and result.get("subcategory") == "Network")
    check("answer text passed through", result.get("answer") == "Restart your VPN client and reconnect.")

    # generate_structured_response should have been called with BOTH the
    # selected page and its sub-page as chunks, tagged source_type="live_url".
    called_chunks = mock_generate.call_args[0][1]
    check("both crawled pages were passed as chunks", len(called_chunks) == 2, f"got {called_chunks}")
    check("chunks are tagged source_type='live_url'", all(c["source_type"] == "live_url" for c in called_chunks))


def test_not_found_from_generation_returns_none():
    print("\n--- (d2) generate_structured_response says not_found -> live_url_answer returns None ---")

    question = "how do I fix my vpn"
    candidates = [{"url": "https://example.com/it/vpn-guide", "title": "How to fix my VPN"}]
    inspected = [{"url": candidates[0]["url"], "title": candidates[0]["title"], "snippet": "s"}]
    selection = {"selected_urls": [candidates[0]["url"]], "reasoning": "r"}
    search_result = {
        "content_pieces": [{"title": "How to fix my VPN", "url": candidates[0]["url"], "content": "unrelated content"}],
        "search_history": [],
    }

    with patch.object(live_url_search, "get_url_candidates", return_value=candidates), \
         patch.object(live_url_search, "inspect_candidates", new=AsyncMock(return_value=inspected)), \
         patch.object(live_url_search, "select_best_urls", return_value=selection), \
         patch.object(live_url_search, "iterative_domain_search", new=AsyncMock(return_value=search_result)), \
         patch.object(helpdesk_answer, "generate_structured_response",
                       return_value={"not_found": True, "subject": "", "description": "", "answer": "",
                                     "category": "", "sub_category": "", "follow_up_questions": []}):

        result = run(live_url_search.live_url_answer(question, FAKE_CONFIG))

    check("live_url_answer returns None when generation says not_found", result is None, f"got {result}")


# --------------------------------------------------------------------------
# (e) Multiple selected URLs: content merges into one answer
# --------------------------------------------------------------------------

def test_multiple_selected_urls_merge_into_one_answer():
    print("\n--- (e) two selected URLs, both succeed -> merged chunks, two sources ---")

    question = "price and specs of the laptop"
    candidates = [
        {"url": "https://example.com/pricing", "title": "Pricing Page"},
        {"url": "https://other.com/specs", "title": "Specs Page"},
    ]
    inspected = [
        {"url": candidates[0]["url"], "title": candidates[0]["title"], "snippet": "s1"},
        {"url": candidates[1]["url"], "title": candidates[1]["title"], "snippet": "s2"},
    ]
    selection = {"selected_urls": [candidates[0]["url"], candidates[1]["url"]], "reasoning": "both help"}

    def fake_iterative_search(question, url, config):
        if url == candidates[0]["url"]:
            return {
                "content_pieces": [{"title": "Pricing Page", "url": url, "content": "Costs $999."}],
                "search_history": [],
            }
        return {
            "content_pieces": [{"title": "Specs Page", "url": url, "content": "16GB RAM, i7 CPU."}],
            "search_history": [],
        }

    structured = {
        "not_found": False, "subject": "s", "description": "d", "answer": "It costs $999 with 16GB RAM.",
        "category": "IT", "sub_category": "Hardware", "follow_up_questions": ["q1", "q2", "q3"],
    }

    with patch.object(live_url_search, "get_url_candidates", return_value=candidates), \
         patch.object(live_url_search, "inspect_candidates", new=AsyncMock(return_value=inspected)), \
         patch.object(live_url_search, "select_best_urls", return_value=selection), \
         patch.object(live_url_search, "iterative_domain_search", new=AsyncMock(side_effect=fake_iterative_search)), \
         patch.object(helpdesk_answer, "generate_structured_response", return_value=structured) as mock_generate:

        result = run(live_url_search.live_url_answer(question, FAKE_CONFIG))

    check("result is not None", result is not None, f"got {result}")
    check("two entries in sources, one per selected domain",
          result is not None and len(result.get("sources", [])) == 2, f"got {result}")
    check("sources cover both selected URLs",
          result is not None and {s["url"] for s in result["sources"]} == {candidates[0]["url"], candidates[1]["url"]},
          f"got {result.get('sources')}")

    called_chunks = mock_generate.call_args[0][1]
    check("chunks from both domains were merged into one generation call", len(called_chunks) == 2,
          f"got {called_chunks}")
    check("both domains' content made it into the merged chunks",
          {c["url"] for c in called_chunks} == {candidates[0]["url"], candidates[1]["url"]},
          f"got {called_chunks}")


def test_one_of_two_selected_urls_fails_still_answers_from_the_other():
    print("\n--- (e2) one of two selected URLs returns nothing -> still answers from the other ---")

    question = "price and specs of the laptop"
    candidates = [
        {"url": "https://example.com/pricing", "title": "Pricing Page"},
        {"url": "https://dead.example.com/specs", "title": "Specs Page"},
    ]
    inspected = [
        {"url": candidates[0]["url"], "title": candidates[0]["title"], "snippet": "s1"},
        {"url": candidates[1]["url"], "title": candidates[1]["title"], "snippet": "s2"},
    ]
    selection = {"selected_urls": [candidates[0]["url"], candidates[1]["url"]], "reasoning": "both looked relevant"}

    def fake_iterative_search(question, url, config):
        if url == candidates[0]["url"]:
            return {
                "content_pieces": [{"title": "Pricing Page", "url": url, "content": "Costs $999."}],
                "search_history": [],
            }
        return None  # the dead-link domain found nothing

    structured = {
        "not_found": False, "subject": "s", "description": "d", "answer": "It costs $999.",
        "category": "IT", "sub_category": "Hardware", "follow_up_questions": ["q1", "q2", "q3"],
    }

    with patch.object(live_url_search, "get_url_candidates", return_value=candidates), \
         patch.object(live_url_search, "inspect_candidates", new=AsyncMock(return_value=inspected)), \
         patch.object(live_url_search, "select_best_urls", return_value=selection), \
         patch.object(live_url_search, "iterative_domain_search", new=AsyncMock(side_effect=fake_iterative_search)), \
         patch.object(helpdesk_answer, "generate_structured_response", return_value=structured) as mock_generate:

        result = run(live_url_search.live_url_answer(question, FAKE_CONFIG))

    check("result is not None (the successful domain alone is enough)", result is not None, f"got {result}")
    check("exactly one source, from the domain that actually returned content",
          result is not None and result.get("sources") == [
              {"title": "Pricing Page", "url": candidates[0]["url"], "source_type": "live_url"}
          ], f"got {result.get('sources')}")

    called_chunks = mock_generate.call_args[0][1]
    check("only the successful domain's content was used",
          len(called_chunks) == 1 and called_chunks[0]["url"] == candidates[0]["url"], f"got {called_chunks}")


# --------------------------------------------------------------------------
# (f) MediaWiki namespace/action links and cross-domain links are excluded
# from sub-link exploration
# --------------------------------------------------------------------------

def test_is_utility_link_flags_mediawiki_namespaces_and_actions():
    print("\n--- _is_utility_link flags MediaWiki Talk/Category_talk/edit-action links ---")

    check("Category_talk: page is flagged",
          live_url_search._is_utility_link("https://en.wikipedia.org/wiki/Category_talk:Lenovo_laptops"))
    check("Talk: page is flagged",
          live_url_search._is_utility_link("https://en.wikipedia.org/wiki/Talk:Python_(programming_language)"))
    check("?action=edit is flagged",
          live_url_search._is_utility_link(
              "https://en.wikipedia.org/w/index.php?title=Category:Lenovo_laptops&action=edit"))
    check("a genuine article page is NOT flagged",
          not live_url_search._is_utility_link("https://en.wikipedia.org/wiki/Lenovo"))


def test_build_sublink_candidates_drops_cross_domain_links():
    print("\n--- _build_sublink_candidates drops links to a different domain than the main page ---")

    links = [
        {"url": "https://en.wikipedia.org/wiki/Lenovo_Legion", "title": "Lenovo Legion"},
        # Same "topic", different domain (e.g. an interlanguage link) — must
        # be excluded even though nothing about it looks like a utility link.
        {"url": "https://pt.wikipedia.org/wiki/Categoria:Laptops_da_Lenovo", "title": "Categoria Laptops Lenovo"},
    ]
    candidates = live_url_search._build_sublink_candidates(links, "https://en.wikipedia.org/wiki/Lenovo")
    urls = {c["url"] for c in candidates}
    check("same-domain link is kept", "https://en.wikipedia.org/wiki/Lenovo_Legion" in urls, f"got {urls}")
    check("cross-domain link is dropped", "https://pt.wikipedia.org/wiki/Categoria:Laptops_da_Lenovo" not in urls,
          f"got {urls}")


# --------------------------------------------------------------------------
# iterative_domain_search(): the bounded search-refine loop
# --------------------------------------------------------------------------

def _think(action, selected_url=None, refined_query=None, reasoning="r"):
    return {"action": action, "selected_url": selected_url, "refined_query": refined_query, "reasoning": reasoning}


def test_iter_a_select_on_first_iteration_stops_immediately():
    print("\n--- iterative (a) 'select' on iteration 1 stops immediately, no further iterations ---")

    with patch.object(live_url_search, "_search_with_retry",
                       return_value=[{"title": "VPN fix", "url": "https://example.com/vpn-fix", "snippet": "s"}]) as mock_search, \
         patch.object(live_url_search, "_observe_and_think",
                       return_value=_think("select", selected_url="https://example.com/vpn-fix")) as mock_think, \
         patch.object(live_url_search, "fetch_page_plain",
                       return_value={"title": "VPN fix", "url": "https://example.com/vpn-fix", "content": "Restart your VPN.", "links": []}) as mock_fetch:

        result = run(live_url_search.iterative_domain_search(
            "how do I fix my vpn", "https://example.com/it-guide", FAKE_CONFIG, max_iterations=3
        ))

    check("result is not None", result is not None, f"got {result}")
    check("exactly one content piece", len(result.get("content_pieces", [])) == 1, f"got {result}")
    check("content piece is the selected URL's content",
          result["content_pieces"][0] == {"title": "VPN fix", "url": "https://example.com/vpn-fix", "content": "Restart your VPN."},
          f"got {result['content_pieces']}")
    check("search called exactly once (no further iterations)", mock_search.call_count == 1,
          f"call_count={mock_search.call_count}")
    check("think-step called exactly once", mock_think.call_count == 1)
    check("fetch_page_plain called exactly once, for the selected URL",
          mock_fetch.call_count == 1 and mock_fetch.call_args[0] == ("https://example.com/vpn-fix",))
    check("exactly one entry in search_history", len(result.get("search_history", [])) == 1)


def test_iter_b_refine_carries_new_query_into_next_iteration():
    print("\n--- iterative (b) 'refine' carries the new query into the next iteration's search ---")

    search_calls = []

    def fake_search(query, max_results=5):
        search_calls.append(query)
        return [{"title": "some result", "url": "https://example.com/x", "snippet": "s"}]

    think_calls = []

    def fake_think(question, search_history, config):
        think_calls.append([turn["query"] for turn in search_history])
        if len(search_history) == 1:
            return _think("refine", refined_query="more specific vpn error terms")
        return _think("select", selected_url="https://example.com/found-it")

    with patch.object(live_url_search, "_search_with_retry", side_effect=fake_search), \
         patch.object(live_url_search, "_observe_and_think", side_effect=fake_think), \
         patch.object(live_url_search, "fetch_page_plain",
                       return_value={"title": "Found", "url": "https://example.com/found-it", "content": "The answer.", "links": []}):

        result = run(live_url_search.iterative_domain_search(
            "how do I fix my vpn", "https://example.com/it-guide", FAKE_CONFIG, max_iterations=3
        ))

    check("result is not None", result is not None, f"got {result}")
    check("exactly 2 search queries were issued", len(search_calls) == 2, f"got {search_calls}")
    check("first query used the original question", "how do I fix my vpn" in search_calls[0], f"got {search_calls[0]!r}")
    check(
        "second query used the refined_query from the first think-step, not the original question",
        "more specific vpn error terms" in search_calls[1] and "how do I fix my vpn" not in search_calls[1],
        f"got {search_calls[1]!r}",
    )
    check("second think-step saw both iterations' history", len(think_calls[1]) == 2, f"got {think_calls}")


def test_strip_site_prefix_removes_model_added_site_scope():
    print("\n--- _strip_site_prefix removes a model-added 'site:' prefix from its own refined_query ---")

    check("a leading site: prefix is stripped",
          live_url_search._strip_site_prefix("site:en.wikipedia.org Lenovo ThinkPad") == "Lenovo ThinkPad")
    check("a doubled-up site: prefix (both instances) is stripped",
          live_url_search._strip_site_prefix("site:en.wikipedia.org site:en.wikipedia.org Lenovo") == "Lenovo")
    check("a query with no site: prefix at all is left unchanged",
          live_url_search._strip_site_prefix("Lenovo ThinkPad laptops") == "Lenovo ThinkPad laptops")


def test_iter_b2_refine_query_with_models_own_site_prefix_is_not_doubled():
    print("\n--- iterative (b2) a refine query that includes the model's own 'site:' prefix isn't doubled up ---")

    # Reproduces the real bug found via live testing (gpt-5.1): the model's
    # refined_query sometimes echoes "site:domain" itself (likely because
    # the history shown to it already displays queries in that form),
    # which — without stripping — doubled into a malformed
    # "site:x site:x actual terms" query on the next round.
    search_calls = []

    def fake_search(query, max_results=5):
        search_calls.append(query)
        return [{"title": "some result", "url": "https://example.com/x", "snippet": "s"}]

    def fake_think(question, search_history, config):
        if len(search_history) == 1:
            return _think("refine", refined_query="site:example.com ThinkPad laptops")
        return _think("select", selected_url="https://example.com/found-it")

    with patch.object(live_url_search, "_search_with_retry", side_effect=fake_search), \
         patch.object(live_url_search, "_observe_and_think", side_effect=fake_think), \
         patch.object(live_url_search, "fetch_page_plain",
                       return_value={"title": "Found", "url": "https://example.com/found-it", "content": "The answer.", "links": []}):

        run(live_url_search.iterative_domain_search(
            "how do I fix my vpn", "https://example.com/it-guide", FAKE_CONFIG, max_iterations=3
        ))

    check("the domain's site: prefix appears exactly once in the second query, not doubled",
          search_calls[1].count("site:example.com") == 1, f"got {search_calls[1]!r}")
    check("the model's own search terms still made it into the query",
          "ThinkPad laptops" in search_calls[1], f"got {search_calls[1]!r}")


def test_iter_c_give_up_stops_early_with_iterations_remaining():
    print("\n--- iterative (c) 'give_up' stops early even though iterations remain ---")

    with patch.object(live_url_search, "_search_with_retry", return_value=[]) as mock_search, \
         patch.object(live_url_search, "_observe_and_think", return_value=_think("give_up")) as mock_think, \
         patch.object(live_url_search, "fetch_page_plain") as mock_fetch:

        result = run(live_url_search.iterative_domain_search(
            "an unanswerable question", "https://example.com/it-guide", FAKE_CONFIG, max_iterations=3
        ))

    check("result is None", result is None, f"got {result}")
    check("stopped after iteration 1, not all 3", mock_search.call_count == 1, f"call_count={mock_search.call_count}")
    check("think-step called exactly once", mock_think.call_count == 1)
    check("fetch_page_plain never called (nothing was selected)", mock_fetch.call_count == 0)


def test_iter_d_repeated_refine_falls_back_to_top_search_result():
    print("\n--- iterative (d) model keeps refining into a repeated query -> falls back to top search result ---")

    # Reproduces the real bug: search returns real results every round, but the
    # model keeps saying "refine" with the SAME refined_query instead of ever
    # selecting or giving up. Real results shouldn't be discarded for that.
    with patch.object(live_url_search, "_search_with_retry",
                       return_value=[{"title": "t", "url": "https://example.com/x", "snippet": "s"}]) as mock_search, \
         patch.object(live_url_search, "_observe_and_think",
                       return_value=_think("refine", refined_query="still not specific enough")) as mock_think, \
         patch.object(live_url_search, "fetch_page_plain",
                       return_value={"title": "Found via fallback", "url": "https://example.com/x", "content": "The answer.", "links": []}) as mock_fetch:

        result = run(live_url_search.iterative_domain_search(
            "a hard question", "https://example.com/it-guide", FAKE_CONFIG, max_iterations=3
        ))

    check("result is not None (falls back rather than giving up on real results)", result is not None, f"got {result}")
    check("fell back to the top search result's URL",
          result and result["content_pieces"][0]["url"] == "https://example.com/x", f"got {result}")
    check(
        "stopped after 2 searches, not all 3 (repeated query detected early)",
        mock_search.call_count == 2, f"call_count={mock_search.call_count}",
    )
    check("think-step called exactly 2 times (matches the 2 searches)", mock_think.call_count == 2)
    check("fetch_page_plain called exactly once, for the fallback URL", mock_fetch.call_count == 1)


def test_iter_d2_true_exhaustion_with_no_results_returns_none():
    print("\n--- iterative (d2) genuinely distinct refinements but zero results throughout -> None ---")

    refine_calls = []

    def fake_think(question, search_history, config):
        n = len(search_history)
        refine_calls.append(n)
        return _think("refine", refined_query=f"genuinely distinct query attempt {n}")

    with patch.object(live_url_search, "_search_with_retry", return_value=[]) as mock_search, \
         patch.object(live_url_search, "_observe_and_think", side_effect=fake_think), \
         patch.object(live_url_search, "fetch_page_plain") as mock_fetch:

        result = run(live_url_search.iterative_domain_search(
            "a hard question", "https://example.com/it-guide", FAKE_CONFIG, max_iterations=3
        ))

    check("result is None (nothing to fall back to — every iteration had 0 results)", result is None, f"got {result}")
    check("ran all 3 iterations (queries were genuinely distinct each time, not a repeat)",
          mock_search.call_count == 3, f"call_count={mock_search.call_count}")
    check("fetch_page_plain never called (no result existed to fetch)", mock_fetch.call_count == 0)


def test_iter_g_give_up_is_never_overridden_by_fallback():
    print("\n--- iterative (g) explicit 'give_up' is always honored, even with real results available ---")

    with patch.object(live_url_search, "_search_with_retry",
                       return_value=[{"title": "t", "url": "https://example.com/x", "snippet": "s"}]), \
         patch.object(live_url_search, "_observe_and_think", return_value=_think("give_up")), \
         patch.object(live_url_search, "fetch_page_plain") as mock_fetch:

        result = run(live_url_search.iterative_domain_search(
            "a hard question", "https://example.com/it-guide", FAKE_CONFIG, max_iterations=3
        ))

    check("result is None — give_up is respected even though search had a real result available",
          result is None, f"got {result}")
    check("fetch_page_plain never called (give_up must not trigger the fallback)", mock_fetch.call_count == 0)


def test_iter_e_failed_fetch_after_select_treated_as_none():
    print("\n--- iterative (e) fetch failure after a successful 'select' -> None, not a crash ---")

    with patch.object(live_url_search, "_search_with_retry",
                       return_value=[{"title": "t", "url": "https://example.com/dead-link", "snippet": "s"}]), \
         patch.object(live_url_search, "_observe_and_think",
                       return_value=_think("select", selected_url="https://example.com/dead-link")), \
         patch.object(live_url_search, "fetch_page_plain",
                       return_value={"url": "https://example.com/dead-link", "content": "", "links": []}) as mock_fetch:

        result = run(live_url_search.iterative_domain_search(
            "how do I fix my vpn", "https://example.com/it-guide", FAKE_CONFIG, max_iterations=3
        ))

    check("result is None (no crash)", result is None, f"got {result}")
    check("fetch_page_plain was attempted for the selected URL", mock_fetch.call_count == 1)


def test_iter_h_select_also_pulls_in_relevant_inner_sublinks():
    print("\n--- iterative (h) a successful 'select' also explores the page's own relevant sub-links ---")

    def fake_fetch(url):
        if url == "https://example.com/main":
            return {
                "title": "Main Page", "url": url,
                "content": "Main page content about the topic.",
                "links": [
                    {"url": "https://example.com/sub-topic-detail", "title": "topic detail sub page"},
                    {"url": "https://example.com/cart", "title": "Cart"},  # utility link, must be excluded
                ],
            }
        return {
            "title": "Sub Page", "url": url,
            "content": "Sub page content about the topic in real detail. " * 5,  # clear MIN_SUBPAGE_CONTENT_CHARS
            "links": [],
        }

    with patch.object(live_url_search, "_search_with_retry",
                       return_value=[{"title": "Main", "url": "https://example.com/main", "snippet": "s"}]), \
         patch.object(live_url_search, "_observe_and_think",
                       return_value=_think("select", selected_url="https://example.com/main")), \
         patch.object(live_url_search, "fetch_page_plain", side_effect=fake_fetch) as mock_fetch:

        result = run(live_url_search.iterative_domain_search(
            "topic detail", "https://example.com/it-guide", FAKE_CONFIG, max_iterations=3
        ))

    check("result is not None", result is not None, f"got {result}")
    pieces = result["content_pieces"] if result else []
    check("2 content pieces: main page + the relevant sub-link", len(pieces) == 2, f"got {pieces}")
    check("main page is first", pieces[0]["url"] == "https://example.com/main" if pieces else False)
    check(
        "the relevant sub-link was pulled in, the utility (cart) link was not",
        len(pieces) == 2 and pieces[1]["url"] == "https://example.com/sub-topic-detail",
        f"got {pieces}",
    )
    check("fetch_page_plain called for main + exactly one sub-link (cart excluded)", mock_fetch.call_count == 2,
          f"call_count={mock_fetch.call_count}")


if __name__ == "__main__":
    test_no_matching_candidates_skips_search_entirely()
    test_small_pool_lets_borderline_candidate_through()
    test_select_best_urls_null_propagates_to_none()
    test_select_best_urls_itself_returns_none_on_empty_selection()
    test_select_best_urls_rejects_hallucinated_url()
    test_select_best_urls_keeps_valid_ones_and_drops_hallucinated()
    test_select_best_urls_returns_multiple_genuine_candidates()
    test_select_best_urls_caps_at_max_selected_urls()
    test_search_failure_for_one_candidate_does_not_block_others()
    test_search_with_retry_recovers_from_one_empty_attempt()
    test_search_with_retry_gives_up_after_max_attempts()
    test_search_with_retry_truncates_to_max_results()
    test_successful_live_url_answer_shape()
    test_not_found_from_generation_returns_none()
    test_multiple_selected_urls_merge_into_one_answer()
    test_one_of_two_selected_urls_fails_still_answers_from_the_other()
    test_is_utility_link_flags_mediawiki_namespaces_and_actions()
    test_build_sublink_candidates_drops_cross_domain_links()

    test_iter_a_select_on_first_iteration_stops_immediately()
    test_iter_b_refine_carries_new_query_into_next_iteration()
    test_strip_site_prefix_removes_model_added_site_scope()
    test_iter_b2_refine_query_with_models_own_site_prefix_is_not_doubled()
    test_iter_c_give_up_stops_early_with_iterations_remaining()
    test_iter_d_repeated_refine_falls_back_to_top_search_result()
    test_iter_d2_true_exhaustion_with_no_results_returns_none()
    test_iter_e_failed_fetch_after_select_treated_as_none()
    test_iter_g_give_up_is_never_overridden_by_fallback()
    test_iter_h_select_also_pulls_in_relevant_inner_sublinks()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        raise SystemExit(1)
    else:
        print("All live URL search tests passed.")
