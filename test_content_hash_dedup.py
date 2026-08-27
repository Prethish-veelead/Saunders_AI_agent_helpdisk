"""
Local tests for Search-based dedup tracking (replacing Cosmos DB for
Library docs and tickets): search_index.get_indexed_content_hash(), and
the skip-if-unchanged logic in sync_library_docs_timer/sync_tickets_timer.
No real Azure connection needed — every Search/embedding call is mocked.

Usage:
    python test_content_hash_dedup.py
"""

import hashlib
from unittest.mock import MagicMock, patch

import function_app
import search_index
from sharepoint_client import LibraryDocument

FAILURES = []


def check(label: str, condition: bool, detail: str = ""):
    if condition:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label} {detail}")
        FAILURES.append(label)


# --------------------------------------------------------------------------
# get_indexed_content_hash() itself
# --------------------------------------------------------------------------

def test_get_indexed_content_hash():
    print("\n--- search_index.get_indexed_content_hash() ---")

    mock_client = MagicMock()
    mock_client.search.return_value = [{"content_hash": "abc123"}]
    with patch.object(search_index, "get_search_client", return_value=mock_client):
        result = search_index.get_indexed_content_hash("some-page-id")

    check("returns the content_hash of the first match", result == "abc123", f"got {result}")
    call_kwargs = mock_client.search.call_args.kwargs
    check("filters by the given page_id", "some-page-id" in call_kwargs.get("filter", ""))
    check("selects only content_hash", call_kwargs.get("select") == ["content_hash"])
    check("caps to top=1", call_kwargs.get("top") == 1)

    mock_client_empty = MagicMock()
    mock_client_empty.search.return_value = []
    with patch.object(search_index, "get_search_client", return_value=mock_client_empty):
        result_none = search_index.get_indexed_content_hash("never-indexed")

    check("returns None when nothing is indexed for this page_id", result_none is None, f"got {result_none}")


# --------------------------------------------------------------------------
# sync_library_docs_timer: (a) unchanged skipped, (b) changed re-indexed,
# (c) new (no existing hash) indexed
# --------------------------------------------------------------------------

def _make_doc(item_id, text, title="Doc"):
    return LibraryDocument(title=title, text=text, item_id=item_id, last_modified="", web_url="", category="", sub_category="")


def test_library_docs_skip_unchanged_reindex_changed_index_new():
    print("\n--- sync_library_docs_timer: skip unchanged / re-index changed / index new ---")

    unchanged_text = "This document has not changed."
    unchanged_hash = hashlib.sha256(unchanged_text.encode("utf-8")).hexdigest()

    changed_doc = _make_doc("doc-changed", "New content, different from before.", "Changed Doc")
    unchanged_doc = _make_doc("doc-unchanged", unchanged_text, "Unchanged Doc")
    new_doc = _make_doc("doc-new", "Brand new document, never indexed.", "New Doc")

    def fake_get_indexed_content_hash(page_id):
        if page_id == "doc-changed":
            return "some-stale-hash-that-does-not-match"
        if page_id == "doc-unchanged":
            return unchanged_hash
        if page_id == "doc-new":
            return None
        raise AssertionError(f"unexpected page_id {page_id}")

    mock_search_client = MagicMock()

    with patch.object(function_app, "get_library_documents", return_value=[changed_doc, unchanged_doc, new_doc]), \
         patch.object(function_app, "create_or_update_index"), \
         patch.object(function_app, "get_search_client", return_value=mock_search_client), \
         patch.object(function_app, "_cleanup_removed_library_docs"), \
         patch.object(function_app, "get_indexed_content_hash", side_effect=fake_get_indexed_content_hash), \
         patch.object(function_app, "chunk_text", return_value=["one chunk"]), \
         patch.object(function_app, "get_embedding", return_value=[0.1, 0.2]) as mock_get_embedding:

        function_app.sync_library_docs_timer(timer=None)

    indexed_page_ids = [
        record["page_id"]
        for call in mock_search_client.merge_or_upload_documents.call_args_list
        for record in call.args[0]
    ]
    embedded_for = [call.args[0] for call in mock_get_embedding.call_args_list]

    check("(a) unchanged doc's page_id never gets re-indexed", "doc-unchanged" not in indexed_page_ids,
          f"indexed={indexed_page_ids}")
    check("(a) unchanged doc's text never gets re-embedded", unchanged_doc.text not in embedded_for)
    check("(b) changed doc gets indexed", "doc-changed" in indexed_page_ids, f"indexed={indexed_page_ids}")
    check("(c) new doc (no existing hash) gets indexed", "doc-new" in indexed_page_ids, f"indexed={indexed_page_ids}")
    check("merge_or_upload_documents called exactly twice (changed + new, not unchanged)",
          mock_search_client.merge_or_upload_documents.call_count == 2,
          f"call_count={mock_search_client.merge_or_upload_documents.call_count}")


# --------------------------------------------------------------------------
# sync_tickets_timer: same three scenarios
# --------------------------------------------------------------------------

def _make_ticket(ticket_id, item_id):
    return {"ticket_id": ticket_id, "item_id": item_id, "subject": f"subject for {ticket_id}",
            "description": "some description", "department": "IT", "sub_category": "Network", "status": "Closed"}


def test_tickets_skip_unchanged_reindex_changed_index_new():
    print("\n--- sync_tickets_timer: skip unchanged / re-index changed / index new ---")

    changed_ticket = _make_ticket("HD-CHANGED", "item-changed")
    unchanged_ticket = _make_ticket("HD-UNCHANGED", "item-unchanged")
    new_ticket = _make_ticket("HD-NEW", "item-new")

    # Comments are what get concatenated into full_text and hashed, so
    # compute the "unchanged" ticket's real hash from what get_ticket_comments
    # + the ticket fields will actually produce.
    unchanged_full_text = "\n\n".join([
        unchanged_ticket["subject"], unchanged_ticket["description"], "existing comment",
    ])
    unchanged_hash = hashlib.sha256(unchanged_full_text.encode("utf-8")).hexdigest()

    def fake_get_ticket_comments(item_id):
        return {"item-changed": ["a different comment"], "item-unchanged": ["existing comment"],
                "item-new": ["brand new comment"]}[item_id]

    def fake_get_indexed_content_hash(ticket_id):
        if ticket_id == "HD-CHANGED":
            return "some-stale-hash-that-does-not-match"
        if ticket_id == "HD-UNCHANGED":
            return unchanged_hash
        if ticket_id == "HD-NEW":
            return None
        raise AssertionError(f"unexpected ticket_id {ticket_id}")

    mock_search_client = MagicMock()

    with patch.object(function_app, "get_resolved_tickets", return_value=[changed_ticket, unchanged_ticket, new_ticket]), \
         patch.object(function_app, "get_ticket_comments", side_effect=fake_get_ticket_comments), \
         patch.object(function_app, "create_or_update_index"), \
         patch.object(function_app, "get_search_client", return_value=mock_search_client), \
         patch.object(function_app, "_cleanup_removed_tickets"), \
         patch.object(function_app, "get_indexed_content_hash", side_effect=fake_get_indexed_content_hash), \
         patch.object(function_app, "chunk_text", return_value=["one chunk"]), \
         patch.object(function_app, "get_embedding", return_value=[0.1, 0.2]):

        function_app.sync_tickets_timer(timer=None)

    indexed_ticket_ids = [
        record["ticket_id"]
        for call in mock_search_client.merge_or_upload_documents.call_args_list
        for record in call.args[0]
    ]

    check("(a) unchanged ticket never gets re-indexed", "HD-UNCHANGED" not in indexed_ticket_ids,
          f"indexed={indexed_ticket_ids}")
    check("(b) changed ticket gets indexed", "HD-CHANGED" in indexed_ticket_ids, f"indexed={indexed_ticket_ids}")
    check("(c) new ticket (no existing hash) gets indexed", "HD-NEW" in indexed_ticket_ids,
          f"indexed={indexed_ticket_ids}")
    check("merge_or_upload_documents called exactly twice (changed + new, not unchanged)",
          mock_search_client.merge_or_upload_documents.call_count == 2,
          f"call_count={mock_search_client.merge_or_upload_documents.call_count}")


if __name__ == "__main__":
    test_get_indexed_content_hash()
    test_library_docs_skip_unchanged_reindex_changed_index_new()
    test_tickets_skip_unchanged_reindex_changed_index_new()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        raise SystemExit(1)
    else:
        print("All content-hash dedup tests passed.")
