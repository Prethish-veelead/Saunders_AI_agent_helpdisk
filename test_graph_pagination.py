"""
Local tests for sharepoint_client._graph_get()'s pagination handling.
No real network needed — requests.get is mocked.

Usage:
    python test_graph_pagination.py
"""

from unittest.mock import MagicMock, patch

import sharepoint_client

FAILURES = []


def check(label: str, condition: bool, detail: str = ""):
    if condition:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label} {detail}")
        FAILURES.append(label)


def _resp(json_body, status_code=200):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body
    r.text = str(json_body)
    return r


def test_single_page_response_unchanged():
    print("\n--- (a) A response with no @odata.nextLink is returned as-is ---")

    page = {"value": [{"id": "1"}, {"id": "2"}]}
    with patch.object(sharepoint_client, "_get_graph_token", return_value="tok"), \
         patch.object(sharepoint_client, "requests") as mock_requests:
        mock_requests.get.return_value = _resp(page)
        result = sharepoint_client._graph_get("/sites/x/lists/y/items")

    check("returns exactly the 2 items from the single page",
          [v["id"] for v in result["value"]] == ["1", "2"], f"got {result}")
    check("requests.get called exactly once (no pagination follow-up)", mock_requests.get.call_count == 1)


def test_multi_page_response_is_aggregated():
    print("\n--- (b) Multiple pages (via @odata.nextLink) are followed and merged ---")

    page1 = {"value": [{"id": "1"}, {"id": "2"}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/next?skip=2"}
    page2 = {"value": [{"id": "3"}, {"id": "4"}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/next?skip=4"}
    page3 = {"value": [{"id": "5"}]}  # no nextLink -> last page

    with patch.object(sharepoint_client, "_get_graph_token", return_value="tok"), \
         patch.object(sharepoint_client, "requests") as mock_requests:
        mock_requests.get.side_effect = [_resp(page1), _resp(page2), _resp(page3)]
        result = sharepoint_client._graph_get("/sites/x/lists/y/items")

    check("all 5 items across 3 pages are present in the merged result",
          [v["id"] for v in result["value"]] == ["1", "2", "3", "4", "5"], f"got {result}")
    check("requests.get called exactly 3 times (initial + 2 nextLink follow-ups)",
          mock_requests.get.call_count == 3, f"call_count={mock_requests.get.call_count}")
    check("the follow-up calls used the exact nextLink URL, not the original path",
          mock_requests.get.call_args_list[1].args[0] == "https://graph.microsoft.com/v1.0/next?skip=2"
          and mock_requests.get.call_args_list[2].args[0] == "https://graph.microsoft.com/v1.0/next?skip=4",
          f"got {mock_requests.get.call_args_list}")
    check("no leftover @odata.nextLink in the merged result", "@odata.nextLink" not in result, f"got {result}")


def test_non_paged_single_object_response_unaffected():
    print("\n--- (c) A single-object response (no 'value' key at all) passes through untouched ---")

    site_response = {"id": "site-123", "displayName": "My Site"}
    with patch.object(sharepoint_client, "_get_graph_token", return_value="tok"), \
         patch.object(sharepoint_client, "requests") as mock_requests:
        mock_requests.get.return_value = _resp(site_response)
        result = sharepoint_client._graph_get("/sites/hostname:/path")

    check("single-object response returned unchanged", result == site_response, f"got {result}")
    check("requests.get called exactly once", mock_requests.get.call_count == 1)


def test_a_failed_page_raises_instead_of_silently_truncating():
    print("\n--- (d) A failed follow-up page raises rather than silently returning a partial list ---")

    page1 = {"value": [{"id": "1"}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/next?skip=1"}

    with patch.object(sharepoint_client, "_get_graph_token", return_value="tok"), \
         patch.object(sharepoint_client, "requests") as mock_requests:
        mock_requests.get.side_effect = [_resp(page1), _resp({"error": "boom"}, status_code=500)]
        raised = False
        try:
            sharepoint_client._graph_get("/sites/x/lists/y/items")
        except sharepoint_client.GraphAPIError:
            raised = True

    check("GraphAPIError is raised rather than returning an incomplete page-1-only result", raised)


if __name__ == "__main__":
    test_single_page_response_unchanged()
    test_multi_page_response_is_aggregated()
    test_non_paged_single_object_response_unaffected()
    test_a_failed_page_raises_instead_of_silently_truncating()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        raise SystemExit(1)
    else:
        print("All Graph pagination tests passed.")
