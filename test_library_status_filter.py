"""
Local tests for the ArticleStatus="Published" filter on
sharepoint_client.get_library_documents() — only documents with this
status should be synced/indexed, mirroring how tickets are already
restricted to Closed/Resolved. No real network needed — the Graph calls
(_graph_get / _graph_get_bytes) and drive-id resolution are all mocked.

Usage:
    python test_library_status_filter.py
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


DRIVE_ID = "drive-1"

DRIVE_CHILDREN = {
    "value": [
        {"id": "item-published", "name": "Connect-Office-Wifi.docx", "lastModifiedDateTime": "2026-01-01", "webUrl": "https://x/1"},
        {"id": "item-draft", "name": "Draft-Notes.docx", "lastModifiedDateTime": "2026-01-01", "webUrl": "https://x/2"},
        {"id": "item-no-status", "name": "Untitled.docx", "lastModifiedDateTime": "2026-01-01", "webUrl": "https://x/3"},
    ]
}


def _list_item_response(status_value):
    fields = {}
    if status_value is not None:
        fields["ArticleStatus"] = status_value
    return {"fields": fields}


def _fake_graph_get(path, params=None, **kwargs):
    if path == f"/drives/{DRIVE_ID}/root/children":
        return DRIVE_CHILDREN
    if path == f"/drives/{DRIVE_ID}/items/item-published/listItem":
        return _list_item_response("Published")
    if path == f"/drives/{DRIVE_ID}/items/item-draft/listItem":
        return _list_item_response("Draft")
    if path == f"/drives/{DRIVE_ID}/items/item-no-status/listItem":
        return _list_item_response(None)
    raise AssertionError(f"Unexpected _graph_get call: {path}")


def test_only_published_documents_are_included():
    print("\n--- Only ArticleStatus='Published' documents are synced ---")

    with patch.object(sharepoint_client, "SHAREPOINT_LIBRARY_LIST_ID", "lib-list-id"), \
         patch.object(sharepoint_client, "_get_library_drive_id", return_value=DRIVE_ID), \
         patch.object(sharepoint_client, "_graph_get", side_effect=_fake_graph_get), \
         patch.object(sharepoint_client, "_graph_get_bytes") as mock_get_bytes, \
         patch.object(sharepoint_client, "_extract_text", return_value="some real text"):
        mock_get_bytes.return_value = b"fake bytes"

        docs = sharepoint_client.get_library_documents()

    titles = [d.title for d in docs]
    check("exactly one document returned (the Published one)", len(docs) == 1, f"got {titles}")
    check("the Published document is the one included",
          titles == ["Connect-Office-Wifi.docx"], f"got {titles}")

    downloaded_item_ids = {call.args[0] for call in mock_get_bytes.call_args_list}
    check("only the Published item's content was ever downloaded",
          downloaded_item_ids == {f"/drives/{DRIVE_ID}/items/item-published/content"},
          f"got {downloaded_item_ids}")


def test_missing_status_field_fails_closed():
    print("\n--- A document with no ArticleStatus value at all is excluded (fail closed) ---")

    with patch.object(sharepoint_client, "SHAREPOINT_LIBRARY_LIST_ID", "lib-list-id"), \
         patch.object(sharepoint_client, "_get_library_drive_id", return_value=DRIVE_ID), \
         patch.object(sharepoint_client, "_graph_get", side_effect=_fake_graph_get), \
         patch.object(sharepoint_client, "_graph_get_bytes") as mock_get_bytes, \
         patch.object(sharepoint_client, "_extract_text", return_value="some real text"):
        mock_get_bytes.return_value = b"fake bytes"

        docs = sharepoint_client.get_library_documents()

    titles = [d.title for d in docs]
    check("'Untitled.docx' (no ArticleStatus field) is not included", "Untitled.docx" not in titles, f"got {titles}")


def test_category_and_subcategory_still_resolve_for_published_docs():
    print("\n--- Category/sub-category lookup still works after the refactor ---")

    def fake_graph_get_with_category(path, params=None, **kwargs):
        if path == f"/drives/{DRIVE_ID}/root/children":
            return {"value": [{"id": "item-published", "name": "Doc.docx", "lastModifiedDateTime": "", "webUrl": ""}]}
        if path == f"/drives/{DRIVE_ID}/items/item-published/listItem":
            return {"fields": {"ArticleStatus": "Published", "KBCategoryLookupId": "5", "KBSubCategoryLookupId": "9"}}
        if path.endswith("/lists//items"):
            return {"value": []}
        raise AssertionError(f"Unexpected _graph_get call: {path}")

    with patch.object(sharepoint_client, "SHAREPOINT_LIBRARY_LIST_ID", "lib-list-id"), \
         patch.object(sharepoint_client, "_get_library_drive_id", return_value=DRIVE_ID), \
         patch.object(sharepoint_client, "_graph_get", side_effect=fake_graph_get_with_category), \
         patch.object(sharepoint_client, "_graph_get_bytes", return_value=b"fake"), \
         patch.object(sharepoint_client, "_extract_text", return_value="text"), \
         patch.object(sharepoint_client, "_get_category_names", return_value={"5": "IT"}), \
         patch.object(sharepoint_client, "_get_subcategory_names", return_value={"9": "Network"}):
        docs = sharepoint_client.get_library_documents()

    check("one Published document returned", len(docs) == 1, f"got {docs}")
    check("category resolved correctly", docs[0].category == "IT", f"got {docs[0].category if docs else None}")
    check("sub_category resolved correctly", docs[0].sub_category == "Network", f"got {docs[0].sub_category if docs else None}")


if __name__ == "__main__":
    test_only_published_documents_are_included()
    test_missing_status_field_fails_closed()
    test_category_and_subcategory_still_resolve_for_published_docs()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} test(s) FAILED: {FAILURES}")
        raise SystemExit(1)
    print("All library ArticleStatus filter tests passed.")
