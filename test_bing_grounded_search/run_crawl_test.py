"""CLI runner for the crawl-and-match experiment (as opposed to Bing
grounding in run_test.py). Same question set, same output shape, so the two
approaches are directly comparable.

Usage:
    python run_crawl_test.py "question one" "question two" ...
    python run_crawl_test.py                      # uses DEFAULT_QUESTIONS
"""

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bing_search import DEFAULT_QUESTIONS, load_env_file
from crawl_search import CrawlAnswerer, FIXED_URLS, search_and_answer


def main():
    load_env_file()

    print("=" * 70)
    print("Seed URLs (crawl stays inside these domains only):")
    for u in FIXED_URLS:
        print(f"  - {u}")
    print("=" * 70)

    questions = sys.argv[1:] or DEFAULT_QUESTIONS

    answerer = CrawlAnswerer()
    results = []
    try:
        for i, question in enumerate(questions, 1):
            print(f"[{i}/{len(questions)}] Question: {question}")

            result = search_and_answer(question, answerer)

            print(f"          Candidates considered: {result['candidates_considered']}")
            print("          Top-ranked pages:")
            for r in result["ranked"]:
                print(f"            score={r['score']:.0f}  {r['title']}  -- {r['url']}")

            print(f"          Answer:\n{result['answer']}\n")

            if result["pages_used"]:
                print("          Pages actually used for the answer:")
                for p in result["pages_used"]:
                    print(f"            {p['title']} -- {p['url']}")
            else:
                print("          Pages actually used: (none — fetch failed)")

            print(f"          Latency: {result['latency_sec']}s")
            print(
                f"          Tokens: prompt={result['prompt_tokens']} "
                f"completion={result['completion_tokens']} "
                f"total={result['total_tokens']}"
            )
            print("-" * 70)

            results.append(result)
    finally:
        answerer.close()

    if not results:
        return

    total = len(results)
    answered = sum(1 for r in results if r["pages_used"])
    latencies = [r["latency_sec"] for r in results]
    token_counts = [r["total_tokens"] for r in results if r["total_tokens"] is not None]

    print("\n" + "=" * 70)
    print("SUMMARY")
    print(f"  Total questions:          {total}")
    print(f"  Answered from a fetched page: {answered}")
    print(f"  Avg latency:              {sum(latencies) / total:.2f}s")
    print(f"  Max latency:              {max(latencies):.2f}s")
    if token_counts:
        print(f"  Total tokens (all questions): {sum(token_counts)}")
        print(f"  Avg tokens/question:      {sum(token_counts) / len(token_counts):.0f}")
    print("=" * 70)


if __name__ == "__main__":
    main()
