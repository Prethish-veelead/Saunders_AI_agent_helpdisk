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
    if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_API_KEY:
        raise EmbeddingConfigError("AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY must be set.")

    url = (
        f"{AZURE_OPENAI_ENDPOINT.rstrip('/')}/openai/deployments/"
        f"{AZURE_OPENAI_EMBEDDING_DEPLOYMENT}/embeddings"
        f"?api-version={AZURE_OPENAI_API_VERSION}"
    )
    headers = {"api-key": AZURE_OPENAI_API_KEY, "Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json={"input": text}, timeout=30)
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]
