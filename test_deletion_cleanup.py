"""
Local tests for Library document deletion detection/cleanup. No real Azure
connection needed: every Search call is mocked, so this only verifies the
*decision logic* (which items count as "removed" and exactly what gets
deleted), not the underlying Azure client itself.

(The seed-URL cleanup tests that used to live here were removed along with
the pre-crawl pipeline itself — its leftover index content was purged with
a one-off script, since removed.)

Usage:
    python test_deletion_cleanup.py
"""

from unittest.mock import patch

import function_app

FAILURES = []


def check(label: str, condition: bool, detail: str = ""):
    if condition:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label} {detail}")
        FAILURES.append(label)


# --------------------------------------------------------------------------
# Library document cleanup
# --------------------------------------------------------------------------

def test_library_doc_cleanup():
    print("\n--- Library document cleanup ---")

    # Currently indexed (per the search index): doc-kept and doc-removed.
    # SharePoint now only has doc-kept.
    indexed_ids = {"doc-kept", "doc-removed"}
    current_item_ids = {"doc-kept"}

    with patch.object(function_app, "get_indexed_page_ids", return_value=indexed_ids) as mock_get_indexed, \
         patch.object(function_app, "delete_chunks_for_page", return_value=5) as mock_delete_chunks:

        removed_count = function_app._cleanup_removed_library_docs(current_item_ids)

    check("returns removed_count == 1", removed_count == 1, f"got {removed_count}")
    check(
        "get_indexed_page_ids called with source_type='library_doc'",
        mock_get_indexed.call_args[0] == ("library_doc",),
        f"calls={mock_get_indexed.call_args_list}",
    )
    check(
        "delete_chunks_for_page called exactly once, for the removed item_id",
        mock_delete_chunks.call_count == 1 and mock_delete_chunks.call_args[0] == ("doc-removed",),
        f"calls={mock_delete_chunks.call_args_list}",
    )

    removed_ids_seen = [c.args[0] for c in mock_delete_chunks.call_args_list]
    check("kept item_id never passed to delete_chunks_for_page", "doc-kept" not in removed_ids_seen)


if __name__ == "__main__":
    test_library_doc_cleanup()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        raise SystemExit(1)
    else:
        print("All deletion cleanup tests passed.")
