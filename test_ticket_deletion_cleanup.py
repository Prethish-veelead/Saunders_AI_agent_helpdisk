"""
Local tests for ticket deletion detection/cleanup. No real Azure
connection needed — every Search call is mocked, so this only verifies
the decision logic (exactly what gets deleted for a removed ticket, and
that tickets still present trigger nothing). "Currently indexed" is read
from the search index (get_indexed_page_ids) rather than a Cosmos
tracking list.

Usage:
    python test_ticket_deletion_cleanup.py
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


def test_removed_ticket_gets_cleaned_up():
    print("\n--- Removed ticket triggers exactly one delete_chunks_for_page ---")

    # Currently indexed: HD-1 (kept) and HD-99 (no longer Closed/Resolved).
    indexed_ids = {"HD-1", "HD-99"}
    current_ticket_ids = {"HD-1"}

    with patch.object(function_app, "get_indexed_page_ids", return_value=indexed_ids) as mock_get_indexed, \
         patch.object(function_app, "delete_chunks_for_page", return_value=4) as mock_delete_chunks:

        removed_count = function_app._cleanup_removed_tickets(current_ticket_ids)

    check("returns removed_count == 1", removed_count == 1, f"got {removed_count}")
    check(
        "get_indexed_page_ids called with source_type='ticket'",
        mock_get_indexed.call_args[0] == ("ticket",),
        f"calls={mock_get_indexed.call_args_list}",
    )
    check(
        "delete_chunks_for_page called exactly once, for the removed ticket_id",
        mock_delete_chunks.call_count == 1 and mock_delete_chunks.call_args[0] == ("HD-99",),
        f"calls={mock_delete_chunks.call_args_list}",
    )

    removed_ids_seen_chunks = [c.args[0] for c in mock_delete_chunks.call_args_list]
    check("kept ticket_id (HD-1) never passed to delete_chunks_for_page", "HD-1" not in removed_ids_seen_chunks)


def test_no_removals_when_all_present():
    print("\n--- No removals when every indexed ticket is still resolved/closed ---")

    indexed_ids = {"HD-1", "HD-2"}
    current_ticket_ids = {"HD-1", "HD-2"}

    with patch.object(function_app, "get_indexed_page_ids", return_value=indexed_ids), \
         patch.object(function_app, "delete_chunks_for_page") as mock_delete_chunks:

        removed_count = function_app._cleanup_removed_tickets(current_ticket_ids)

    check("returns removed_count == 0", removed_count == 0, f"got {removed_count}")
    check("delete_chunks_for_page never called", mock_delete_chunks.call_count == 0)


def test_one_failure_does_not_stop_others():
    print("\n--- One removal failing doesn't stop cleanup of the others ---")

    indexed_ids = {"HD-1", "HD-2", "HD-3"}
    current_ticket_ids = set()  # all three no longer resolved/closed

    def flaky_delete_chunks(ticket_id):
        if ticket_id == "HD-2":
            raise RuntimeError("simulated Search failure")
        return 1

    with patch.object(function_app, "get_indexed_page_ids", return_value=indexed_ids), \
         patch.object(function_app, "delete_chunks_for_page", side_effect=flaky_delete_chunks) as mock_delete_chunks:

        removed_count = function_app._cleanup_removed_tickets(current_ticket_ids)

    check("returns removed_count == 2 (HD-2's failure excluded)", removed_count == 2, f"got {removed_count}")
    check("delete_chunks_for_page attempted for all 3", mock_delete_chunks.call_count == 3)


if __name__ == "__main__":
    test_removed_ticket_gets_cleaned_up()
    test_no_removals_when_all_present()
    test_one_failure_does_not_stop_others()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        raise SystemExit(1)
    else:
        print("All ticket deletion cleanup tests passed.")
