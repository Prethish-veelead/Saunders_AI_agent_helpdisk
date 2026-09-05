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


# --------------------------------------------------------------------------
# (d) The actual bug fix: source attribution follows the model's citation,
#     not raw retrieval score — a ticket chunk can outscore a library
#     chunk while the answer text actually came from the library chunk.
# --------------------------------------------------------------------------

def test_cited_chunk_wins_over_higher_scoring_uncited_chunk():
    print("\n--- Citation-based source wins even when a different chunk scores higher ---")

    # Reproduces the real reported bug: a ticket chunk scores higher than
    # the library chunk the answer actually came from. Reference numbering
    # is 1-based and matches chunk list order (chunk 1 = ticket, chunk 2 =
    # library) — same as _build_context_block's own numbering.
    chunks = [
        {"chunk_id": "t1", "page_id": "ticket-page", "title": "Some unrelated ticket", "url": "",
         "source_type": "ticket", "category": "IT", "sub_category": "Network", "@search.score": 9.0},
        {"chunk_id": "l1", "page_id": "lib-page", "title": "KB_Connecting_to_VPN.docx",
         "url": "https://example.sharepoint.com/KB_Connecting_to_VPN.docx",
         "source_type": "library_doc", "category": "IT", "sub_category": "VPN Access", "@search.score": 2.0},
    ]
    structured = {
        "not_found": False, "subject": "how to fix vpn", "description": "d",
        "answer": "Open GlobalProtect and connect to vpn.saundersintl.com.",
        "answer_reference_numbers": [2],  # cites the library chunk, NOT the higher-scoring ticket
        "category": "IT", "sub_category": "VPN Access",
        "follow_up_questions": [],
    }

    with patch.object(helpdesk_answer, "generate_structured_response", return_value=structured):
        result = helpdesk_answer._generate_and_format("how to fix vpn", chunks)

    check("source is library_doc, not the higher-scoring ticket",
          result.get("source") == "library_doc", f"got {result.get('source')}")
    check("sources[0] is the actual cited library doc URL",
          result["sources"][0]["url"] == "https://example.sharepoint.com/KB_Connecting_to_VPN.docx",
          f"got {result.get('sources')}")
    check("the ticket page is NOT in sources at all",
          all(s["source_type"] != "ticket" for s in result["sources"]), f"got {result.get('sources')}")


def test_invalid_reference_numbers_fall_back_to_score_ranking():
    print("\n--- Invalid/out-of-range answer_reference_numbers falls back to score ranking ---")

    chunks = [
        {"chunk_id": "a", "page_id": "a-page", "title": "Page A", "url": "", "source_type": "ticket",
         "category": "IT", "sub_category": "X", "@search.score": 5.0},
    ]
    structured = {
        "not_found": False, "subject": "s", "description": "d", "answer": "a",
        "answer_reference_numbers": [99],  # out of range — only 1 chunk exists
        "category": "IT", "sub_category": "X", "follow_up_questions": [],
    }

    with patch.object(helpdesk_answer, "generate_structured_response", return_value=structured):
        result = helpdesk_answer._generate_and_format("some question", chunks)

    check("falls back to the only available chunk via score ranking",
          result.get("source") == "ticket", f"got {result}")


def test_follow_up_without_valid_reference_is_dropped():
    print("\n--- A follow-up question with no valid reference_number is dropped ---")

    chunks = [
        {"chunk_id": "c1", "page_id": "p1", "title": "VPN Guide", "url": "https://example.com/vpn",
         "source_type": "library_doc", "category": "IT", "sub_category": "VPN", "@search.score": 5.0,
         "content": "To connect: open GlobalProtect and enter vpn.example.com as the portal address."},
    ]
    structured = {
        "not_found": False, "subject": "s", "description": "d", "answer": "Use GlobalProtect.",
        "answer_reference_numbers": [1],
        "category": "IT", "sub_category": "VPN",
        "follow_up_questions": [
            {"question": "What is the portal address?", "reference_number": 1},  # valid
            {"question": "A second question also citing reference 1", "reference_number": 1},  # valid
            {"question": "Missing reference entirely"},  # malformed — no reference_number key
            {"question": "Points past the end", "reference_number": 7},  # out of range
            "just a plain string, not a dict",  # malformed shape entirely
        ],
    }

    # _follow_up_answerable does its own live classify-model call — irrelevant
    # to what THIS test checks (structural validation), so it's stubbed to
    # always pass here; its actual behavior has its own dedicated test below.
    with patch.object(helpdesk_answer, "generate_structured_response", return_value=structured), \
         patch.object(helpdesk_answer, "_follow_up_answerable", return_value=True):
        result = helpdesk_answer._generate_and_format("how do I connect to vpn", chunks)

    follow_ups = result.get("follow_up_questions", [])
    check("only the 2 structurally-valid follow-ups survive (both cite reference 1)",
          len(follow_ups) == 2, f"got {follow_ups}")
    check("the malformed/out-of-range ones are gone",
          "Missing reference entirely" not in follow_ups
          and "Points past the end" not in follow_ups
          and "just a plain string, not a dict" not in follow_ups,
          f"got {follow_ups}")


def test_follow_up_dropped_when_cited_content_does_not_actually_answer_it():
    print("\n--- A structurally-valid follow-up is still dropped if its cited content doesn't answer it ---")

    chunks = [
        {"chunk_id": "c1", "page_id": "p1", "title": "VPN Guide", "content": "Open GlobalProtect to connect."},
        {"chunk_id": "c2", "page_id": "p2", "title": "Printer Guide", "content": "Load paper into tray 2."},
    ]
    raw_follow_ups = [
        {"question": "What is the portal address?", "reference_number": 1},  # content doesn't cover this
        {"question": "How do I load paper?", "reference_number": 2},  # content does cover this
    ]

    def fake_answerable(question_text, chunk):
        return chunk["chunk_id"] == "c2"

    with patch.object(helpdesk_answer, "_follow_up_answerable", side_effect=fake_answerable):
        result = helpdesk_answer.resolve_valid_follow_ups(chunks, raw_follow_ups)

    check("only the confirmed-answerable follow-up survives", result == ["How do I load paper?"], f"got {result}")


def test_follow_up_answerable_calls_classify_model_and_parses_yes_no():
    print("\n--- _follow_up_answerable classifies via the cheap model and parses its yes/no reply ---")

    def fake_post(url, headers=None, json=None, timeout=None):
        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                prompt = json.get("messages", [{}, {}])[-1].get("content", "")
                # Only the "Question: ..." line decides yes/no — the fake
                # content ("Load paper into tray 2.") always mentions
                # "paper" too, so scanning the whole prompt would trivially
                # say yes regardless of what's actually being asked.
                question_line = prompt.split("Question:", 1)[-1]
                reply = "yes" if "paper" in question_line.lower() else "no"
                return {"choices": [{"message": {"content": reply}}]}
        return FakeResp()

    with patch.object(helpdesk_answer, "requests") as mock_requests:
        mock_requests.post.side_effect = fake_post
        answerable = helpdesk_answer._follow_up_answerable(
            "How do I load paper?", {"content": "Load paper into tray 2."}
        )
        not_answerable = helpdesk_answer._follow_up_answerable(
            "What is the portal address?", {"content": "Load paper into tray 2."}
        )

    check("content that covers the question is confirmed answerable", answerable is True)
    check("content that doesn't cover the question is rejected", not_answerable is False)


def test_follow_up_answerable_fails_closed_on_empty_content_or_error():
    print("\n--- _follow_up_answerable fails closed: no content, or a request error, both drop the follow-up ---")

    check("empty chunk content is rejected without even calling the model",
          helpdesk_answer._follow_up_answerable("Any question?", {"content": ""}) is False)
    check("missing content key is rejected the same way",
          helpdesk_answer._follow_up_answerable("Any question?", {}) is False)

    with patch.object(helpdesk_answer, "requests") as mock_requests:
        mock_requests.post.side_effect = Exception("network error")
        result = helpdesk_answer._follow_up_answerable("Any question?", {"content": "Some real content."})
    check("a failed classify call drops the follow-up rather than raising", result is False)


if __name__ == "__main__":
    test_dedupe_and_top_three()
    test_category_matches_top_ranked_source()
    test_not_found_short_circuits_cleanly()
    test_cited_chunk_wins_over_higher_scoring_uncited_chunk()
    test_invalid_reference_numbers_fall_back_to_score_ranking()
    test_follow_up_dropped_when_cited_content_does_not_actually_answer_it()
    test_follow_up_answerable_calls_classify_model_and_parses_yes_no()
    test_follow_up_answerable_fails_closed_on_empty_content_or_error()
    test_follow_up_without_valid_reference_is_dropped()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        raise SystemExit(1)
    else:
        print("All structured response tests passed.")
