"""
Local tests for the restructured /api/ask response: dedup-by-source,
top-3 ranking, category/subcategory from the top-ranked source, and the
not_found short-circuit. No real Azure/OpenAI connection needed —
generate_structured_response() and search_chunks() are mocked.

Usage:
    python test_structured_response.py
"""

from unittest.mock import patch

import helpdesk_answer

FAILURES = []


def check(label: str, condition: bool, detail: str = ""):
    if condition:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label} {detail}")
        FAILURES.append(label)


NOT_FOUND_STRUCTURED = {
    "not_found": True, "subject": "", "description": "", "answer": "", "follow_up_questions": [],
}


def _answered_structured(answer="Restart your VPN client. [1]"):
    return {
        "not_found": False,
        "subject": "vpn is not working",
        "description": "The user's VPN connection is not working.",
        "answer": answer,
        "follow_up_questions": ["Is your internet stable?", "Did you update the client?", "Who is your IT contact?"],
    }


# --------------------------------------------------------------------------
# (a) 5 chunks, 3 sharing a page_id -> dedupe to fewer unique sources,
#     only the top 3 appear in the response
# --------------------------------------------------------------------------

def test_dedupe_and_top_three():
    print("\n--- Dedup by page_id + top-3 truncation ---")

    # page A: 3 chunks (same source, different chunks) — best score 8.0
    # page B: 1 chunk — score 9.0 (highest overall)
    # page C: 1 chunk — score 1.0 (lowest)
    chunks = [
        {"chunk_id": "a1", "page_id": "A", "title": "Page A", "source_type": "crawled_url",
         "category": "IT", "sub_category": "Network", "@search.score": 5.0},
        {"chunk_id": "a2", "page_id": "A", "title": "Page A", "source_type": "crawled_url",
         "category": "IT", "sub_category": "Network", "@search.score": 8.0},
        {"chunk_id": "a3", "page_id": "A", "title": "Page A", "source_type": "crawled_url",
         "category": "IT", "sub_category": "Network", "@search.score": 3.0},
        {"chunk_id": "b1", "page_id": "B", "title": "Page B", "source_type": "library_doc",
         "category": "HR", "sub_category": "Payroll", "@search.score": 9.0},
        {"chunk_id": "c1", "page_id": "C", "title": "Page C", "source_type": "ticket",
         "category": "IT", "sub_category": "Hardware", "@search.score": 1.0},
    ]

    ranked = helpdesk_answer._dedupe_and_rank(chunks)
    check("5 chunks dedupe to 3 unique sources", len(ranked) == 3, f"got {len(ranked)}")
    check(
        "the kept chunk for page A is its highest-scoring one (a2, score 8.0)",
        ranked[0]["chunk_id"] == "b1" or any(r["chunk_id"] == "a2" for r in ranked),
        f"ranked={[r['chunk_id'] for r in ranked]}",
    )
    a_entry = next(r for r in ranked if r["page_id"] == "A")
    check("page A's surviving chunk is a2 (score 8.0), not a1 or a3", a_entry["chunk_id"] == "a2",
          f"got {a_entry['chunk_id']}")
    check("ranked is sorted by score descending", [r["@search.score"] for r in ranked] == sorted(
        [r["@search.score"] for r in ranked], reverse=True))

    # Now with generate_structured_response mocked, only top 3 of MORE than
    # 3 unique sources should appear in the final "sources" list.
    chunks_4_unique = chunks + [
        {"chunk_id": "d1", "page_id": "D", "title": "Page D", "source_type": "crawled_url",
         "category": "Finance", "sub_category": "Expenses", "@search.score": 6.0},
    ]
    with patch.object(helpdesk_answer, "generate_structured_response", return_value=_answered_structured()):
        result = helpdesk_answer._generate_and_format("some question", chunks_4_unique)

    check("4 unique sources truncate to exactly 3 in the response", len(result["sources"]) == 3,
          f"got {len(result['sources'])}")
    returned_ids = {s["title"] for s in result["sources"]}
    check(
        "the 3 returned are the top-scoring ones (B=9.0, D=6.0, A=8.0) — not C (1.0)",
        "Page C" not in returned_ids,
        f"sources={result['sources']}",
    )


# --------------------------------------------------------------------------
# (b) category/subcategory in the response match the top-ranked source
# --------------------------------------------------------------------------

def test_category_matches_top_ranked_source():
    print("\n--- category/subcategory/source come from the top-ranked source ---")

    chunks = [
        {"chunk_id": "low", "page_id": "low-page", "title": "Low score page", "source_type": "library_doc",
         "category": "HR", "sub_category": "Leave", "@search.score": 2.0},
        {"chunk_id": "top", "page_id": "top-page", "title": "Top score page", "source_type": "ticket",
         "category": "IT", "sub_category": "Network", "@search.score": 9.5},
    ]

    with patch.object(helpdesk_answer, "generate_structured_response", return_value=_answered_structured()):
        result = helpdesk_answer._generate_and_format("vpn is not working", chunks)

    check("category matches the top-ranked source's category (IT, not HR)", result.get("category") == "IT",
          f"got {result.get('category')}")
    check("subcategory matches the top-ranked source's sub_category (Network, not Leave)",
          result.get("subcategory") == "Network", f"got {result.get('subcategory')}")
    check("source matches the top-ranked source's source_type (ticket)", result.get("source") == "ticket",
          f"got {result.get('source')}")
    check("sources[0] is the top-ranked page", result["sources"][0]["title"] == "Top score page",
          f"got {result['sources']}")


# --------------------------------------------------------------------------
# (c) not_found=true short-circuits cleanly, no partial shape leaks through
# --------------------------------------------------------------------------

def test_not_found_short_circuits_cleanly():
    print("\n--- not_found=true short-circuits to {'status': 'not_found'} only ---")

    chunks = [
        {"chunk_id": "x1", "page_id": "X", "title": "Some page", "source_type": "crawled_url",
         "category": "IT", "sub_category": "Network", "@search.score": 5.0},
    ]

    with patch.object(helpdesk_answer, "generate_structured_response", return_value=NOT_FOUND_STRUCTURED):
        result = helpdesk_answer._generate_and_format("an unanswerable question", chunks)

    check("status == not_found", result.get("status") == "not_found", f"got {result}")
    check("no other keys leaked into the response", set(result.keys()) == {"status"}, f"got {result}")

    # And through the full answer_question() ticket-first-then-fallback path too.
    with patch.object(helpdesk_answer, "search_chunks", return_value=chunks), \
         patch.object(helpdesk_answer, "generate_structured_response", return_value=NOT_FOUND_STRUCTURED):
        result2 = helpdesk_answer.answer_question("an unanswerable question")

    check("answer_question() also returns the clean not_found shape", result2 == {"status": "not_found"},
          f"got {result2}")


if __name__ == "__main__":
    test_dedupe_and_top_three()
    test_category_matches_top_ranked_source()
    test_not_found_short_circuits_cleanly()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        raise SystemExit(1)
    else:
        print("All structured response tests passed.")
