"""
Standalone diagnostic for live_url_search.py's per-question flow.
------------------------------------------------------------------

Walks through every step of live_url_answer() and prints full detail at
each stage — every candidate's fuzzy-match score (not just survivors),
every SearXNG snippet, the AI selection's reasoning — then hands off to
iterative_domain_search()'s own bounded search-refine loop, whose
per-iteration query/results/decision trace is exposed via the SAME
INFO-level logging the real production path uses (see live_url_search.py's
logger.info calls in iterative_domain_search/_observe_and_think) — this
script just turns that logging on and prints it to the console, so what
you see here is exactly what Application Insights would show for a real
run, not a separate reimplementation that could drift out of sync.

Requires the same Azure OpenAI / SharePoint config as the real app (reads
from environment variables, same as live_url_search.py itself) — the
iterative search step needs OpenAI credentials regardless of --url, since
it makes a real "observe and think" call each iteration even when
candidate selection is bypassed.

Usage:
    python debug_live_url_search.py "my laptop is showing a blue screen"
    python debug_live_url_search.py "some question" --url https://known-page.com
"""

import argparse
import asyncio
import logging
import os
import sys
from urllib.parse import urlsplit

import live_url_search as lus

# Anchor text scraped from arbitrary real-world pages can contain any
# Unicode character (emoji, non-Latin scripts, etc.) — the Windows console's
# default cp1252 encoding can't display all of it. Never let a print crash
# this diagnostic; just substitute what can't be shown.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Same INFO-level visibility this module logs to Application Insights in
# production — turned on here so the iterative search loop's per-iteration
# trace (query, result count, action, reasoning) prints straight to console.
logging.basicConfig(level=logging.INFO, format="    [log %(asctime)s.%(msecs)03d] %(message)s", datefmt="%H:%M:%S", stream=sys.stdout)
for _noisy in ("httpx", "httpcore", "azure.core.pipeline.policies.http_logging_policy", "primp", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


def _header(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _print_scored(scored: list[tuple[float, dict]], threshold: float, label_key: str = "url") -> None:
    for score, candidate in scored:
        tag = "SURVIVED" if score >= threshold else "  cut   "
        title = candidate.get("title") or ""
        url = candidate.get(label_key, candidate.get("url", ""))
        title_part = f"{title!r}  " if title else ""
        print(f"    [{tag}] score={score:6.2f}  {title_part}({url})")


def _build_config() -> dict:
    return {
        "endpoint": os.environ.get("AZURE_OPENAI_ENDPOINT", ""),
        "api_key": os.environ.get("AZURE_OPENAI_API_KEY", ""),
        "api_version": os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
        "select_deployment": os.environ.get("AZURE_OPENAI_CLASSIFY_DEPLOYMENT", "gpt-4o-mini"),
    }


async def run(question: str, url_override: str | None) -> None:
    config = _build_config()
    selected_urls = [url_override] if url_override else None

    if url_override:
        _header("URL OVERRIDE MODE — skipping steps 1-4 (candidate selection / SearXNG / AI selection)")
        print(f"  Testing directly against: {url_override}")
    else:
        # ------------------------------------------------------------
        # Step 1 — get_url_candidates()
        # ------------------------------------------------------------
        _header("STEP 1: get_url_candidates()")
        try:
            candidates = lus.get_url_candidates()
        except Exception as exc:  # noqa: BLE001 — this is a diagnostic script, show the real error
            print(f"  ERROR: {exc}")
            return
        print(f"  Found {len(candidates)} candidate(s):")
        for c in candidates:
            print(f"    - {c.get('title', '')!r}  ({c.get('url', '')})")
        if not candidates:
            print("\n  No candidates at all -> live_url_answer would return None here.")
            return

        # ------------------------------------------------------------
        # Step 2 — prefilter_candidates(), every score shown
        # ------------------------------------------------------------
        _header(f"STEP 2: prefilter_candidates()  (threshold PREFILTER_MIN_SCORE={lus.PREFILTER_MIN_SCORE})")
        scored = lus._score_candidates(question, candidates)
        _print_scored(scored, lus.PREFILTER_MIN_SCORE)
        prefiltered = lus.prefilter_candidates(question, candidates)
        print(f"\n  {len(prefiltered)}/{len(candidates)} candidate(s) survived (top_n=5 cap also applies).")
        if not prefiltered:
            print("\n  Nothing survived prefilter -> live_url_answer returns None here (SearXNG never runs).")
            return

        # ------------------------------------------------------------
        # Step 3 — inspect_candidates(), SearXNG snippets
        # ------------------------------------------------------------
        _header("STEP 3: inspect_candidates()  (SearXNG snippets)")
        inspected = await lus.inspect_candidates(question, prefiltered)
        for c in inspected:
            snippet = c.get("snippet") or "(no snippet / SearXNG failed)"
            print(f"    {c.get('url', '')}")
            print(f"      snippet: {snippet[:220]}")

        # ------------------------------------------------------------
        # Step 4 — select_best_urls()
        # ------------------------------------------------------------
        _header("STEP 4: select_best_urls()")
        selection = lus.select_best_urls(question, inspected, config)
        if not selection:
            print("  No URL selected (model returned an empty list, or the call failed).")
            print("  -> live_url_answer returns None here.")
            return
        selected_urls = selection["selected_urls"]
        print(f"  Selected {len(selected_urls)} URL(s): {selected_urls}")
        print(f"  Reasoning: {selection.get('reasoning', '')}")

    # ------------------------------------------------------------
    # Step 5 — iterative_domain_search() per selected URL: bounded
    # search-refine loop. Run one at a time here (production runs them
    # concurrently via asyncio.gather) so each domain's trace prints in
    # its own clearly separated section. Per-iteration detail (query,
    # result count, action, reasoning) comes from the SAME logger.info()
    # calls the real code path uses — this just prints them, rather than
    # re-deriving them separately.
    # ------------------------------------------------------------
    any_content = False
    for selected_url in selected_urls:
        domain = urlsplit(selected_url).netloc
        _header(
            f"STEP 5: iterative_domain_search()  (domain={domain}, "
            f"max_iterations={lus.MAX_SEARCH_ITERATIONS})"
        )
        result = await lus.iterative_domain_search(question, selected_url, config)

        _header(f"STEP 6: outcome for {domain}")
        if not result:
            print("  No page selected — the model gave up, a post-selection fetch failed, or")
            print(f"  max_iterations ({lus.MAX_SEARCH_ITERATIONS}) was exhausted with nothing chosen.")
            continue

        any_content = True
        pieces = result["content_pieces"]
        history = result["search_history"]
        print(f"  {len(history)} search iteration(s) run.")
        print(f"  {len(pieces)} content piece(s) found:")
        for p in pieces:
            print(f"    - {p['url']}  ({len(p['content'])} chars)")

    _header("FINAL: overall outcome")
    if not any_content:
        print("  No selected URL returned usable content.")
        print("  -> live_url_answer returns None here (falls through to the index search).")
    elif len(selected_urls) > 1:
        print(f"  Content from all successful domain(s) above would be merged into one answer,")
        print(f"  with one 'sources' entry per domain that returned content.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", help="The question to run through the live URL search flow")
    parser.add_argument(
        "--url", default=None,
        help="Bypass candidate selection/SearXNG/AI-selection and test iterative_domain_search "
             "directly against this URL's domain",
    )
    args = parser.parse_args()
    asyncio.run(run(args.question, args.url))


if __name__ == "__main__":
    main()
