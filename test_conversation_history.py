"""
Local tests for the persona/citation/conversation-history changes to the
/api/ask flow: no bracket-style citation markers, optional multi-turn
conversation_history threaded into the prompt, and malformed history
entries being silently skipped. No real Azure/OpenAI connection needed —
the OpenAI HTTP call (requests.post) and function_app's request parsing
are exercised directly/mocked.

Usage:
    python test_conversation_history.py
"""

import re
from unittest.mock import MagicMock, patch

import function_app
import helpdesk_answer

FAILURES = []


def check(label: str, condition: bool, detail: str = ""):
    if condition:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label} {detail}")
        FAILURES.append(label)


CHUNKS = [
    {"chunk_id": "t1", "page_id": "HD-1", "source_type": "ticket", "title": "vpn is not working",
     "url": "", "category": "IT", "sub_category": "Network", "content": "Restart the VPN client.",
     "@search.score": 5.0},
]


def _mock_openai_response(answer_text="Restart your VPN client."):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "choices": [{"message": {"content": (
            '{"not_found": false, "subject": "vpn is not working", '
            '"description": "The user\'s VPN connection is not working.", '
            f'"answer": "{answer_text}", '
            '"follow_up_questions": ["q1", "q2", "q3"]}'
        )}}]
    }
    return resp


# --------------------------------------------------------------------------
# (a) answer text contains no bracket-style citation markers like [1]
# --------------------------------------------------------------------------

def test_no_citation_markers_in_answer():
    print("\n--- (a) No [1]-style citation markers in the answer text ---")

    with patch.object(helpdesk_answer, "AZURE_OPENAI_ENDPOINT", "https://fake.openai.azure.com"), \
         patch.object(helpdesk_answer, "AZURE_OPENAI_API_KEY", "fake-key"), \
         patch("helpdesk_answer.requests.post", return_value=_mock_openai_response()) as mock_post:
        structured = helpdesk_answer.generate_structured_response("how do I fix my vpn", CHUNKS)

    check(
        "mocked answer text has no [n] bracket markers",
        re.search(r"\[\d+\]", structured["answer"]) is None,
        f"answer={structured['answer']!r}",
    )

    system_prompt = mock_post.call_args.kwargs["json"]["messages"][0]["content"]
    check(
        "system prompt no longer instructs inline citation usage (e.g. 'cite sources inline')",
        "cite" not in system_prompt.lower(),
        f"prompt={system_prompt!r}",
    )
    check(
        "system prompt explicitly forbids citation markers/footnotes/reference numbers",
        "Do not include citation markers, footnotes, or reference numbers" in system_prompt,
    )
    check(
        "system prompt establishes the Saunders Assistant persona",
        "Saunders Assistant" in system_prompt,
    )


# --------------------------------------------------------------------------
# (b) conversation_history is included in the constructed prompt
# --------------------------------------------------------------------------

def test_conversation_history_included_in_prompt():
    print("\n--- (b) conversation_history appears in the constructed prompt ---")

    history = [
        {"question": "how do I reset my password", "answer": "Use the self-service portal."},
        {"question": "does that work on mobile", "answer": "Yes, the portal is mobile-friendly."},
    ]

    with patch.object(helpdesk_answer, "AZURE_OPENAI_ENDPOINT", "https://fake.openai.azure.com"), \
         patch.object(helpdesk_answer, "AZURE_OPENAI_API_KEY", "fake-key"), \
         patch("helpdesk_answer.requests.post", return_value=_mock_openai_response()) as mock_post:
        helpdesk_answer.generate_structured_response("what about on windows", CHUNKS, history)

    system_prompt = mock_post.call_args.kwargs["json"]["messages"][0]["content"]
    check("prompt includes the 'Previous conversation:' section header", "Previous conversation:" in system_prompt)
    check("prompt includes the first turn's question", "how do I reset my password" in system_prompt)
    check("prompt includes the first turn's answer", "Use the self-service portal." in system_prompt)
    check("prompt includes the second turn's question", "does that work on mobile" in system_prompt)
    check("prompt includes the second turn's answer", "Yes, the portal is mobile-friendly." in system_prompt)
    check(
        "history section appears before the reference content",
        system_prompt.index("Previous conversation:") < system_prompt.index("Reference content:"),
    )


# --------------------------------------------------------------------------
# (c) no conversation_history (or empty list) -> unchanged prompt shape
# --------------------------------------------------------------------------

def test_no_history_matches_prior_prompt_shape():
    print("\n--- (c) No history / empty history -> same prompt as before this change ---")

    with patch.object(helpdesk_answer, "AZURE_OPENAI_ENDPOINT", "https://fake.openai.azure.com"), \
         patch.object(helpdesk_answer, "AZURE_OPENAI_API_KEY", "fake-key"), \
         patch("helpdesk_answer.requests.post", return_value=_mock_openai_response()) as mock_post:
        helpdesk_answer.generate_structured_response("how do I fix my vpn", CHUNKS)
        prompt_omitted = mock_post.call_args.kwargs["json"]["messages"][0]["content"]

        helpdesk_answer.generate_structured_response("how do I fix my vpn", CHUNKS, None)
        prompt_none = mock_post.call_args.kwargs["json"]["messages"][0]["content"]

        helpdesk_answer.generate_structured_response("how do I fix my vpn", CHUNKS, [])
        prompt_empty_list = mock_post.call_args.kwargs["json"]["messages"][0]["content"]

    check("omitted param and explicit None produce an identical prompt", prompt_omitted == prompt_none)
    check("omitted param and empty list produce an identical prompt", prompt_omitted == prompt_empty_list)
    check("no 'Previous conversation:' section leaks in when there's no history",
          "Previous conversation:" not in prompt_omitted)
    check("_build_history_block returns '' for falsy history", helpdesk_answer._build_history_block(None) == ""
          and helpdesk_answer._build_history_block([]) == "")


# --------------------------------------------------------------------------
# (d) malformed conversation_history entries are silently skipped
# --------------------------------------------------------------------------

def test_malformed_history_entries_skipped():
    print("\n--- (d) Malformed conversation_history entries are silently skipped ---")

    raw = [
        {"question": "valid question one", "answer": "valid answer one"},
        {"question": "missing answer key"},
        {"answer": "missing question key"},
        {"question": 123, "answer": "wrong type for question"},
        {"question": "wrong type for answer", "answer": 456},
        "not even a dict",
        ["also not a dict"],
        None,
        {"question": "valid question two", "answer": "valid answer two"},
    ]

    parsed = function_app._parse_conversation_history(raw)

    check("only the 2 well-formed entries survive", len(parsed) == 2, f"got {parsed}")
    check(
        "surviving entries are exactly the valid ones, in order",
        parsed == [
            {"question": "valid question one", "answer": "valid answer one"},
            {"question": "valid question two", "answer": "valid answer two"},
        ],
        f"got {parsed}",
    )

    check("non-list input (dict) returns []", function_app._parse_conversation_history({"question": "x"}) == [])
    check("non-list input (string) returns []", function_app._parse_conversation_history("nope") == [])
    check("None input returns []", function_app._parse_conversation_history(None) == [])

    # Capped to the last 5 turns.
    many_turns = [{"question": f"q{i}", "answer": f"a{i}"} for i in range(8)]
    capped = function_app._parse_conversation_history(many_turns)
    check("caps to the last 5 turns", len(capped) == 5, f"got {len(capped)} turns")
    check(
        "keeps the most recent 5 (q3..q7), drops the oldest 3",
        capped == [{"question": f"q{i}", "answer": f"a{i}"} for i in range(3, 8)],
        f"got {capped}",
    )


if __name__ == "__main__":
    test_no_citation_markers_in_answer()
    test_conversation_history_included_in_prompt()
    test_no_history_matches_prior_prompt_shape()
    test_malformed_history_entries_skipped()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        raise SystemExit(1)
    else:
        print("All conversation-history / persona / citation tests passed.")
