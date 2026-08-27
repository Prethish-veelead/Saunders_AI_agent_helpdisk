"""
Azure AI Search index schema and client setup.
--------------------------------------------------

One index, shared across all three content sources this project produces
(crawled URLs, SharePoint List Q&A, Library documents) — distinguished by
`source_type` so retrieval and citations can tell them apart, and so the
List Q&A ingestion path (not yet built) has nothing to change here when
it's added — it just writes documents with source_type="list_qa".

Vector field sized for text-embedding-3-large (3072 dimensions) per the
"best, not cheap" embedding choice made earlier.
"""

import os

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SearchableField,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)

SEARCH_ENDPOINT = os.environ.get("SEARCH_ENDPOINT", "")
SEARCH_KEY = os.environ.get("SEARCH_KEY", "")
SEARCH_INDEX_NAME = os.environ.get("SEARCH_INDEX_NAME", "crawled-content-index")
EMBEDDING_DIMENSIONS = int(os.environ.get("EMBEDDING_DIMENSIONS", "3072"))  # text-embedding-3-large

VECTOR_PROFILE_NAME = "default-vector-profile"
VECTOR_ALGORITHM_NAME = "default-hnsw"


class SearchConfigError(Exception):
    """Raised when required Azure AI Search configuration is missing."""


def _require_config() -> None:
    if not SEARCH_ENDPOINT or not SEARCH_KEY:
        raise SearchConfigError("SEARCH_ENDPOINT and SEARCH_KEY must be set.")


def get_index_client() -> SearchIndexClient:
    _require_config()
    return SearchIndexClient(endpoint=SEARCH_ENDPOINT, credential=AzureKeyCredential(SEARCH_KEY))


def get_search_client() -> SearchClient:
    _require_config()
    return SearchClient(
        endpoint=SEARCH_ENDPOINT,
        index_name=SEARCH_INDEX_NAME,
        credential=AzureKeyCredential(SEARCH_KEY),
    )


def build_index_definition() -> SearchIndex:
    vector_search = VectorSearch(
        profiles=[
            VectorSearchProfile(
                name=VECTOR_PROFILE_NAME,
                algorithm_configuration_name=VECTOR_ALGORITHM_NAME,
            )
        ],
        algorithms=[HnswAlgorithmConfiguration(name=VECTOR_ALGORITHM_NAME)],
    )

    fields = [
        SimpleField(name="chunk_id", type=SearchFieldDataType.String, key=True),
        SimpleField(name="page_id", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SearchableField(name="title", type=SearchFieldDataType.String),
        SimpleField(name="url", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="domain", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="parent_url", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="crawl_depth", type=SearchFieldDataType.Int32, filterable=True),
        SimpleField(name="content_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
        # Distinguishes this project's three content sources — not used yet
        # by the crawler alone, but means nothing here needs to change when
        # the SharePoint List Q&A ingestion path is added later.
        SimpleField(name="source_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="crawled_at", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
        SimpleField(name="content_hash", type=SearchFieldDataType.String, filterable=True),
        # ticket_id/status: populated only for source_type="ticket".
        # category/sub_category: shared across all three source types —
        # AI-classified for crawled_url, taken directly from SharePoint
        # metadata for library_doc, and from the ticket's Department field
        # for ticket (renamed from "department" so all three sources use
        # one common field pair).
        SimpleField(name="ticket_id", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="category", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="sub_category", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="status", type=SearchFieldDataType.String, filterable=True),
        SearchField(
            name="vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=EMBEDDING_DIMENSIONS,
            vector_search_profile_name=VECTOR_PROFILE_NAME,
        ),
    ]

    return SearchIndex(name=SEARCH_INDEX_NAME, fields=fields, vector_search=vector_search)


def create_or_update_index() -> None:
    """Idempotent — safe to call every time the indexing script runs."""
    client = get_index_client()
    client.create_or_update_index(build_index_definition())


def get_indexed_content_hash(page_id: str) -> str | None:
    """Content hash of the first indexed chunk for this page_id, or None if
    nothing is indexed for it yet. This is the dedup/change-detection check
    for Library docs and tickets — a sync run compares this against the
    freshly computed hash to decide whether to skip re-embedding/re-indexing
    unchanged content, instead of tracking that in Cosmos DB.
    """
    client = get_search_client()
    escaped_page_id = page_id.replace("'", "''")
    results = client.search(
        search_text="*",
        filter=f"page_id eq '{escaped_page_id}'",
        select=["content_hash"],
        top=1,
    )
    for r in results:
        return r.get("content_hash")
    return None


def get_indexed_page_ids(source_type: str, page_size: int = 1000) -> set[str]:
    """Distinct page_ids currently indexed for one source_type — the
    "what's currently tracked" side of removal-detection for Library docs
    and tickets, read straight from the index instead of a separate Cosmos
    tracking list. Paginated via $skip since a source can have more chunks
    than fit in one page.
    """
    client = get_search_client()
    escaped = source_type.replace("'", "''")
    page_ids: set[str] = set()
    skip = 0
    while True:
        results = client.search(
            search_text="*",
            filter=f"source_type eq '{escaped}'",
            select=["page_id"],
            top=page_size,
            skip=skip,
        )
        batch = [r["page_id"] for r in results]
        if not batch:
            break
        page_ids.update(batch)
        if len(batch) < page_size:
            break
        skip += page_size
    return page_ids


def delete_chunks_for_page(page_id: str) -> int:
    """Deletes every indexed chunk belonging to one page/document (crawled
    URL or Library doc, identified by page_id). Returns the number of
    chunks deleted — 0 if none were found.
    """
    client = get_search_client()
    escaped_page_id = page_id.replace("'", "''")
    results = client.search(
        search_text="*",
        filter=f"page_id eq '{escaped_page_id}'",
        select=["chunk_id"],
        top=1000,
    )
    chunk_ids = [r["chunk_id"] for r in results]
    if not chunk_ids:
        return 0
    client.delete_documents([{"chunk_id": cid} for cid in chunk_ids])
    return len(chunk_ids)
