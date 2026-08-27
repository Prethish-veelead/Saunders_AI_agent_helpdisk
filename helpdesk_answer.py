"""
Question answering over the crawled-content index.
------------------------------------------------------

Classic RAG, hybrid search (keyword + vector, Azure AI Search's own RRF
fusion) + one generation call — per the earlier decision against agentic
retrieval (your SDD's own caveat: preview/portal-only maturity, not worth
the reliability risk even prioritizing quality).

Two source types are indexed today: SharePoint tickets and SharePoint
Library docs (the earlier crawled-URL pipeline was fully decommissioned —
see function_app.py's module docstring; live_url_search.py covers that
need per-question instead). The ticket-first fallback search
(search_chunks_all_sources) queries each source_type independently and
merges the results, rather than one combined query across both — see its
own docstring for why that matters. The index doesn't care which pipeline
populated a given chunk; `source_type` just tags it for citation/routing.

Uses gpt-4o (not mini) for answer generation, per the "best, not cheap"
direction — this is where a stronger model earns its cost, synthesizing
potentially long, messy crawled content into a coherent, cited answer.
Embeddings use text-embedding-3-large to match how the index was built
(same model must embed both the query and the indexed content, or vector
similarity is meaningless).

Deliberately reuses embedding_client.get_embedding() and
search_index.get_search_client() rather than duplicating that config/client
setup — both are already built and tested elsewhere in this project.
"""

import asyncio
import json
import logging
import os
from typing import Optional

import requests
from azure.search.documents.models import VectorizedQuery

from embedding_client import get_embedding
from search_index import get_search_client
# live_url_search is NOT imported here at module level — see the deferred
# import inside answer_question() below for why.

logger = logging.getLogger("helpdesk_answer")

AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")
AZURE_OPENAI_ANSWER_DEPLOYMENT = os.environ.get("AZURE_OPENAI_ANSWER_DEPLOYMENT", "gpt-4o")
# Cheap model used for the live-URL-search "which candidate(s) answer this"
# selection call (live_url_search.select_best_urls).
AZURE_OPENAI_CLASSIFY_DEPLOYMENT = os.environ.get("AZURE_OPENAI_CLASSIFY_DEPLOYMENT", "gpt-4o-mini")
SEARCH_TOP_K = int(os.environ.get("SEARCH_TOP_K", "5"))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("ANSWER_REQUEST_TIMEOUT_SECONDS", "30"))

MAX_RESPONSE_SOURCES = 3
# A single live-fetched page can be huge (a real Wikipedia article hit
# 75,682 chars in testing) with no cap before this. gpt-4o's context
# window hasn't actually been exceeded by anything observed yet, but nor
# is there anything stopping several large chunks from combining into a
# prompt that does — this bounds each chunk's contribution regardless.
MAX_CHUNK_CONTENT_CHARS = int(os.environ.get("MAX_CHUNK_CONTENT_CHARS", "12000"))


class AnswerConfigError(Exception):
    """Raised when required configuration is missing."""


def search_chunks(question: str, top_k: int = SEARCH_TOP_K, source_type_filter: str = None) -> list[dict]:
    """Hybrid search: keyword (search_text) + vector, fused by Azure AI
    Search's own ranking (RRF) — one search call, not a separate agent.

    source_type_filter, when given, restricts the search to one source
    (e.g. "ticket") — used to check resolved tickets first before falling
    back to the full index.
    """
    search_client = get_search_client()
    query_vector = get_embedding(question)

    vector_query = VectorizedQuery(vector=query_vector, k_nearest_neighbors=top_k, fields="vector")

    search_kwargs = dict(
        search_text=question,
        vector_queries=[vector_query],
        select=[
            "chunk_id", "page_id", "title", "url", "domain", "source_type", "content_type", "content",
            "ticket_id", "category", "sub_category", "status",
        ],
        top=top_k,
    )
    if source_type_filter:
        escaped = source_type_filter.replace("'", "''")
        search_kwargs["filter"] = f"source_type eq '{escaped}'"

    results = search_client.search(**search_kwargs)
    return [dict(r) for r in results]


# Every source_type actually indexed today (crawled_url was fully
# decommissioned — see function_app.py's module docstring). Kept as an
# explicit, hardcoded list rather than discovered dynamically (e.g. via a
# facet query): it's small, stable, and known by this project's own
# architecture, so a fixed list avoids an extra round-trip for no real
# benefit.
FALLBACK_SOURCE_TYPES = ["ticket", "library_doc"]


def search_chunks_all_sources(question: str, top_k: int = SEARCH_TOP_K) -> list[dict]:
    """The full-index fallback search — but run as one search_chunks()
    call PER source_type and merged, not one combined query across all
    source types.

    Found via live testing: a single combined hybrid (BM25 + vector)
    query's `top` cap is shared across ALL source types. Several
    near-duplicate test tickets whose content is nothing but the same
    phrase repeated ("vpn issue" over and over) score artificially high
    on the keyword/BM25 side purely from term-frequency repetition,
    despite genuinely weak relevance (vector similarity ~0.03) — enough
    of them filled every slot in a top_k=5 combined query, so a real,
    well-written, non-repetitive Library doc with a MUCH better relevance
    score (8.16, confirmed by searching source_type="library_doc" alone)
    never even reached the generation call. Searching each source_type
    independently guarantees every source gets its own fair top_k
    regardless of how noisy another source's data is.
    """
    chunks = []
    for source_type in FALLBACK_SOURCE_TYPES:
        chunks.extend(search_chunks(question, top_k=top_k, source_type_filter=source_type))
    return chunks


def _build_context_block(chunks: list[dict]) -> str:
    parts = []
    for i, chunk in enumerate(chunks):
        label = chunk.get("title") or chunk.get("url") or chunk.get("page_id") or f"source {i + 1}"
        source_ref = chunk.get("url") or chunk.get("page_id") or ""
        content = chunk.get("content", "")
        if len(content) > MAX_CHUNK_CONTENT_CHARS:
            content = content[:MAX_CHUNK_CONTENT_CHARS] + "\n[...truncated...]"
        parts.append(f"[{i + 1}] {label} ({source_ref})\n{content}")
    return "\n\n".join(parts)


def _build_history_block(conversation_history: Optional[list[dict]]) -> str:
    """Renders prior turns as a "Previous conversation:" section so the
    model can resolve references like "what about the second one". Empty
    string if there's no history — in which case the prompt is byte-for-
    byte identical to the single-turn case, unchanged from before this
    feature existed.

    This API is stateless: it never stores or tracks history itself. The
    caller (frontend) is responsible for resending prior turns on each
    request if multi-turn context is wanted — nothing here persists
    anything between calls.
    """
    if not conversation_history:
        return ""
    turns = "\n".join(
        f"Q: {turn['question']}\nA: {turn['answer']}" for turn in conversation_history
    )
    return f"Previous conversation:\n{turns}\n\n"


def generate_structured_response(
    question: str, chunks: list[dict], conversation_history: Optional[list[dict]] = None
) -> dict:
    """One generation call, forced to a JSON object matching:
      {"not_found": bool, "subject": str, "description": str,
       "answer": str, "follow_up_questions": [str, ...]}
    follow_up_questions is at most 3 items, but can be fewer (even empty)
    — each one must be answerable from the SAME reference content passed
    in, not just a plausible-sounding guess, so a shorter list is
    preferred over padding with unanswerable ones.
    subject/description restate the USER'S QUESTION (not the source's own
    subject/title) — that distinction matters because chunk titles (e.g.
    a ticket's subject) are not the same thing as what the user asked.

    conversation_history (optional): prior turns as a list of {"question",
    "answer"} dicts, most recent last — see _build_history_block(). Not
    stored anywhere by this function or this API; purely per-call context.
    """
    if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_API_KEY:
        raise AnswerConfigError("AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY must be set.")

    context_block = _build_context_block(chunks)
    history_block = _build_history_block(conversation_history)
    system_prompt = (
        "Your name is Saunders Assistant. If the user's question is asking "
        "who you are or what your name is, answer directly that you are "
        "Saunders Assistant — you don't need the reference content for "
        "identity questions like that.\n\n"
        "You answer helpdesk questions using ONLY the reference content below. "
        "Each reference is numbered and includes its source.\n\n"
        "Respond with ONLY a JSON object matching exactly this schema:\n"
        '{"not_found": boolean, "subject": string, "description": string, '
        '"answer": string, "category": string, "sub_category": string, '
        '"follow_up_questions": [string, string, string]}\n\n'
        "Rules:\n"
        "- subject and description are a clean restatement of the USER'S "
        "QUESTION (not the source document's own subject/title). subject is "
        "a short phrase; description is one sentence.\n"
        "- Do not include citation markers, footnotes, or reference numbers "
        "in the answer text.\n"
        "- Format the answer so it is easy to scan, like a helpful support "
        "agent's reply: start with a one-sentence direct answer, then use "
        "short bullet points on their own lines (each starting with \"- \") "
        "for any steps, options, specs, or reasons — one idea per bullet, "
        "plain language, no filler. Use a plain sentence with no bullets "
        "only when the whole answer is a single simple fact with nothing to "
        "list. Never write the answer as one dense paragraph when it "
        "contains more than one distinct point.\n"
        "- category and sub_category: a short IT/HR/Finance-style category "
        "and a more specific subcategory that best fit the reference content "
        "and question. If the reference content doesn't already indicate a "
        "category, infer your best guess from the content itself.\n"
        "- follow_up_questions must be exactly 3 relevant questions someone "
        "might naturally ask next — but ONLY questions you can already see "
        "the answer to in the reference content above. Do not suggest a "
        "plausible-sounding question whose answer isn't actually in the "
        "reference content — that would lead the user to a dead end. If "
        "the reference content doesn't support 3 genuinely answerable "
        "follow-ups, return fewer (even an empty list) rather than padding "
        "with ones you can't back up.\n"
        "- If the reference content does not contain enough information to "
        "answer the question, set not_found=true and leave subject, "
        "description, answer as empty strings and follow_up_questions as "
        "an empty list.\n\n"
        f"{history_block}"
        f"Reference content:\n{context_block}"
    )

    url = (
        f"{AZURE_OPENAI_ENDPOINT.rstrip('/')}/openai/deployments/"
        f"{AZURE_OPENAI_ANSWER_DEPLOYMENT}/chat/completions"
        f"?api-version={AZURE_OPENAI_API_VERSION}"
    )
    headers = {"api-key": AZURE_OPENAI_API_KEY, "Content-Type": "application/json"}
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        "temperature": 0.0,
        "max_tokens": 900,
        "response_format": {"type": "json_object"},
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return json.loads(content)


def _dedupe_and_rank(chunks: list[dict]) -> list[dict]:
    """One entry per unique source, keeping only its highest-scoring
    chunk, sorted by score descending. Dedup key is page_id — that already
    doubles as ticket_id for tickets (page_id was deliberately set to
    ticket_id in sync_tickets_timer for exactly this kind of reuse), so no
    separate ticket_id branch is needed.
    """
    best_by_page: dict[str, dict] = {}
    for c in chunks:
        key = c.get("page_id") or c.get("url") or c.get("title")
        score = c.get("@search.score", 0)
        existing = best_by_page.get(key)
        if existing is None or score > existing.get("@search.score", 0):
            best_by_page[key] = c
    return sorted(best_by_page.values(), key=lambda c: c.get("@search.score", 0), reverse=True)


def _build_sources(chunks: list[dict]) -> list[dict]:
    return [
        {
            "title": c.get("title") or c.get("url") or c.get("page_id"),
            "url": c.get("url"),
            "source_type": c.get("source_type"),
        }
        for c in chunks
    ]


def _generate_and_format(
    question: str, chunks: list[dict], conversation_history: Optional[list[dict]] = None
) -> dict:
    """Runs generation against the full (non-deduped) chunk set, then
    builds the final response shape from the deduped, top-ranked sources.
    """
    structured = generate_structured_response(question, chunks, conversation_history)

    if structured.get("not_found"):
        return {"status": "not_found"}

    ranked_sources = _dedupe_and_rank(chunks)
    top_sources = ranked_sources[:MAX_RESPONSE_SOURCES]
    top = top_sources[0] if top_sources else {}

    return {
        "subject": structured.get("subject", ""),
        "description": structured.get("description", ""),
        "status": "answered",
        "answer": structured.get("answer", ""),
        "category": top.get("category", ""),
        "subcategory": top.get("sub_category", ""),
        "source": top.get("source_type", ""),
        "sources": _build_sources(top_sources),
        "follow_up_questions": structured.get("follow_up_questions", []),
    }


def answer_question(question: str, conversation_history: Optional[list[dict]] = None) -> dict:
    """Main entry point. Checks resolved/closed tickets first — a real
    support conversation that reached a resolution is often a better
    answer than generic documentation — and only falls back to the full
    index (crawled URLs + Library docs + tickets) if ticket data doesn't
    answer it. Returns:
      {"subject", "description", "status": "answered", "answer",
       "category", "subcategory", "source", "sources": [{"title", "url", "source_type"}, ...],
       "follow_up_questions": [str, str, str]}
      {"status": "not_found"}
      {"status": "error", "message": "..."}

    conversation_history (optional): prior turns as [{"question", "answer"}, ...],
    most recent last — passed straight through to generate_structured_response()
    for context only. This API is stateless: it does not store, cache, or
    remember history itself. If the caller wants multi-turn behavior, it
    must resend the relevant prior turns on every request; omit it (or
    pass an empty list) for plain single-turn behavior, unchanged from
    before this parameter existed.
    """
    try:
        ticket_chunks = search_chunks(question, source_type_filter="ticket")
    except Exception:  # noqa: BLE001
        logger.exception("Ticket search failed for question: %s", question[:100])
        ticket_chunks = []

    if ticket_chunks:
        try:
            result = _generate_and_format(question, ticket_chunks, conversation_history)
        except Exception:  # noqa: BLE001
            logger.exception("Ticket answer generation failed for question: %s", question[:100])
            result = None

        if result is not None and result.get("status") != "not_found":
            return result

    # Live per-question URL search. NOTE: this does NOT gate on whether
    # ticket_chunks was non-empty — Azure AI Search's vector kNN always
    # returns the top-K nearest tickets for ANY query regardless of actual
    # relevance (confirmed against production: an unrelated question still
    # returned 5 "ticket" chunks, all scoring ~0.03), so "ticket_chunks
    # non-empty" is true almost universally once the index has more than a
    # handful of tickets. Gating on it would silently disable this feature
    # almost entirely rather than skip it only for well-matched cases.
    # Latency is instead kept in check via tighter timeouts/sub-link caps
    # in live_url_search.py (verified ~40% faster against real DDG/HTTP).
    try:
        # Deferred import: live_url_search pulls in trafilatura, bs4,
        # rapidfuzz, and httpx (via searxng_client) at its own module
        # level — real import weight that a cold Azure Functions worker
        # would otherwise pay on EVERY invocation (including the majority
        # of requests that resolve from the ticket index above and never
        # reach this branch at all).
        # Deferring it to first actual use means that cost is only paid
        # when a request genuinely needs live URL search.
        from live_url_search import live_url_answer

        live_config = {
            "endpoint": AZURE_OPENAI_ENDPOINT,
            "api_key": AZURE_OPENAI_API_KEY,
            "api_version": AZURE_OPENAI_API_VERSION,
            "select_deployment": AZURE_OPENAI_CLASSIFY_DEPLOYMENT,
        }
        live_result = asyncio.run(live_url_answer(question, live_config))
    except Exception:  # noqa: BLE001 — live search is a bonus path, never block the index fallback
        logger.exception("Live URL search failed for question: %s", question[:100])
        live_result = None

    if live_result is not None:
        return live_result

    # Ticket search returned nothing, or didn't answer it — fall back to
    # everything (Library docs + tickets), searched per source_type and
    # merged (see search_chunks_all_sources) so noisy data in one source
    # can't crowd a genuinely better match in another out of contention.
    try:
        chunks = search_chunks_all_sources(question)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Search failed for question: %s", question[:100])
        return {"status": "error", "message": f"Search failed: {exc}"}

    if not chunks:
        return {"status": "not_found"}

    try:
        return _generate_and_format(question, chunks, conversation_history)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Answer generation failed for question: %s", question[:100])
        return {"status": "error", "message": f"Answer generation failed: {exc}"}
