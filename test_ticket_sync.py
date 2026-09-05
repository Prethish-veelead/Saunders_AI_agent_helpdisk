"""
Local tests for ticket indexing and the ticket-first-then-fallback query
path. No real Azure/Graph connection needed — everything is mocked.

Usage:
    python test_ticket_sync.py
"""

from unittest.mock import patch

import helpdesk_answer
import crawl_url_search
import sharepoint_client

FAILURES = []


def check(label: str, condition: bool, detail: str = ""):
    if condition:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label} {detail}")
        FAILURES.append(label)


# --------------------------------------------------------------------------
# (a) get_resolved_tickets() only returns Closed/Resolved items
# --------------------------------------------------------------------------

def test_get_resolved_tickets():
    print("\n--- get_resolved_tickets() ---")

    # As Graph would return after server-side filtering on Status.
    mock_response = {
        "value": [
            {
                "id": "1",
                "fields": {
                    "TicketID0": "HD-1", "Title": "vpn is not working",
                    "Description": "vpn down", "Department": "IT",
                    "SubCategory": "Network", "Status": "Closed",
                },
            },
            {
                "id": "3",
                "fields": {
                    "TicketID0": "HD-3", "Title": "vpn is not working again",
                    "Description": "vpn down again", "Department": "IT",
                    "SubCategory": "Network", "Status": "Resolved",
                },
            },
        ]
    }

    with patch.object(sharepoint_client, "SHAREPOINT_TICKETS_LIST_ID", "fake-list-id"), \
         patch.object(sharepoint_client, "_get_site_id", return_value="fake-site-id"), \
         patch.object(sharepoint_client, "_graph_get", return_value=mock_response) as mock_graph_get:

        tickets = sharepoint_client.get_resolved_tickets()

    check("returns exactly 2 tickets", len(tickets) == 2, f"got {len(tickets)}")
    check(
        "every returned ticket's status is Closed or Resolved",
        all(t["status"] in ("Closed", "Resolved") for t in tickets),
        f"statuses={[t['status'] for t in tickets]}",
    )

    filter_expr = mock_graph_get.call_args.kwargs.get("params", {}).get("$filter", "")
    check(
        "the Graph filter sent only targets Closed/Resolved",
        "eq 'Closed'" in filter_expr and "eq 'Resolved'" in filter_expr,
        f"filter={filter_expr!r}",
    )

    first = tickets[0]
    check(
        "field mapping is correct (item_id/ticket_id/subject/etc.)",
        first["item_id"] == "1" and first["ticket_id"] == "HD-1"
        and first["subject"] == "vpn is not working" and first["department"] == "IT"
        and first["sub_category"] == "Network",
        f"got {first}",
    )


# --------------------------------------------------------------------------
# (b) Ticket-first-then-fallback logic in answer_question()
# --------------------------------------------------------------------------

TICKET_CHUNK = {
    "chunk_id": "t1", "page_id": "HD-1", "source_type": "ticket", "title": "vpn is not working",
    "category": "IT", "sub_category": "Network", "content": "Restart the VPN client.",
    "@search.score": 5.0,
}
FALLBACK_CHUNK = {
    "chunk_id": "c1", "page_id": "lib-1", "source_type": "library_doc", "title": "IT Guide.docx",
    "url": "https://sharepoint.example.com/it-guide.docx", "content": "General IT troubleshooting.",
    "category": "IT", "sub_category": "General", "@search.score": 4.0,
}
NOT_FOUND_RESPONSE = {"not_found": True, "subject": "", "description": "", "answer": "", "follow_up_questions": []}


def test_ticket_answers_skips_fallback():
    print("\n--- Ticket answers it -> fallback search is skipped ---")

    with patch.object(helpdesk_answer, "search_chunks") as mock_search, \
         patch.object(helpdesk_answer, "generate_structured_response") as mock_generate:
        mock_search.return_value = [TICKET_CHUNK]
        mock_generate.return_value = {
            "not_found": False, "subject": "vpn is not working",
            "description": "The user's VPN connection is not working.",
            "answer": "Restart your VPN client. [1]",
            "follow_up_questions": ["q1", "q2", "q3"],
        }

        result = helpdesk_answer.answer_question("vpn is not working")

    check("status == answered", result["status"] == "answered", f"got {result}")
    check("search_chunks called exactly once (no fallback call)", mock_search.call_count == 1,
          f"call_count={mock_search.call_count}")
    check(
        "that one call was filtered to source_type='ticket'",
        mock_search.call_args.kwargs.get("source_type_filter") == "ticket",
        f"call_args={mock_search.call_args}",
    )
    check(
        "category/subcategory/source set from the top-ranked ticket chunk",
        result.get("category") == "IT" and result.get("subcategory") == "Network" and result.get("source") == "ticket",
        f"got {result}",
    )


def test_ticket_first_check_never_receives_conversation_history():
    print("\n--- Ticket-first check ignores conversation_history (prevents citing a stale/irrelevant ticket) ---")
    print("Real bug this guards against: a follow-up re-asking info already given in a prior turn got")
    print("answered correctly from memory, but cited a totally unrelated ticket to satisfy the schema —")
    print("because generate_structured_response() was handed conversation_history alongside")
    print("near-universally-irrelevant ticket chunks (Azure AI Search's kNN always returns *something*).")

    history = [{"question": "what are lenovo gaming laptops?", "answer": "The Legion Pro 7 has an i9 and RTX 4080."}]

    with patch.object(helpdesk_answer, "search_chunks") as mock_search, \
         patch.object(helpdesk_answer, "generate_structured_response") as mock_generate:
        mock_search.return_value = [TICKET_CHUNK]
        mock_generate.return_value = {
            "not_found": False, "subject": "s", "description": "d", "answer": "irrelevant ticket answer",
            "answer_reference_numbers": [1], "category": "IT", "sub_category": "VPN", "follow_up_questions": [],
        }

        helpdesk_answer.answer_question("what are the specs of the legion pro 7?", conversation_history=history)

    first_call_history = mock_generate.call_args_list[0].args[2] if len(mock_generate.call_args_list[0].args) > 2 \
        else mock_generate.call_args_list[0].kwargs.get("conversation_history", "MISSING")
    check("the ticket-first generate_structured_response call got conversation_history=None",
          first_call_history is None, f"got {first_call_history}")


def test_fallback_search_still_receives_conversation_history():
    print("\n--- Fallback (post ticket-first) search still gets conversation_history, for legitimate follow-ups ---")

    history = [{"question": "how do I reset my vpn?", "answer": "Restart the client."}]

    with patch.object(helpdesk_answer, "search_chunks") as mock_search, \
         patch.object(helpdesk_answer, "search_chunks_all_sources") as mock_fallback_search, \
         patch.object(helpdesk_answer, "generate_structured_response") as mock_generate:
        mock_search.return_value = []
        mock_fallback_search.return_value = [FALLBACK_CHUNK]
        mock_generate.return_value = {
            "not_found": False, "subject": "s", "description": "d", "answer": "a",
            "answer_reference_numbers": [1], "category": "IT", "sub_category": "Network", "follow_up_questions": [],
        }

        # ticket_chunks == [] takes the same "no ticket chunks" branch already
        # exercised by test_falls_back_when_no_ticket_chunks, which attempts
        # crawl_url_answer() first (fails soft with no SharePoint config in
        # this test environment, same as that test) before reaching the
        # merged fallback search this test is actually checking.
        helpdesk_answer.answer_question("does that work on mobile too?", conversation_history=history)

    call_history = mock_generate.call_args.args[2] if len(mock_generate.call_args.args) > 2 \
        else mock_generate.call_args.kwargs.get("conversation_history", "MISSING")
    check("the fallback generate_structured_response call still received the real conversation_history",
          call_history == history, f"got {call_history}")


def test_crawl_url_answer_never_receives_conversation_history():
    print("\n--- Live URL crawl call site also ignores conversation_history (same class of bug as tickets) ---")
    print("Real bug this guards against: a VPN/MFA follow-up whose correct answer had already been given")
    print("from a library doc in turn 1 got re-answered correctly from memory on turn 2, but cited an")
    print("unrelated tracked page that never mentioned VPN or MFA — crawl_url_search's candidate pool has")
    print("the same 'always returns something, often irrelevant' flaw the ticket-first check had.")

    history = [{"question": "how do I approve the MFA prompt for VPN?", "answer": "Sign in to GlobalProtect and approve it."}]

    with patch.object(helpdesk_answer, "search_chunks", return_value=[]), \
         patch.object(crawl_url_search, "crawl_url_answer") as mock_live_answer:
        async def fake_live_answer(question, config, conversation_history=None):
            return None
        mock_live_answer.side_effect = fake_live_answer

        with patch.object(helpdesk_answer, "search_chunks_all_sources", return_value=[]):
            helpdesk_answer.answer_question("is it good for gaming?", conversation_history=history)

    call = mock_live_answer.call_args
    passed_history = call.args[2] if len(call.args) > 2 else call.kwargs.get("conversation_history", "MISSING")
    check("crawl_url_answer was called with conversation_history=None",
          passed_history is None, f"got {passed_history}")


def test_ticket_first_check_requires_actionable_resolution():
    print("\n--- Ticket-first check requires require_actionable_resolution=True (incomplete tickets fall through) ---")
    print("A ticket can be about the same topic without its own resolution being usable (e.g. \"escalated\",")
    print("\"fixed on my end\") — that must not be shown as the final answer.")

    with patch.object(helpdesk_answer, "search_chunks") as mock_search, \
         patch.object(helpdesk_answer, "generate_structured_response") as mock_generate:
        mock_search.return_value = [TICKET_CHUNK]
        mock_generate.return_value = {
            "not_found": False, "subject": "s", "description": "d", "answer": "a",
            "answer_reference_numbers": [1], "category": "IT", "sub_category": "VPN", "follow_up_questions": [],
        }

        helpdesk_answer.answer_question("vpn is not working")

    call = mock_generate.call_args_list[0]
    require_flag = call.args[3] if len(call.args) > 3 else call.kwargs.get("require_actionable_resolution", "MISSING")
    check("the ticket-first generate_structured_response call got require_actionable_resolution=True",
          require_flag is True, f"got {require_flag}")


def test_fallback_search_does_not_require_actionable_resolution():
    print("\n--- Fallback (post ticket-first) search does not set require_actionable_resolution ---")
    print("That rule is ticket-specific — library docs/live URLs aren't informal closing notes like tickets are.")

    with patch.object(helpdesk_answer, "search_chunks") as mock_search, \
         patch.object(helpdesk_answer, "search_chunks_all_sources") as mock_fallback_search, \
         patch.object(helpdesk_answer, "generate_structured_response") as mock_generate:
        mock_search.return_value = []
        mock_fallback_search.return_value = [FALLBACK_CHUNK]
        mock_generate.return_value = {
            "not_found": False, "subject": "s", "description": "d", "answer": "a",
            "answer_reference_numbers": [1], "category": "IT", "sub_category": "Network", "follow_up_questions": [],
        }

        helpdesk_answer.answer_question("something with zero ticket signal")

    call = mock_generate.call_args
    require_flag = call.args[3] if len(call.args) > 3 else call.kwargs.get("require_actionable_resolution", False)
    check("the fallback generate_structured_response call left require_actionable_resolution at its default (False)",
          require_flag is False, f"got {require_flag}")


def test_pure_greetings_get_the_welcome_response_without_any_search():
    print("\n--- A bare greeting gets the welcome message directly, no search at all ---")

    greetings = ["helpdesk", "hi helpdesk", "Hi Helpdesk!", "hello", "hey there", "HI!!"]
    for greeting in greetings:
        with patch.object(helpdesk_answer, "search_chunks") as mock_search, \
             patch.object(helpdesk_answer, "search_chunks_all_sources") as mock_fallback, \
             patch.object(crawl_url_search, "crawl_url_answer") as mock_live:
            result = helpdesk_answer.answer_question(greeting)

        check(f"{greeting!r} -> answered with the welcome text",
              result.get("status") == "answered" and result.get("answer") == helpdesk_answer.GREETING_RESPONSE_TEXT,
              f"got {result}")
        check(f"{greeting!r} -> ticket search was never called", mock_search.call_count == 0)
        check(f"{greeting!r} -> live URL search was never called", mock_live.call_count == 0)
        check(f"{greeting!r} -> fallback search was never called", mock_fallback.call_count == 0)


def test_real_question_mentioning_helpdesk_is_not_treated_as_a_greeting():
    print("\n--- A real question that happens to say 'helpdesk' still goes through normal search ---")

    with patch.object(helpdesk_answer, "search_chunks") as mock_search, \
         patch.object(helpdesk_answer, "search_chunks_all_sources", return_value=[FALLBACK_CHUNK]), \
         patch.object(helpdesk_answer, "generate_structured_response") as mock_generate:
        mock_search.return_value = []
        mock_generate.return_value = {
            "not_found": False, "subject": "s", "description": "d", "answer": "a",
            "answer_reference_numbers": [1], "category": "IT", "sub_category": "General", "follow_up_questions": [],
        }
        result = helpdesk_answer.answer_question("how do I contact the helpdesk?")

    check("a real question mentioning 'helpdesk' is NOT short-circuited to the greeting",
          result.get("answer") != helpdesk_answer.GREETING_RESPONSE_TEXT, f"got {result}")
    check("ticket search was actually called for the real question", mock_search.call_count == 1)


def test_falls_back_when_no_ticket_chunks():
    print("\n--- No ticket chunks found -> falls back to per-source-type merged search ---")

    with patch.object(helpdesk_answer, "search_chunks") as mock_ticket_search, \
         patch.object(helpdesk_answer, "search_chunks_all_sources") as mock_fallback_search, \
         patch.object(helpdesk_answer, "generate_structured_response") as mock_generate:
        mock_ticket_search.return_value = []
        mock_fallback_search.return_value = [FALLBACK_CHUNK]
        mock_generate.return_value = {
            "not_found": False, "subject": "IT troubleshooting",
            "description": "General IT troubleshooting guidance.",
            "answer": "Here's the general answer. [1]",
            "follow_up_questions": ["q1", "q2", "q3"],
        }

        result = helpdesk_answer.answer_question("something unrelated to any ticket")

    check("status == answered", result["status"] == "answered", f"got {result}")
    check("ticket search called once, filtered to 'ticket'",
          mock_ticket_search.call_count == 1 and mock_ticket_search.call_args.kwargs.get("source_type_filter") == "ticket",
          f"call_count={mock_ticket_search.call_count}, call_args={mock_ticket_search.call_args}")
    check("fallback (per-source-type merged) search called exactly once", mock_fallback_search.call_count == 1,
          f"call_count={mock_fallback_search.call_count}")
    check("final source is the fallback (library_doc) chunk", result["sources"][0]["source_type"] == "library_doc")


def test_falls_back_when_ticket_answer_is_not_found():
    print("\n--- Ticket chunks exist but don't answer it -> falls back ---")

    with patch.object(helpdesk_answer, "search_chunks") as mock_ticket_search, \
         patch.object(helpdesk_answer, "search_chunks_all_sources") as mock_fallback_search, \
         patch.object(helpdesk_answer, "generate_structured_response") as mock_generate:
        mock_ticket_search.return_value = [TICKET_CHUNK]
        mock_fallback_search.return_value = [FALLBACK_CHUNK]
        mock_generate.side_effect = [
            NOT_FOUND_RESPONSE,
            {
                "not_found": False, "subject": "IT troubleshooting",
                "description": "General IT troubleshooting guidance.",
                "answer": "Here's the real answer. [1]",
                "follow_up_questions": ["q1", "q2", "q3"],
            },
        ]

        result = helpdesk_answer.answer_question("a question the ticket doesn't actually cover")

    check("status == answered", result["status"] == "answered", f"got {result}")
    check("ticket search called once", mock_ticket_search.call_count == 1)
    check("fallback search called once", mock_fallback_search.call_count == 1)
    check("generate_structured_response called twice (once for tickets, once for fallback)",
          mock_generate.call_count == 2)
    check("final answer is the fallback answer", result["answer"] == "Here's the real answer. [1]")
    check("final source is the fallback chunk", result["sources"][0]["source_type"] == "library_doc")


# --------------------------------------------------------------------------
# (b2) search_chunks_all_sources(): per-source-type search prevents one
# noisy source from crowding another out of a combined query's top_k
# --------------------------------------------------------------------------

def test_search_chunks_all_sources_merges_every_source_type_independently():
    print("\n--- search_chunks_all_sources searches each source_type separately and merges ---")

    # Reproduces the real bug found via live testing: a single combined
    # query's top_k can be entirely filled by one noisy source (many
    # near-duplicate spam tickets scoring artificially high on keyword
    # repetition) before a genuinely relevant chunk from a DIFFERENT
    # source_type (a library doc) ever gets a chance to be included.
    ticket_chunks = [{"source_type": "ticket", "title": f"spam ticket {i}", "@search.score": 0.03} for i in range(5)]
    library_chunks = [{"source_type": "library_doc", "title": "Real KB Article", "@search.score": 8.16}]

    def fake_search_chunks(question, top_k=5, source_type_filter=None):
        if source_type_filter == "ticket":
            return ticket_chunks
        if source_type_filter == "library_doc":
            return library_chunks
        raise AssertionError(f"unexpected source_type_filter: {source_type_filter}")

    with patch.object(helpdesk_answer, "search_chunks", side_effect=fake_search_chunks):
        merged = helpdesk_answer.search_chunks_all_sources("how to fix the vpn issue")

    check("library_doc chunk is present despite 5 higher-count ticket chunks",
          any(c["source_type"] == "library_doc" for c in merged), f"got {merged}")
    check("all ticket chunks are also present (nothing silently dropped)",
          sum(1 for c in merged if c["source_type"] == "ticket") == 5, f"got {merged}")
    check("exactly 6 chunks total (5 ticket + 1 library_doc)", len(merged) == 6, f"got {len(merged)}")


def test_live_url_search_attempted_even_when_ticket_chunks_found_but_not_confident():
    print("\n--- Ticket chunks found but not_found -> live URL search is still attempted ---")

    # This intentionally does NOT gate on ticket_chunks being non-empty:
    # Azure AI Search's vector kNN always returns the top-K nearest tickets
    # for any query regardless of actual relevance, so "ticket_chunks
    # non-empty" is true almost universally — gating on it would silently
    # disable live search almost entirely rather than skip it selectively.
    with patch.object(helpdesk_answer, "search_chunks") as mock_search, \
         patch.object(helpdesk_answer, "search_chunks_all_sources", return_value=[FALLBACK_CHUNK]), \
         patch.object(helpdesk_answer, "generate_structured_response") as mock_generate, \
         patch.object(crawl_url_search, "crawl_url_answer") as mock_live_answer:
        mock_search.return_value = [TICKET_CHUNK]
        mock_generate.side_effect = [
            NOT_FOUND_RESPONSE,
            {
                "not_found": False, "subject": "IT troubleshooting",
                "description": "General IT troubleshooting guidance.",
                "answer": "Here's the real answer.",
                "follow_up_questions": ["q1", "q2", "q3"],
            },
        ]

        async def fake_live_answer(question, config, conversation_history=None):
            return None

        mock_live_answer.side_effect = fake_live_answer

        helpdesk_answer.answer_question("a question the ticket doesn't actually cover")

    check(
        "live_url_answer is still attempted even though ticket_chunks was non-empty",
        mock_live_answer.call_count == 1,
        f"call_count={mock_live_answer.call_count}",
    )


def test_live_url_search_attempted_when_no_ticket_chunks():
    print("\n--- No ticket chunks at all -> live URL search IS attempted ---")

    with patch.object(helpdesk_answer, "search_chunks") as mock_search, \
         patch.object(helpdesk_answer, "search_chunks_all_sources", return_value=[FALLBACK_CHUNK]), \
         patch.object(helpdesk_answer, "generate_structured_response"), \
         patch.object(crawl_url_search, "crawl_url_answer") as mock_live_answer:
        mock_search.return_value = []

        async def fake_live_answer(question, config, conversation_history=None):
            return None

        mock_live_answer.side_effect = fake_live_answer

        helpdesk_answer.answer_question("something with zero ticket signal")

    check(
        "live_url_answer is called when ticket search found nothing at all",
        mock_live_answer.call_count == 1,
        f"call_count={mock_live_answer.call_count}",
    )


if __name__ == "__main__":
    test_get_resolved_tickets()
    test_ticket_answers_skips_fallback()
    test_ticket_first_check_never_receives_conversation_history()
    test_crawl_url_answer_never_receives_conversation_history()
    test_fallback_search_still_receives_conversation_history()
    test_ticket_first_check_requires_actionable_resolution()
    test_fallback_search_does_not_require_actionable_resolution()
    test_pure_greetings_get_the_welcome_response_without_any_search()
    test_real_question_mentioning_helpdesk_is_not_treated_as_a_greeting()
    test_falls_back_when_no_ticket_chunks()
    test_falls_back_when_ticket_answer_is_not_found()
    test_search_chunks_all_sources_merges_every_source_type_independently()
    test_live_url_search_attempted_even_when_ticket_chunks_found_but_not_confident()
    test_live_url_search_attempted_when_no_ticket_chunks()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        raise SystemExit(1)
    else:
        print("All ticket sync / query-path tests passed.")
