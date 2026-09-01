"""CLI runner for the Bing-grounded-search experiment.

Usage:
    python run_test.py "question one" "question two" ...
    python run_test.py                      # uses DEFAULT_QUESTIONS

Standalone — see bing_search.py for the isolation notes.
"""

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bing_search import (
    DEFAULT_QUESTIONS,
    BingGroundedTester,
    build_scoped_query,
    get_domains,
    load_env_file,
)


def domain_check(citations, domains):
    passed, failed = [], []
    for url in citations:
        if any(domain in url for domain in domains):
            passed.append(url)
        else:
            failed.append(url)
    return passed, failed


def main():
    load_env_file()

    domains = get_domains()
    print("=" * 70)
    print("Domains under test (from fixed URLs):")
    for d in domains:
        print(f"  - {d}")
    print("=" * 70)

    questions = sys.argv[1:] or DEFAULT_QUESTIONS

    tester = None
    results = []
    try:
        tester = BingGroundedTester()
        print(f"Agent created: {tester.agent.id}\n")

        for i, question in enumerate(questions, 1):
            query = build_scoped_query(question, domains)
            print(f"[{i}/{len(questions)}] Question: {question}")
            print(f"          Scoped query: {query}")

            result = tester.ask(question, domains)
            passed, failed = domain_check(result["citations"], domains)

            print(f"          Answer:\n{result['annotated_answer']}\n")
            if result["sources"]:
                print("          Sources:")
                for s in result["sources"]:
                    status = "PASS" if s["url"] in passed else "FAIL"
                    print(f"            [{s['n']}] ({status}) {s['title']} -- {s['url']}")
            else:
                print("          Sources: (none returned)")
            print(f"          Latency: {result['latency_sec']}s")
            print(
                f"          Tokens: prompt={result['prompt_tokens']} "
                f"completion={result['completion_tokens']} "
                f"total={result['total_tokens']}"
            )
            print("-" * 70)

            results.append({
                "question": question,
                "latency_sec": result["latency_sec"],
                "citations": result["citations"],
                "in_domain": passed,
                "out_of_domain": failed,
                "total_tokens": result["total_tokens"],
            })
    finally:
        if tester is not None:
            print("Cleaning up test agent...")
            tester.cleanup()

    if not results:
        return

    total = len(results)
    fully_in_domain = sum(1 for r in results if r["citations"] and not r["out_of_domain"])
    has_out_of_domain = sum(1 for r in results if r["out_of_domain"])
    latencies = [r["latency_sec"] for r in results]
    token_counts = [r["total_tokens"] for r in results if r["total_tokens"] is not None]

    print("\n" + "=" * 70)
    print("SUMMARY")
    print(f"  Total questions:                {total}")
    print(f"  Fully in-domain citations:       {fully_in_domain}")
    print(f"  With >=1 out-of-domain citation: {has_out_of_domain}")
    print(f"  Avg latency:                    {sum(latencies) / total:.2f}s")
    print(f"  Max latency:                    {max(latencies):.2f}s")
    if token_counts:
        print(f"  Total tokens (all questions):   {sum(token_counts)}")
        print(f"  Avg tokens/question:            {sum(token_counts) / len(token_counts):.0f}")
    print("=" * 70)


if __name__ == "__main__":
    main()
