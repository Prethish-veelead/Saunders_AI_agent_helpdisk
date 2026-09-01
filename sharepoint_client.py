"""
SharePoint access for the crawler.
-------------------------------------

Reads seed URLs from a SharePoint List (to feed the crawl orchestrator) and
document content from a SharePoint Library (indexed directly, no crawling
needed since the content is already available).

Graph auth prefers the Function App's own Managed Identity
(DefaultAzureCredential) — no stored client secret, nothing to leak or
rotate. The identity needs the Microsoft Graph "Sites.Read.All" (or
"Sites.Selected") application permission granted directly via a Graph
app-role assignment (managed identities don't appear in the Portal's
normal API-permissions UI for this) — granting that requires Entra ID
Global/Privileged Role Administrator, which not every environment's admin
has (found the hard way: our own dev tenant's account lacks it, while the
client's tenant has it). So this falls back to the older client-credentials
flow (SHAREPOINT_CLIENT_ID + SHAREPOINT_CLIENT_SECRET + AZURE_TENANT_ID)
whenever those are configured, and only uses Managed Identity when they're
absent — this keeps existing deployments (like our own dev environment)
working without requiring that permission grant, while new deployments
default to the more secure, secret-free path.
"""

import io
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests
from azure.identity import DefaultAzureCredential
from bs4 import BeautifulSoup

logger = logging.getLogger("sharepoint_client")

# Legacy client-credentials fallback — only used when all three are set
# (see _get_graph_token). Not required for new deployments, which should
# rely on Managed Identity instead.
AZURE_TENANT_ID = os.environ.get("AZURE_TENANT_ID", "")
SHAREPOINT_CLIENT_ID = os.environ.get("SHAREPOINT_CLIENT_ID", "")
SHAREPOINT_CLIENT_SECRET = os.environ.get("SHAREPOINT_CLIENT_SECRET", "")

SHAREPOINT_SITE_ID = os.environ.get("SHAREPOINT_SITE_ID", "")
SHAREPOINT_HOSTNAME = os.environ.get("SHAREPOINT_HOSTNAME", "")
SHAREPOINT_SITE_PATH = os.environ.get("SHAREPOINT_SITE_PATH", "")

SHAREPOINT_URL_LIST_ID = os.environ.get("SHAREPOINT_URL_LIST_ID", "")
LIST_FIELD_URL = os.environ.get("LIST_FIELD_URL", "SeedURL")
# Optional — only used by get_url_candidates() for live per-question URL
# search (live_url_search.py). Falls back to the URL itself as its own
# title if this column doesn't exist on the List; never fails because of it.
LIST_FIELD_TITLE = os.environ.get("LIST_FIELD_TITLE", "Title")

SHAREPOINT_LIBRARY_LIST_ID = os.environ.get("SHAREPOINT_LIBRARY_LIST_ID", "")

# KBCategory/KBSubCategory on the library are Lookup columns — they only
# store a numeric id (exposed by Graph as "<name>LookupId"), not the
# category text. These two reference lists hold the actual names.
SHAREPOINT_CATEGORIES_LIST_ID = os.environ.get("SHAREPOINT_CATEGORIES_LIST_ID", "")
SHAREPOINT_SUBCATEGORIES_LIST_ID = os.environ.get("SHAREPOINT_SUBCATEGORIES_LIST_ID", "")
LIST_FIELD_CATEGORY = os.environ.get("LIST_FIELD_CATEGORY", "KBCategory")
LIST_FIELD_SUBCATEGORY = os.environ.get("LIST_FIELD_SUBCATEGORY", "KBSubCategory")
LIST_FIELD_CATEGORY_NAME = os.environ.get("LIST_FIELD_CATEGORY_NAME", "CategoryName")
LIST_FIELD_SUBCATEGORY_NAME = os.environ.get("LIST_FIELD_SUBCATEGORY_NAME", "SubCategoryName")

# Only documents whose ArticleStatus is this value get synced/indexed —
# same idea as tickets being restricted to Closed/Resolved. Confirmed the
# same field name and value are used on both the client's and our dev
# SharePoint library.
LIST_FIELD_LIBRARY_STATUS = os.environ.get("LIST_FIELD_LIBRARY_STATUS", "ArticleStatus")
LIBRARY_PUBLISHED_STATUS_VALUE = os.environ.get("LIBRARY_PUBLISHED_STATUS_VALUE", "Published")

SHAREPOINT_TICKETS_LIST_ID = os.environ.get("SHAREPOINT_TICKETS_LIST_ID", "")
SHAREPOINT_TICKET_COMMENTS_LIST_ID = os.environ.get("SHAREPOINT_TICKET_COMMENTS_LIST_ID", "")

# SharePoint's internal field names often don't match the display name
# (e.g. the "TicketID" column's internal name is "TicketID0" because it
# collided with something at creation time) — these overrides follow the
# same pattern as LIST_FIELD_URL above, so a schema change on the
# SharePoint side never requires a code change here.
LIST_FIELD_TICKET_ID = os.environ.get("LIST_FIELD_TICKET_ID", "TicketID0")
LIST_FIELD_TICKET_SUBJECT = os.environ.get("LIST_FIELD_TICKET_SUBJECT", "Title")
LIST_FIELD_TICKET_DESCRIPTION = os.environ.get("LIST_FIELD_TICKET_DESCRIPTION", "Description")
LIST_FIELD_TICKET_DEPARTMENT = os.environ.get("LIST_FIELD_TICKET_DEPARTMENT", "Department")
LIST_FIELD_TICKET_SUBCATEGORY = os.environ.get("LIST_FIELD_TICKET_SUBCATEGORY", "SubCategory")
LIST_FIELD_TICKET_STATUS = os.environ.get("LIST_FIELD_TICKET_STATUS", "Status")
LIST_FIELD_TICKET_COMMENT_TEXT = os.environ.get("LIST_FIELD_TICKET_COMMENT_TEXT", "CommentText")
# HD_TicketComments links back to its ticket via a Lookup column — Graph
# exposes the target's SharePoint item id as "<InternalName>LookupId",
# not the ticket's human-readable TicketID. get_ticket_comments() must be
# called with that item id, not with the TicketID string.
LIST_FIELD_TICKET_COMMENT_LOOKUP = os.environ.get("LIST_FIELD_TICKET_COMMENT_LOOKUP", "TicketLookupLookupId")

# Both Status and the comment lookup are non-indexed SharePoint columns —
# Graph refuses to filter on them without this header. Fine at current
# list sizes; would need the columns indexed in SharePoint if these lists
# grow much larger.
_HONOR_NON_INDEXED_HEADER = {"Prefer": "HonorNonIndexedQueriesWarningMayFailRandomly"}

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "20"))
SUPPORTED_LIBRARY_EXTENSIONS = (".pdf", ".docx", ".html", ".md", ".pptx")


class SharePointConfigError(Exception):
    """Raised when required SharePoint/Graph configuration is missing."""


class GraphAPIError(Exception):
    """Raised when a Microsoft Graph API call fails unexpectedly."""


# --------------------------------------------------------------------------
# Auth — Managed Identity via DefaultAzureCredential by default (no stored
# secret; that credential object caches and refreshes its own token
# internally, so no manual expiry bookkeeping is needed for it). Falls
# back to a cached client-credentials token when a legacy secret is
# configured — see the module docstring for why both paths exist.
# --------------------------------------------------------------------------

_credential = DefaultAzureCredential()


@dataclass
class _TokenCache:
    access_token: Optional[str] = None
    expires_at: float = 0.0


_legacy_token_cache = _TokenCache()


def _get_graph_token_legacy() -> str:
    """Client-credentials flow, only reached when SHAREPOINT_CLIENT_ID/
    SECRET/AZURE_TENANT_ID are all configured — see _get_graph_token().
    """
    now = time.time()
    if _legacy_token_cache.access_token and now < (_legacy_token_cache.expires_at - 60):
        return _legacy_token_cache.access_token

    url = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": SHAREPOINT_CLIENT_ID,
        "client_secret": SHAREPOINT_CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
    }
    resp = requests.post(url, data=data, timeout=REQUEST_TIMEOUT_SECONDS)
    if resp.status_code != 200:
        raise GraphAPIError(f"Failed to acquire Graph token (client credentials): {resp.status_code} {resp.text[:200]}")

    body = resp.json()
    _legacy_token_cache.access_token = body["access_token"]
    _legacy_token_cache.expires_at = now + int(body.get("expires_in", 3600))
    return _legacy_token_cache.access_token


def _get_graph_token() -> str:
    if AZURE_TENANT_ID and SHAREPOINT_CLIENT_ID and SHAREPOINT_CLIENT_SECRET:
        return _get_graph_token_legacy()
    try:
        return _credential.get_token("https://graph.microsoft.com/.default").token
    except Exception as exc:  # noqa: BLE001 — surfaced as our own error type, same contract as before
        raise GraphAPIError(f"Failed to acquire Graph token via Managed Identity: {exc}") from exc


def _graph_get(
    path: str,
    params: Optional[dict[str, str]] = None,
    extra_headers: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Follows Graph API pagination (@odata.nextLink) and returns every
    page's "value" entries merged into one list.

    Critical: Graph paginates /items collections (List items, Library
    items) once the list is large enough — silently, with no error, just
    a smaller-than-expected "value" array plus an "@odata.nextLink" to the
    next page. A caller that reads only the first page sees an incomplete
    list. That's dangerous here specifically because get_library_documents()
    and get_resolved_tickets() feed cleanup logic (_cleanup_removed_library_docs,
    _cleanup_removed_tickets) that deletes any indexed item NOT present in
    the current fetched set — a truncated first page makes real, still-
    existing items look "removed from SharePoint" and wipes them from the
    search index. Found via live testing: a sync run reported only 8
    Library documents (page 1) and deleted 56 real ones as a result.
    """
    token = _get_graph_token()
    headers = {"Authorization": f"Bearer {token}"}
    if extra_headers:
        headers.update(extra_headers)
    resp = requests.get(
        f"{GRAPH_BASE_URL}{path}", headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS
    )
    if resp.status_code != 200:
        raise GraphAPIError(f"Graph GET {path} failed: {resp.status_code} {resp.text[:200]}")
    data = resp.json()

    if "value" not in data:
        # A single-object response (e.g. the site lookup in _get_site_id)
        # rather than a paged collection — nothing to aggregate.
        return data

    all_values = list(data["value"])
    next_link = data.get("@odata.nextLink")
    while next_link:
        # next_link is already a complete absolute URL with its own query
        # string (including the paging cursor) — passed as-is, no params.
        resp = requests.get(next_link, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            raise GraphAPIError(f"Graph GET (paged) {path} failed: {resp.status_code} {resp.text[:200]}")
        page = resp.json()
        all_values.extend(page.get("value", []))
        next_link = page.get("@odata.nextLink")

    data["value"] = all_values
    data.pop("@odata.nextLink", None)  # every page has been merged in; a stale link here would mislead a caller
    return data


def _graph_get_bytes(path: str) -> bytes:
    token = _get_graph_token()
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(f"{GRAPH_BASE_URL}{path}", headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    if resp.status_code != 200:
        raise GraphAPIError(f"Graph GET {path} failed: {resp.status_code} {resp.text[:200]}")
    return resp.content


_resolved_site_id: Optional[str] = None


def _get_site_id() -> str:
    global _resolved_site_id
    if SHAREPOINT_SITE_ID:
        return SHAREPOINT_SITE_ID
    if _resolved_site_id:
        return _resolved_site_id

    site_data = _graph_get(f"/sites/{SHAREPOINT_HOSTNAME}:{SHAREPOINT_SITE_PATH}")
    _resolved_site_id = site_data["id"]
    return _resolved_site_id


# --------------------------------------------------------------------------
# Tracked URLs — from the SharePoint List, read for live_url_search.py's
# per-question search (the old scheduled pre-crawl pipeline that used to
# also read this List has been decommissioned).
# --------------------------------------------------------------------------

def get_url_candidates() -> list[dict]:
    """Reads the URL (and title, if available) columns from the configured
    SharePoint List, for live_url_search.py to fuzzy-match a question
    against.

    Falls back to using the URL itself as the title when the List has no
    title-like column (LIST_FIELD_TITLE) or the value is empty for a given
    row — a missing/renamed title column must never break this.
    """
    if not SHAREPOINT_URL_LIST_ID:
        raise SharePointConfigError("SharePoint List configuration is incomplete.")

    site_id = _get_site_id()
    data = _graph_get(
        f"/sites/{site_id}/lists/{SHAREPOINT_URL_LIST_ID}/items",
        params={"$expand": "fields"},
    )
    candidates = []
    for entry in data.get("value", []):
        fields = entry.get("fields", {})
        url = (fields.get(LIST_FIELD_URL) or "").strip()
        if not url:
            continue
        title = (fields.get(LIST_FIELD_TITLE) or "").strip() or url
        candidates.append({"url": url, "title": title})
    return candidates


# --------------------------------------------------------------------------
# Library documents — extracted directly, no crawling needed
# --------------------------------------------------------------------------

@dataclass
class LibraryDocument:
    title: str
    text: str
    item_id: str
    last_modified: str = ""
    web_url: str = ""
    category: str = ""
    sub_category: str = ""


def _extract_text(filename: str, content: bytes) -> str:
    lower = filename.lower()
    try:
        if lower.endswith(".pdf"):
            import pdfplumber
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                return "\n".join(page.extract_text() or "" for page in pdf.pages)
        if lower.endswith(".docx"):
            import docx
            doc = docx.Document(io.BytesIO(content))
            return "\n".join(p.text for p in doc.paragraphs)
        if lower.endswith(".html"):
            import trafilatura
            html_text = content.decode("utf-8", errors="replace")
            extracted = trafilatura.extract(html_text, include_comments=False, include_tables=False)
            return extracted or ""
        if lower.endswith(".md"):
            import markdown
            from bs4 import BeautifulSoup
            md_text = content.decode("utf-8", errors="replace")
            rendered_html = markdown.markdown(md_text)
            return BeautifulSoup(rendered_html, "html.parser").get_text(separator="\n")
        if lower.endswith(".pptx"):
            from pptx import Presentation
            prs = Presentation(io.BytesIO(content))
            lines = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        for para in shape.text_frame.paragraphs:
                            for run in para.runs:
                                if run.text:
                                    lines.append(run.text)
            return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to extract text from '%s': %s", filename, exc)
        return ""
    return ""


_resolved_library_drive_id: Optional[str] = None


def _get_library_drive_id() -> str:
    """The target document library (e.g. HD_KnowledgeDocuments) is not
    necessarily the site's default drive — a site can have several document
    libraries. Resolving via the library's list id (like SHAREPOINT_URL_LIST_ID
    for the seed-URL list) targets the right one instead of silently reading
    whatever library happens to be the site default.
    """
    global _resolved_library_drive_id
    if _resolved_library_drive_id:
        return _resolved_library_drive_id

    site_id = _get_site_id()
    drive = _graph_get(f"/sites/{site_id}/lists/{SHAREPOINT_LIBRARY_LIST_ID}/drive")
    _resolved_library_drive_id = drive["id"]
    return _resolved_library_drive_id


_category_names_cache: Optional[dict[str, str]] = None
_subcategory_names_cache: Optional[dict[str, str]] = None


def _get_category_names() -> dict[str, str]:
    """SharePoint list item id -> CategoryName, for HD_Categories. Small
    reference list — fetched once and cached for the process lifetime.
    """
    global _category_names_cache
    if _category_names_cache is not None:
        return _category_names_cache
    if not SHAREPOINT_CATEGORIES_LIST_ID:
        _category_names_cache = {}
        return _category_names_cache

    site_id = _get_site_id()
    data = _graph_get(f"/sites/{site_id}/lists/{SHAREPOINT_CATEGORIES_LIST_ID}/items", params={"$expand": "fields"})
    _category_names_cache = {
        entry.get("id"): entry.get("fields", {}).get(LIST_FIELD_CATEGORY_NAME, "")
        for entry in data.get("value", [])
    }
    return _category_names_cache


def _get_subcategory_names() -> dict[str, str]:
    """SharePoint list item id -> SubCategoryName, for HD_SubCategories."""
    global _subcategory_names_cache
    if _subcategory_names_cache is not None:
        return _subcategory_names_cache
    if not SHAREPOINT_SUBCATEGORIES_LIST_ID:
        _subcategory_names_cache = {}
        return _subcategory_names_cache

    site_id = _get_site_id()
    data = _graph_get(
        f"/sites/{site_id}/lists/{SHAREPOINT_SUBCATEGORIES_LIST_ID}/items", params={"$expand": "fields"}
    )
    _subcategory_names_cache = {
        entry.get("id"): entry.get("fields", {}).get(LIST_FIELD_SUBCATEGORY_NAME, "")
        for entry in data.get("value", [])
    }
    return _subcategory_names_cache


def _get_library_item_metadata(drive_id: str, item_id: str, filename: str) -> tuple[str, str, str]:
    """KBCategory/KBSubCategory/ArticleStatus on the library are custom
    columns, only available through the item's associated list entry — not
    the plain drive-item metadata /drives/.../items/{id} already returns.
    KBCategory/KBSubCategory are Lookup columns specifically: Graph exposes
    them as "<name>LookupId" (a reference, not the text itself), resolved
    here against the HD_Categories/HD_SubCategories reference lists.
    ArticleStatus is a plain text/choice column, returned as-is. Uses
    /drives/{id}/items/... rather than /sites/{id}/drive/... (the site's
    *default* drive) since this library isn't the site default drive and
    the latter 404s.

    Returns (category, sub_category, status) — status is "" if it couldn't
    be determined (missing field, or the fetch itself failed), which
    get_library_documents() treats as "not Published" so a broken lookup
    fails closed rather than silently indexing something it shouldn't.
    """
    try:
        list_item = _graph_get(f"/drives/{drive_id}/items/{item_id}/listItem", params={"$expand": "fields"})
    except GraphAPIError as exc:
        logger.warning("Could not fetch metadata for '%s': %s", filename, exc)
        return "", "", ""

    fields = list_item.get("fields", {})
    category_lookup_id = str(fields.get(f"{LIST_FIELD_CATEGORY}LookupId") or "")
    subcategory_lookup_id = str(fields.get(f"{LIST_FIELD_SUBCATEGORY}LookupId") or "")
    category = _get_category_names().get(category_lookup_id, "") if category_lookup_id else ""
    sub_category = _get_subcategory_names().get(subcategory_lookup_id, "") if subcategory_lookup_id else ""
    status = fields.get(LIST_FIELD_LIBRARY_STATUS, "")
    return category, sub_category, status


def get_library_documents() -> list[LibraryDocument]:
    """Only documents whose ArticleStatus is LIBRARY_PUBLISHED_STATUS_VALUE
    ("Published" by default) are synced — same idea as tickets being
    restricted to Closed/Resolved. The status check happens before the
    file is downloaded, so a Draft document costs nothing beyond the one
    metadata lookup.
    """
    if not SHAREPOINT_LIBRARY_LIST_ID:
        raise SharePointConfigError("SharePoint Library configuration is incomplete.")

    drive_id = _get_library_drive_id()
    data = _graph_get(f"/drives/{drive_id}/root/children")

    documents = []
    for entry in data.get("value", []):
        name = entry.get("name", "")
        if not name.lower().endswith(SUPPORTED_LIBRARY_EXTENSIONS):
            continue
        item_id = entry.get("id")
        category, sub_category, status = _get_library_item_metadata(drive_id, item_id, name)
        if status != LIBRARY_PUBLISHED_STATUS_VALUE:
            logger.info("Skipping '%s' — ArticleStatus is '%s', not '%s'.", name, status, LIBRARY_PUBLISHED_STATUS_VALUE)
            continue
        try:
            content = _graph_get_bytes(f"/drives/{drive_id}/items/{item_id}/content")
            text = _extract_text(name, content)
        except GraphAPIError as exc:
            logger.warning("Skipping '%s' — download failed: %s", name, exc)
            continue
        if text.strip():
            documents.append(LibraryDocument(
                title=name, text=text, item_id=item_id,
                last_modified=entry.get("lastModifiedDateTime", ""),
                web_url=entry.get("webUrl", ""),
                category=category,
                sub_category=sub_category,
            ))
    return documents


# --------------------------------------------------------------------------
# Resolved/closed tickets — a real support conversation that reached a
# resolution, indexed as knowledge alongside crawled URLs and Library docs.
# --------------------------------------------------------------------------

def get_resolved_tickets() -> list[dict]:
    """Tickets whose Status is Closed or Resolved — only resolutions
    considered final are trustworthy enough to index as knowledge.

    Returns each ticket as a dict with: item_id (the ticket's SharePoint
    list item id — required by get_ticket_comments(), NOT the same as
    ticket_id), ticket_id, subject, description, department, sub_category,
    status.
    """
    if not SHAREPOINT_TICKETS_LIST_ID:
        raise SharePointConfigError("SharePoint Tickets List configuration is incomplete.")

    site_id = _get_site_id()

    # Diagnostic only: distinguishes "the list is empty" from "items exist
    # but none match the Closed/Resolved filter" (e.g. a wrong
    # LIST_FIELD_TICKET_STATUS name or unexpected status values) — the
    # filtered query below can't tell those apart on its own.
    raw_total = _graph_get(
        f"/sites/{site_id}/lists/{SHAREPOINT_TICKETS_LIST_ID}/items",
        params={"$select": "id"},
    )
    logger.info(
        "Tickets list has %d item(s) total, before filtering to Closed/Resolved.",
        len(raw_total.get("value", [])),
    )

    filter_expr = (
        f"fields/{LIST_FIELD_TICKET_STATUS} eq 'Closed' or "
        f"fields/{LIST_FIELD_TICKET_STATUS} eq 'Resolved'"
    )
    data = _graph_get(
        f"/sites/{site_id}/lists/{SHAREPOINT_TICKETS_LIST_ID}/items",
        params={"$expand": "fields", "$filter": filter_expr},
        extra_headers=_HONOR_NON_INDEXED_HEADER,
    )

    tickets = []
    for entry in data.get("value", []):
        fields = entry.get("fields", {})
        # Description is a rich-text column — SharePoint returns it as
        # HTML (same as CommentText on comments), not plain text.
        raw_description = fields.get(LIST_FIELD_TICKET_DESCRIPTION) or ""
        description = BeautifulSoup(raw_description, "html.parser").get_text(separator=" ").strip()
        tickets.append({
            "item_id": entry.get("id"),
            "ticket_id": fields.get(LIST_FIELD_TICKET_ID) or entry.get("id"),
            "subject": fields.get(LIST_FIELD_TICKET_SUBJECT) or "",
            "description": description,
            "department": fields.get(LIST_FIELD_TICKET_DEPARTMENT) or "",
            "sub_category": fields.get(LIST_FIELD_TICKET_SUBCATEGORY) or "",
            "status": fields.get(LIST_FIELD_TICKET_STATUS) or "",
        })
    return tickets


def get_ticket_comments(item_id: str) -> list[str]:
    """All comment text for one ticket, as plain strings (CommentText is
    stored as HTML, stripped here). `item_id` must be the ticket's
    SharePoint list item id (get_resolved_tickets()'s "item_id" field) —
    HD_TicketComments' lookup column stores that, not the human-readable
    TicketID.
    """
    if not SHAREPOINT_TICKET_COMMENTS_LIST_ID:
        raise SharePointConfigError("SharePoint Ticket Comments List configuration is incomplete.")

    site_id = _get_site_id()
    escaped_item_id = str(item_id).replace("'", "''")
    filter_expr = f"fields/{LIST_FIELD_TICKET_COMMENT_LOOKUP} eq '{escaped_item_id}'"
    data = _graph_get(
        f"/sites/{site_id}/lists/{SHAREPOINT_TICKET_COMMENTS_LIST_ID}/items",
        params={"$expand": "fields", "$filter": filter_expr},
        extra_headers=_HONOR_NON_INDEXED_HEADER,
    )

    comments = []
    for entry in data.get("value", []):
        raw = entry.get("fields", {}).get(LIST_FIELD_TICKET_COMMENT_TEXT) or ""
        text = BeautifulSoup(raw, "html.parser").get_text(separator=" ").strip()
        if text:
            comments.append(text)
    return comments
