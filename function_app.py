"""
Helpdesk sync + question-answering Function App.
----------------------------------------------------

Two SharePoint sources are synced into Azure AI Search on a schedule
(Library documents, resolved/closed tickets), and one HTTP endpoint
(`ask`) answers questions against that index plus a live, per-question
web search (see live_url_search.py).

The pre-crawl pipeline that used to also feed this index from arbitrary
seed URLs (a Durable Functions orchestrator + Cosmos DB tracking +
Container Apps Job fetching) has been fully decommissioned — live_url_search.py
now covers that need per-question instead of via a scheduled background
crawl. Its leftover "crawled_url" content was purged from the index with a
one-off script (since removed); the index has carried only "ticket" and
"library_doc" source types since.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

import azure.functions as func
import azure.durable_functions as df

from sharepoint_client import get_library_documents, get_resolved_tickets, get_ticket_comments
from embedding_client import get_embedding
from chunking import chunk_text
from search_index import (
    get_search_client, create_or_update_index, delete_chunks_for_page,
    get_indexed_content_hash, get_indexed_page_ids,
)
from helpdesk_answer import answer_question
import hashlib

logger = logging.getLogger("helpdesk_sync")

myApp = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


LIBRARY_CONTENT_TYPE_BY_EXTENSION = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".html": "html",
    ".md": "markdown",
    ".pptx": "pptx",
}


def _library_content_type(title: str) -> str:
    lower = title.lower()
    for ext, content_type in LIBRARY_CONTENT_TYPE_BY_EXTENSION.items():
        if lower.endswith(ext):
            return content_type
    return "unknown"


def _cleanup_removed_library_docs(current_item_ids: set[str]) -> int:
    """Deletes indexed Library documents (search chunks) whose item_id is
    no longer present in the current SharePoint Library listing. "Currently
    indexed" is read straight from the search index (source_type=
    "library_doc") rather than a separate Cosmos tracking list. Returns how
    many were removed.
    """
    indexed_item_ids = get_indexed_page_ids("library_doc")
    removed_ids = indexed_item_ids - current_item_ids
    removed_count = 0
    for item_id in removed_ids:
        try:
            deleted_chunks = delete_chunks_for_page(item_id)
            removed_count += 1
            logger.info(
                "Removed Library document no longer in SharePoint: item_id=%s — deleted %d chunks",
                item_id, deleted_chunks,
            )
        except Exception:  # noqa: BLE001 — one bad cleanup shouldn't stop the rest
            logger.exception("Failed to clean up removed Library document: item_id=%s", item_id)
    if removed_count:
        logger.info("Library doc cleanup: removed %d document(s) no longer in SharePoint.", removed_count)
    return removed_count


@myApp.timer_trigger(schedule="%LIBRARY_SYNC_CRON%", arg_name="timer", run_on_startup=False)
def sync_library_docs_timer(timer: func.TimerRequest) -> None:
    """Daily at 3:30 AM UTC: reads SharePoint Library documents and indexes
    them directly — no crawling needed, the content is already available,
    it just needs chunking, embedding, and pushing to Azure AI Search with
    source_type="library_doc".
    """
    try:
        documents = get_library_documents()
    except Exception as exc:  # noqa: BLE001 — a transient network/Graph failure must not crash this run;
        # log and wait for the next scheduled sync rather than an unhandled exception. Not narrowed
        # to (SharePointConfigError, GraphAPIError) since _graph_get()/_get_graph_token() can also
        # raise raw requests exceptions (timeout, connection error, DNS failure) on a network blip.
        logger.error("Failed to read Library documents from SharePoint: %s", exc)
        return

    logger.info("Found %d Library document(s) to index.", len(documents))
    create_or_update_index()
    search_client = get_search_client()

    current_item_ids = {doc.item_id for doc in documents}
    _cleanup_removed_library_docs(current_item_ids)

    for doc in documents:
        try:
            content_hash = hashlib.sha256(doc.text.encode("utf-8")).hexdigest()
            if get_indexed_content_hash(doc.item_id) == content_hash:
                logger.info("Library doc '%s' unchanged, skipping.", doc.title)
                continue

            chunks = chunk_text(doc.text)
            if not chunks:
                continue

            logger.info(
                "Library doc '%s': %d chars -> %d chunk(s) (sizes: %s)",
                doc.title, len(doc.text), len(chunks), [len(c) for c in chunks],
            )

            records = []
            for i, chunk in enumerate(chunks):
                embedding = get_embedding(chunk)
                records.append({
                    "chunk_id": hashlib.sha256(f"{doc.item_id}|{i}".encode("utf-8")).hexdigest(),
                    "page_id": doc.item_id,
                    "content": chunk,
                    "title": doc.title,
                    "url": doc.web_url,
                    "domain": "sharepoint-library",
                    "parent_url": None,
                    "crawl_depth": 0,
                    "content_type": _library_content_type(doc.title),
                    "source_type": "library_doc",
                    "category": doc.category,
                    "sub_category": doc.sub_category,
                    "crawled_at": doc.last_modified or _now_iso(),
                    "content_hash": content_hash,
                    "vector": embedding,
                })

            search_client.merge_or_upload_documents(records)
            logger.info("Indexed %d chunk(s) for library document '%s'", len(records), doc.title)
        except Exception:  # noqa: BLE001 — one bad document shouldn't stop the rest
            logger.exception("Failed to index library document '%s'", doc.title)


def _cleanup_removed_tickets(current_ticket_ids: set[str]) -> int:
    """Deletes indexed tickets (search chunks) whose ticket_id is no longer
    in the current resolved/closed set — the ticket was reopened, deleted,
    or its status changed away from Closed/Resolved. "Currently indexed" is
    read straight from the search index (source_type="ticket") rather than
    a separate Cosmos tracking list. Returns how many were removed.
    """
    indexed_ticket_ids = get_indexed_page_ids("ticket")
    removed_ids = indexed_ticket_ids - current_ticket_ids
    removed_count = 0
    for ticket_id in removed_ids:
        try:
            deleted_chunks = delete_chunks_for_page(ticket_id)
            removed_count += 1
            logger.info(
                "Removed ticket no longer Closed/Resolved: %s — deleted %d chunks",
                ticket_id, deleted_chunks,
            )
        except Exception:  # noqa: BLE001 — one bad cleanup shouldn't stop the rest
            logger.exception("Failed to clean up removed ticket: %s", ticket_id)
    if removed_count:
        logger.info("Ticket cleanup: removed %d ticket(s) no longer Closed/Resolved.", removed_count)
    return removed_count


@myApp.timer_trigger(schedule="%TICKET_SYNC_CRON%", arg_name="timer", run_on_startup=False)
def sync_tickets_timer(timer: func.TimerRequest) -> None:
    """Every 15 minutes: indexes resolved/closed helpdesk tickets — a real
    support conversation (subject + description + every comment) that
    reached a resolution is often a better answer to "how do I fix this"
    than generic documentation, so it's indexed alongside crawled URLs and
    Library docs with source_type="ticket".
    """
    try:
        tickets = get_resolved_tickets()
    except Exception as exc:  # noqa: BLE001 — same reasoning as sync_library_docs_timer above:
        # a transient network/Graph failure must not crash this run, and isn't necessarily a
        # SharePointConfigError/GraphAPIError (raw requests exceptions can also surface here).
        logger.error("Failed to read tickets from SharePoint: %s", exc)
        return

    logger.info("Found %d resolved/closed ticket(s) to index.", len(tickets))
    create_or_update_index()
    search_client = get_search_client()

    current_ticket_ids = {ticket["ticket_id"] for ticket in tickets}
    _cleanup_removed_tickets(current_ticket_ids)

    tickets_processed = 0
    total_chunks = 0
    for ticket in tickets:
        ticket_id = ticket["ticket_id"]
        try:
            comments = get_ticket_comments(ticket["item_id"])
            text_parts = [ticket.get("subject", ""), ticket.get("description", ""), *comments]
            full_text = "\n\n".join(part for part in text_parts if part)

            content_hash = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
            if get_indexed_content_hash(ticket_id) == content_hash:
                logger.info("Ticket %s unchanged, skipping.", ticket_id)
                continue

            chunks = chunk_text(full_text)
            if not chunks:
                continue

            records = []
            for i, chunk in enumerate(chunks):
                embedding = get_embedding(chunk)
                records.append({
                    # page_id = ticket_id (not item_id) so delete_chunks_for_page()
                    # from the URL/Library cleanup work can be reused as-is.
                    "chunk_id": hashlib.sha256(f"{ticket_id}|{i}".encode("utf-8")).hexdigest(),
                    "page_id": ticket_id,
                    "content": chunk,
                    "title": ticket.get("subject", ""),
                    "url": "",
                    "domain": "sharepoint-tickets",
                    "parent_url": None,
                    "crawl_depth": 0,
                    "content_type": "ticket",
                    "source_type": "ticket",
                    "ticket_id": ticket_id,
                    "category": ticket.get("department", ""),
                    "sub_category": ticket.get("sub_category", ""),
                    "status": ticket.get("status", ""),
                    "crawled_at": _now_iso(),
                    "content_hash": content_hash,
                    "vector": embedding,
                })

            search_client.merge_or_upload_documents(records)
            tickets_processed += 1
            total_chunks += len(records)
            logger.info("Indexed %d chunk(s) for ticket %s", len(records), ticket_id)
        except Exception:  # noqa: BLE001 — one bad ticket shouldn't stop the rest
            logger.exception("Failed to index ticket %s", ticket_id)

    logger.info(
        "Ticket sync: processed %d ticket(s), indexed %d chunk(s) total.",
        tickets_processed, total_chunks,
    )


# --------------------------------------------------------------------------
# Query/answer endpoint — for testing the pipeline by asking real questions.
# --------------------------------------------------------------------------

MAX_CONVERSATION_HISTORY_TURNS = 5


def _parse_conversation_history(raw: Any) -> list[dict]:
    """Validates and normalizes the optional "conversation_history" field
    from the request body: a list of {"question": str, "answer": str}
    turns, most recent last. Malformed entries (not a dict, missing keys,
    wrong types) are silently skipped rather than failing the whole
    request. Truncated to the most recent MAX_CONVERSATION_HISTORY_TURNS
    turns to keep prompt size bounded.

    This endpoint is stateless — nothing here is stored server-side. The
    frontend is responsible for tracking conversation history itself and
    resending it (via this field) on every request if it wants multi-turn
    context; omitting it (or sending an empty list) is plain single-turn
    behavior.
    """
    if not isinstance(raw, list):
        return []

    valid_turns = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        question = entry.get("question")
        answer = entry.get("answer")
        if not isinstance(question, str) or not isinstance(answer, str):
            continue
        valid_turns.append({"question": question, "answer": answer})

    return valid_turns[-MAX_CONVERSATION_HISTORY_TURNS:]


@myApp.route(route="ask", methods=["POST"])
def ask(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"status": "error", "message": "Request body must be valid JSON."}),
            status_code=400, mimetype="application/json",
        )

    question = body.get("question")
    if not question or not isinstance(question, str) or not question.strip():
        return func.HttpResponse(
            json.dumps({"status": "error", "message": "Missing required field 'question'."}),
            status_code=400, mimetype="application/json",
        )

    conversation_history = _parse_conversation_history(body.get("conversation_history"))

    result = answer_question(question.strip(), conversation_history=conversation_history)
    status_code = 200 if result["status"] != "error" else 502
    return func.HttpResponse(json.dumps(result), status_code=status_code, mimetype="application/json")
