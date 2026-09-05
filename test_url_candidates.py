"""
Local tests for sharepoint_client.get_url_candidates() — the tracked-URL
list used by crawl_url_search.py's live per-question website search.

Covers the real bug found live: the client's actual List uses field names
("Url", "Enable") different from this code's defaults ("SeedURL", no
enabled-flag support at all), which silently produced 0 candidates.
LIST_FIELD_URL is already env-var configurable; LIST_FIELD_URL_ENABLED
support is new. No real network needed — _graph_get and _get_site_id are
mocked.

Usage:
    python test_url_candidates.py
"""

from unittest.mock import patch

import sharepoint_client

FAILURES = []


def check(label: str, condition: bool, detail: str = ""):
    if condition:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label} {detail}")
        FAILURES.append(label)


def test_rows_without_the_enable_column_are_all_kept():
    print("\n--- A List with no Enable column at all keeps every row (unchanged, older behavior) ---")

    items = {
        "value": [
            {"fields": {"Url": "https://example.com/a", "Title": "A"}},
            {"fields": {"Url": "https://example.com/b", "Title": "B"}},
        ]
    }

    with patch.object(sharepoint_client, "SHAREPOINT_URL_LIST_ID", "list-1"), \
         patch.object(sharepoint_client, "LIST_FIELD_URL", "Url"), \
         patch.object(sharepoint_client, "_get_site_id", return_value="site-1"), \
         patch.object(sharepoint_client, "_graph_get", return_value=items):
        candidates = sharepoint_client.get_url_candidates()

    check("both rows kept when there's no Enable column", len(candidates) == 2, f"got {candidates}")


def test_disabled_rows_are_excluded_when_the_enable_column_is_present():
    print("\n--- A row with Enable=False is excluded; Enable=True is kept ---")

    items = {
        "value": [
            {"fields": {"Url": "https://example.com/on", "Title": "On", "Enable": True}},
            {"fields": {"Url": "https://example.com/off", "Title": "Off", "Enable": False}},
        ]
    }

    with patch.object(sharepoint_client, "SHAREPOINT_URL_LIST_ID", "list-1"), \
         patch.object(sharepoint_client, "LIST_FIELD_URL", "Url"), \
         patch.object(sharepoint_client, "_get_site_id", return_value="site-1"), \
         patch.object(sharepoint_client, "_graph_get", return_value=items):
        candidates = sharepoint_client.get_url_candidates()

    urls = [c["url"] for c in candidates]
    check("only the enabled row survives", urls == ["https://example.com/on"], f"got {urls}")


def test_url_field_name_is_configurable_to_match_a_real_list():
    print("\n--- LIST_FIELD_URL can be pointed at whatever the real column is actually called ---")

    # Reproduces the real bug: the client's real List column is called
    # "Url", not this code's default "SeedURL" — without LIST_FIELD_URL
    # set to match, every row's URL comes back empty and gets dropped.
    items = {"value": [{"fields": {"Url": "https://example.com/real", "Title": "Real"}}]}

    with patch.object(sharepoint_client, "SHAREPOINT_URL_LIST_ID", "list-1"), \
         patch.object(sharepoint_client, "LIST_FIELD_URL", "Url"), \
         patch.object(sharepoint_client, "_get_site_id", return_value="site-1"), \
         patch.object(sharepoint_client, "_graph_get", return_value=items):
        candidates = sharepoint_client.get_url_candidates()

    check("URL resolved correctly once LIST_FIELD_URL matches the real column",
          candidates == [{"url": "https://example.com/real", "title": "Real"}], f"got {candidates}")

    # And the mismatch this bug actually looked like: field name NOT
    # overridden, so it stays on the wrong default ("SeedURL") — every row
    # silently drops.
    with patch.object(sharepoint_client, "SHAREPOINT_URL_LIST_ID", "list-1"), \
         patch.object(sharepoint_client, "_get_site_id", return_value="site-1"), \
         patch.object(sharepoint_client, "_graph_get", return_value=items):
        candidates_mismatched = sharepoint_client.get_url_candidates()

    check("without the override, a mismatched field name silently yields zero candidates",
          candidates_mismatched == [], f"got {candidates_mismatched}")


if __name__ == "__main__":
    test_rows_without_the_enable_column_are_all_kept()
    test_disabled_rows_are_excluded_when_the_enable_column_is_present()
    test_url_field_name_is_configurable_to_match_a_real_list()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} test(s) FAILED: {FAILURES}")
        raise SystemExit(1)
    print("All URL-candidate tests passed.")
