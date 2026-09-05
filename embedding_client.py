"""
Shared Azure OpenAI embedding client — used by the SharePoint Library and
ticket sync paths (function_app.py) to embed indexed chunks, and by
helpdesk_answer.py to embed incoming questions, so both sides of the
vector search use the same model.
"""

import os

import requests

AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")
AZURE_OPENAI_EMBEDDING_DEPLOYMENT = os.environ.get(
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large"
)


class EmbeddingConfigError(Exception):
    """Raised when required Azure OpenAI configuration is missing."""


def get_embedding(text: str) -> list[float]:
    return get_embeddings_batch([text])[0]


def get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """Embeds many texts in one HTTP call — the embeddings API accepts a
    list for "input" and returns one embedding per item, in the same
    order. Used by crawl_url_search.py to score potentially 100+
    candidate titles against a question without one network round-trip
    per candidate (which would make live per-question crawling far too
    slow). Returns [] for an empty input list without making a call.
    """
    if not texts:
        return []
    if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_API_KEY:
        raise EmbeddingConfigError("AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY must be set.")

    url = (
        f"{AZURE_OPENAI_ENDPOINT.rstrip('/')}/openai/deployments/"
        f"{AZURE_OPENAI_EMBEDDING_DEPLOYMENT}/embeddings"
        f"?api-version={AZURE_OPENAI_API_VERSION}"
    )
    headers = {"api-key": AZURE_OPENAI_API_KEY, "Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json={"input": texts}, timeout=60)
    resp.raise_for_status()
    # The API's own "data" order matches input order, but each item also
    # carries an explicit "index" — sorting by that is a cheap guarantee
    # against ever silently mismatching an embedding to the wrong text.
    data = sorted(resp.json()["data"], key=lambda item: item["index"])
    return [item["embedding"] for item in data]
