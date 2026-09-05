"""
Local tests for the direct-crawl live URL search (crawl_url_search.py).
No real network needed — requests/HTTP fetches and the OpenAI call are
all mocked.

Usage:
    python test_crawl_url_search.py
"""

import asyncio
from unittest.mock import AsyncMock, patch

import crawl_url_search
import helpdesk_answer

FAILURES = []


def check(label: str, condition: bool, detail: str = ""):
    if condition:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label} {detail}")
        FAILURES.append(label)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# (a) _is_utility_link
# --------------------------------------------------------------------------

def test_is_utility_link_variants():
    print("\n--- _is_utility_link flags query strings, colons, social domains, utility paths ---")

    check("a query string is flagged",
          crawl_url_search._is_utility_link("https://en.wikipedia.org/w/index.php?title=X&action=edit"))
    check("a colon in the last path segment is flagged (MediaWiki namespace convention)",
          crawl_url_search._is_utility_link("https://en.wikipedia.org/wiki/Talk:Python"))
    check("a known social-media domain is flagged",
          crawl_url_search._is_utility_link("https://www.facebook.com/SomeCompany"))
    check("a utility path prefix is flagged",
          crawl_url_search._is_utility_link("https://example.com/cart/checkout"))
    check("a genuine content page is NOT flagged",
          not crawl_url_search._is_utility_link("https://example.com/blogs/how-to-fix-vpn"))


# --------------------------------------------------------------------------
# (b) extract_links / extract_title / extract_text
# --------------------------------------------------------------------------

SAMPLE_HTML = """
<html><head><title> My Page Title </title></head>
<body>
<nav>Nav chrome that should be stripped</nav>
<header>Header chrome</header>
<h1>Fallback Heading</h1>
<p>Real content paragraph one.</p>
<p>Real content paragraph two.</p>
<a href="/same-domain-page">Same domain link</a>
<a href="https://other-domain.com/page">Cross domain link</a>
<a href="#section">Same-page anchor</a>
<a href="mailto:test@example.com">Email link</a>
<a href="/cart">Cart link</a>
<a href="https://example.com/same-domain-page#frag">Duplicate via fragment</a>
<footer>Footer chrome that should be stripped</footer>
</body></html>
"""


def test_extract_title_prefers_title_tag():
    print("\n--- extract_title prefers <title>, falls back to <h1>, then to the given fallback ---")

    check("title tag is used and stripped of whitespace",
          crawl_url_search.extract_title(SAMPLE_HTML, "fallback") == "My Page Title")
    check("falls back to h1 when there's no title tag",
          crawl_url_search.extract_title("<html><body><h1>Only Heading</h1></body></html>", "fallback") == "Only Heading")
    check("falls back to the given fallback when neither exists",
          crawl_url_search.extract_title("<html><body><p>no headings</p></body></html>", "fallback") == "fallback")


def test_extract_links_filters_correctly():
    print("\n--- extract_links keeps only same-domain, non-utility, deduped links ---")

    links = crawl_url_search.extract_links(SAMPLE_HTML, "https://example.com/start-page")
    urls = {l["url"] for l in links}

    check("same-domain link is kept", "https://example.com/same-domain-page" in urls, f"got {urls}")
    check("cross-domain link is dropped", "https://other-domain.com/page" not in urls, f"got {urls}")
    check("mailto link is dropped", not any(u.startswith("mailto:") for u in urls), f"got {urls}")
    check("cart (utility path) link is dropped", "https://example.com/cart" not in urls, f"got {urls}")
    check("a link that only differs by #fragment is deduped away", len(urls) == 1, f"got {urls}")


def test_extract_text_strips_chrome_and_truncates():
    print("\n--- extract_text strips nav/header/footer and truncates to max_chars ---")

    text = crawl_url_search.extract_text(SAMPLE_HTML)
    check("nav chrome is stripped", "Nav chrome" not in text, f"got {text!r}")
    check("footer chrome is stripped", "Footer chrome" not in text, f"got {text!r}")
    check("real content survives", "Real content paragraph one." in text, f"got {text!r}")

    long_html = "<html><body><p>" + ("word " * 5000) + "</p></body></html>"
    truncated = crawl_url_search.extract_text(long_html, max_chars=50)
    check("truncates to max_chars", len(truncated) == 50, f"got {len(truncated)}")


# --------------------------------------------------------------------------
# (c) rank_candidates
# --------------------------------------------------------------------------

def test_rank_candidates_sorts_and_limits():
    print("\n--- rank_candidates sorts by fuzzy title match, descending, limited to top_n ---")

    candidates = [
        {"url": "https://example.com/a", "title": "Completely unrelated topic"},
        {"url": "https://example.com/b", "title": "how to fix my vpn connection"},
        {"url": "https://example.com/c", "title": "vpn troubleshooting guide"},
    ]
    ranked = crawl_url_search.rank_candidates("how do I fix my vpn", candidates, top_n=2)

    check("exactly top_n results returned", len(ranked) == 2, f"got {len(ranked)}")
    check("results are sorted by score descending",
          ranked[0][0] >= ranked[1][0], f"got {[r[0] for r in ranked]}")
    check("the unrelated candidate did not make the top 2",
          all(c["url"] != "https://example.com/a" for _, c in ranked), f"got {ranked}")


def test_rank_candidates_short_specific_title_beats_generic_unrelated_one():
    print("\n--- rank_candidates: a short specific title beats a longer unrelated one (real bug found live) ---")

    # Reproduces the exact bug found live: unnormalized token_set_ratio
    # scored "Lenovo" (29.4) BELOW "Computer data storage" (36.7) against
    # "tell me about lenovo laptops" — a filler-word-heavy question penalizes
    # a short, correct title unless both sides are normalized first.
    candidates = [
        {"url": "https://en.wikipedia.org/wiki/Lenovo", "title": "Lenovo"},
        {"url": "https://en.wikipedia.org/wiki/Computer_data_storage", "title": "Computer data storage"},
        {"url": "https://en.wikipedia.org/wiki/Optional_typing", "title": "Optional typing"},
    ]
    ranked = crawl_url_search.rank_candidates("tell me about lenovo laptops", candidates, top_n=1)

    check("the actual Lenovo page wins, not an unrelated Wikipedia article",
          ranked[0][1]["url"] == "https://en.wikipedia.org/wiki/Lenovo", f"got {ranked}")


# --------------------------------------------------------------------------
# (d) build_candidate_pool
# --------------------------------------------------------------------------

def test_build_candidate_pool_includes_seed_and_sublinks():
    print("\n--- build_candidate_pool includes each seed plus its filtered sub-links ---")

    def fake_fetch_page(url):
        if url == "https://example.com/seed":
            return {
                "url": url, "title": "Seed Page",
                "html": '<html><body><a href="/vpn-guide">VPN Guide</a><a href="/cart">Cart</a></body></html>',
            }
        return None

    with patch.object(crawl_url_search, "_fetch_page", side_effect=fake_fetch_page):
        pool = run(crawl_url_search.build_candidate_pool(["https://example.com/seed"]))

    urls = {c["url"] for c in pool}
    check("the seed itself is a candidate", "https://example.com/seed" in urls, f"got {urls}")
    check("its sub-link is a candidate", "https://example.com/vpn-guide" in urls, f"got {urls}")
    check("the utility (cart) sub-link is excluded", "https://example.com/cart" not in urls, f"got {urls}")


def test_build_candidate_pool_skips_failed_seed():
    print("\n--- build_candidate_pool skips a seed that fails to fetch, without crashing ---")

    def fake_fetch_page(url):
        if url == "https://good.example.com":
            return {"url": url, "title": "Good", "html": "<html><body></body></html>"}
        return None  # simulates a failed fetch (dead link, timeout, etc.)

    with patch.object(crawl_url_search, "_fetch_page", side_effect=fake_fetch_page):
        pool = run(crawl_url_search.build_candidate_pool(["https://good.example.com", "https://dead.example.com"]))

    check("only the good seed made it into the pool", {c["url"] for c in pool} == {"https://good.example.com"},
          f"got {pool}")


# --------------------------------------------------------------------------
# (e) crawl_url_answer(): the full orchestration
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# (e0) embedding-based ranking/filtering — the fix for two real bugs found
# live: fuzzy text matching (a) confused a generic Wikipedia article
# titled "Programming language implementation" for the real Python page
# when asked about Python's implementations (perfect literal word match,
# wrong page), and (b) let a laptop retailer's own sub-pages crowd out
# "Lenovo" for a generic "laptops" question, since "Lenovo" alone shares
# no words with "laptop". Embeddings compare meaning, not characters.
# --------------------------------------------------------------------------

def test_cosine_similarity_basics():
    print("\n--- _cosine_similarity: identical, opposite, and orthogonal vectors ---")

    check("identical vectors score 1.0", crawl_url_search._cosine_similarity([1, 0], [1, 0]) == 1.0)
    check("orthogonal vectors score 0.0", crawl_url_search._cosine_similarity([1, 0], [0, 1]) == 0.0)
    check("opposite vectors score -1.0", crawl_url_search._cosine_similarity([1, 0], [-1, 0]) == -1.0)
    check("a zero vector scores 0.0 (no divide-by-zero crash)", crawl_url_search._cosine_similarity([0, 0], [1, 1]) == 0.0)


def test_rank_candidates_by_embedding_uses_meaning_not_literal_text():
    print("\n--- rank_candidates_by_embedding picks the semantically closer candidate ---")
    print("Real bug this fixes: fuzzy text matching scored a generic 'Programming language")
    print("implementation' article above the actual Python page for a Python-implementations question.")

    candidates = [
        {"url": "https://en.wikipedia.org/wiki/Programming_language_implementation", "title": "Programming language implementation"},
        {"url": "https://en.wikipedia.org/wiki/Python_(programming_language)", "title": "Python (programming language)"},
    ]
    # Contrived embeddings: candidate 2 (the real Python page) is closer in
    # direction to the question than candidate 1, despite candidate 1
    # sharing the literal word "implementation" with the question text.
    question_embedding = [1.0, 0.0]
    embeddings = {
        "Programming language implementation": [0.0, 1.0],   # orthogonal -> unrelated
        "Python (programming language)": [0.9, 0.1],           # close -> relevant
    }

    with patch("embedding_client.get_embedding", return_value=question_embedding), \
         patch("embedding_client.get_embeddings_batch",
               side_effect=lambda texts: [embeddings[t] for t in texts]):
        ranked = crawl_url_search.rank_candidates_by_embedding(
            "What are the major implementations of Python?", candidates, top_n=2
        )

    check("the real Python page ranks first", ranked[0][1]["title"] == "Python (programming language)", f"got {ranked}")
    check("the unrelated generic article ranks last", ranked[1][1]["title"] == "Programming language implementation", f"got {ranked}")


def test_filter_relevant_seeds_keeps_semantically_related_seed():
    print("\n--- filter_relevant_seeds keeps a seed related by meaning, not literal words ---")
    print("Real bug this fixes: 'Lenovo' shares no words with 'laptops', so fuzzy matching")
    print("scored it low against a generic laptops question and a different site crowded it out.")

    tracked = [
        {"url": "https://www.lenovo.com/", "title": "Lenovo"},
        {"url": "https://en.wikipedia.org/wiki/Python_(programming_language)", "title": "python"},
    ]
    question_embedding = [1.0, 0.0]
    embeddings = {"Lenovo": [0.9, 0.1], "python": [0.0, 1.0]}

    with patch("embedding_client.get_embedding", return_value=question_embedding), \
         patch("embedding_client.get_embeddings_batch",
               side_effect=lambda texts: [embeddings[t] for t in texts]), \
         patch.object(crawl_url_search, "SEED_RELEVANCE_THRESHOLD", 50.0):
        relevant = crawl_url_search.filter_relevant_seeds("tell me about laptops", tracked)

    check("only the Lenovo seed is kept", [t["title"] for t in relevant] == ["Lenovo"], f"got {relevant}")


def test_filter_relevant_seeds_fails_open_when_nothing_clears_threshold():
    print("\n--- filter_relevant_seeds keeps every seed if none clear the threshold (fail open) ---")

    tracked = [{"url": "https://a.example", "title": "a"}, {"url": "https://b.example", "title": "b"}]
    with patch("embedding_client.get_embedding", return_value=[1.0, 0.0]), \
         patch("embedding_client.get_embeddings_batch", return_value=[[0.0, 1.0], [0.0, 1.0]]), \
         patch.object(crawl_url_search, "SEED_RELEVANCE_THRESHOLD", 50.0):
        relevant = crawl_url_search.filter_relevant_seeds("unrelated question", tracked)

    check("all seeds kept when nothing clears the threshold", relevant == tracked, f"got {relevant}")


def test_filter_relevant_seeds_fails_open_on_embedding_error():
    print("\n--- filter_relevant_seeds keeps every seed if the embedding call itself fails ---")

    tracked = [{"url": "https://a.example", "title": "a"}]
    with patch("embedding_client.get_embedding", side_effect=RuntimeError("network down")):
        relevant = crawl_url_search.filter_relevant_seeds("any question", tracked)

    check("all seeds kept when the relevance check itself errors", relevant == tracked, f"got {relevant}")


def test_crawl_url_answer_no_seeds_returns_none():
    print("\n--- crawl_url_answer returns None when there are no tracked seed URLs ---")

    with patch.object(crawl_url_search, "get_url_candidates", return_value=[]):
        result = run(crawl_url_search.crawl_url_answer("any question", {}))

    check("result is None", result is None, f"got {result}")


def test_crawl_url_answer_success_with_citation_based_sources():
    print("\n--- crawl_url_answer: successful answer, sources come from the model's citation ---")

    candidates = [{"url": "https://example.com/seed", "title": "seed"}]
    pool = [
        {"url": "https://example.com/seed", "title": "VPN Troubleshooting", "seed": "https://example.com/seed"},
        {"url": "https://example.com/unrelated", "title": "Unrelated Page", "seed": "https://example.com/seed"},
    ]

    def fake_fetch_page(url):
        return {"url": url, "title": "T", "html": f"<html><body>Content for {url}</body></html>"}

    structured = {
        "not_found": False, "subject": "s", "description": "d",
        "answer": "Restart your VPN client.",
        "answer_reference_numbers": [1],  # cites only the first (top-ranked) chunk
        "category": "IT", "sub_category": "VPN",
        "follow_up_questions": [{"question": "What if it still fails?", "reference_number": 1}],
    }

    with patch.object(crawl_url_search, "get_url_candidates", return_value=candidates), \
         patch.object(crawl_url_search, "filter_relevant_seeds", side_effect=lambda q, tracked: tracked), \
         patch.object(crawl_url_search, "build_candidate_pool", new=AsyncMock(return_value=pool)), \
         patch.object(crawl_url_search, "rank_candidates_by_embedding", return_value=[(95.0, pool[0]), (10.0, pool[1])]), \
         patch.object(crawl_url_search, "_fetch_page", side_effect=fake_fetch_page), \
         patch.object(helpdesk_answer, "generate_structured_response", return_value=structured), \
         patch.object(helpdesk_answer, "_follow_up_answerable", return_value=True):
        # _follow_up_answerable does its own live classify-model call —
        # irrelevant to what THIS test checks (citation-based sourcing),
        # so it's stubbed to always pass; see test_structured_response.py
        # for its own dedicated tests.

        result = run(crawl_url_search.crawl_url_answer("how do I fix my vpn", {}))

    check("result is not None", result is not None, f"got {result}")
    check("status is answered", result.get("status") == "answered", f"got {result}")
    check("source is live_url", result.get("source") == "live_url", f"got {result}")
    check("exactly one cited source (not both fetched pages)", len(result.get("sources", [])) == 1,
          f"got {result.get('sources')}")
    check("the cited source is the top-ranked VPN page, not the unrelated one",
          result["sources"][0]["url"] == "https://example.com/seed", f"got {result.get('sources')}")
    check("the follow-up question survived (valid reference)",
          result.get("follow_up_questions") == ["What if it still fails?"], f"got {result}")


def test_crawl_url_answer_passes_conversation_history_through():
    print("\n--- crawl_url_answer forwards conversation_history to generate_structured_response ---")

    candidates = [{"url": "https://example.com/seed", "title": "seed"}]
    pool = [{"url": "https://example.com/seed", "title": "VPN Troubleshooting", "seed": "https://example.com/seed"}]
    history = [{"question": "What is a VPN?", "answer": "A virtual private network."}]

    def fake_fetch_page(url):
        return {"url": url, "title": "T", "html": "<html><body>content</body></html>"}

    structured = {
        "not_found": False, "subject": "s", "description": "d",
        "answer": "Restart your VPN client.",
        "answer_reference_numbers": [1],
        "category": "IT", "sub_category": "VPN", "follow_up_questions": [],
    }

    with patch.object(crawl_url_search, "get_url_candidates", return_value=candidates), \
         patch.object(crawl_url_search, "filter_relevant_seeds", side_effect=lambda q, tracked: tracked), \
         patch.object(crawl_url_search, "build_candidate_pool", new=AsyncMock(return_value=pool)), \
         patch.object(crawl_url_search, "rank_candidates_by_embedding", return_value=[(95.0, pool[0])]), \
         patch.object(crawl_url_search, "_fetch_page", side_effect=fake_fetch_page), \
         patch.object(helpdesk_answer, "generate_structured_response", return_value=structured) as mock_generate:

        run(crawl_url_search.crawl_url_answer("it still won't connect", {}, history))

    check("generate_structured_response was called with the conversation history",
          mock_generate.call_args.args[2] == history, f"got call args {mock_generate.call_args}")


def test_crawl_url_answer_not_found_returns_none():
    print("\n--- crawl_url_answer returns None when generation says not_found ---")

    candidates = [{"url": "https://example.com/seed", "title": "seed"}]
    pool = [{"url": "https://example.com/seed", "title": "Some Page", "seed": "https://example.com/seed"}]

    def fake_fetch_page(url):
        return {"url": url, "title": "T", "html": "<html><body>irrelevant content</body></html>"}

    with patch.object(crawl_url_search, "get_url_candidates", return_value=candidates), \
         patch.object(crawl_url_search, "filter_relevant_seeds", side_effect=lambda q, tracked: tracked), \
         patch.object(crawl_url_search, "build_candidate_pool", new=AsyncMock(return_value=pool)), \
         patch.object(crawl_url_search, "rank_candidates_by_embedding", return_value=[(50.0, pool[0])]), \
         patch.object(crawl_url_search, "_fetch_page", side_effect=fake_fetch_page), \
         patch.object(helpdesk_answer, "generate_structured_response",
                       return_value={"not_found": True, "subject": "", "description": "", "answer": "",
                                     "answer_reference_numbers": [], "category": "", "sub_category": "",
                                     "follow_up_questions": []}):

        result = run(crawl_url_search.crawl_url_answer("an unanswerable question", {}))

    check("result is None", result is None, f"got {result}")


if __name__ == "__main__":
    test_is_utility_link_variants()
    test_extract_title_prefers_title_tag()
    test_extract_links_filters_correctly()
    test_extract_text_strips_chrome_and_truncates()
    test_rank_candidates_sorts_and_limits()
    test_rank_candidates_short_specific_title_beats_generic_unrelated_one()
    test_build_candidate_pool_includes_seed_and_sublinks()
    test_build_candidate_pool_skips_failed_seed()
    test_cosine_similarity_basics()
    test_rank_candidates_by_embedding_uses_meaning_not_literal_text()
    test_filter_relevant_seeds_keeps_semantically_related_seed()
    test_filter_relevant_seeds_fails_open_when_nothing_clears_threshold()
    test_filter_relevant_seeds_fails_open_on_embedding_error()
    test_crawl_url_answer_no_seeds_returns_none()
    test_crawl_url_answer_success_with_citation_based_sources()
    test_crawl_url_answer_passes_conversation_history_through()
    test_crawl_url_answer_not_found_returns_none()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        raise SystemExit(1)
    else:
        print("All crawl-based URL search tests passed.")
