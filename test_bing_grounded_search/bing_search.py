"""Standalone experiment: Bing-grounded search scoped to a fixed set of
domains, run through an Azure AI Foundry agent.

Isolated on purpose — nothing here is imported by, or imports from,
function_app.py / helpdesk_answer.py / live_url_search.py / sharepoint_client.py.
Config comes only from environment variables (see load_env_file below),
never hardcoded.
"""

import os
import time
from urllib.parse import urlparse

from azure.identity import DefaultAzureCredential
from azure.ai.agents import AgentsClient
from azure.ai.agents.models import BingGroundingTool, ListSortOrder, MessageRole

# Fixed test URLs (per instructions — not pulled from the SharePoint List for
# this experiment). Domains are derived from these, never hardcoded elsewhere.
FIXED_URLS = [
    "https://en.wikipedia.org/wiki/Lenovo",
    "https://en.wikipedia.org/wiki/History_of_Python",
    "https://intaglaptops.com/blogs/laptops-blogs",
]

DEFAULT_QUESTIONS = [
    "How do I reset a laptop to factory settings?",
    "What is the recommended way to replace a laptop battery?",
    "What are common causes of a laptop overheating?",
    "How do I fix a laptop that won't power on?",
]

AGENT_NAME = "bing-grounded-search-test"
AGENT_INSTRUCTIONS = (
    "You are a search assistant. Answer only using information found via "
    "web search results — never from prior knowledge. Always cite the "
    "source URLs you used. If the search results don't answer the "
    "question, say so clearly instead of guessing."
)


def load_env_file(path=None):
    """Loads KEY=VALUE lines from a .env file into os.environ, without
    overwriting variables already set in the real environment. No extra
    dependency (python-dotenv) needed for this one-time test folder."""
    path = path or os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def get_domains(urls=None):
    """Extracts and dedupes domains (netloc) from the given URLs, preserving
    first-seen order."""
    urls = urls if urls is not None else FIXED_URLS
    domains = []
    for url in urls:
        netloc = urlparse(url).netloc
        if netloc and netloc not in domains:
            domains.append(netloc)
    return domains


def build_scoped_query(question, domains):
    site_filter = " OR ".join(f"site:{d}" for d in domains)
    return f"({site_filter}) {question}"


def _extract_citations(message):
    """Pulls citation annotations off a thread message: each has the literal
    inline marker text the model left in the answer (e.g. '【5:4†source】'),
    plus the real URL/title it refers to. The Agents SDK has carried these
    annotations in slightly different places across preview versions, so
    this checks both locations documented for the current azure-ai-agents
    release. Returns a list of {"marker", "url", "title"} in the order they
    appear in the text."""
    annotations = []

    for text_message in getattr(message, "text_messages", None) or []:
        for annotation in getattr(text_message.text, "annotations", None) or []:
            citation = getattr(annotation, "url_citation", None)
            url = getattr(citation, "url", None) if citation else None
            if not url:
                continue
            marker = getattr(annotation, "text", None) or url
            annotations.append({
                "marker": marker,
                "url": url,
                "title": getattr(citation, "title", None) or url,
            })

    for annotation in getattr(message, "url_citation_annotations", None) or []:
        citation = getattr(annotation, "url_citation", None)
        url = getattr(citation, "url", None) if citation else None
        if not url:
            continue
        marker = getattr(annotation, "text", None) or url
        if not any(a["marker"] == marker and a["url"] == url for a in annotations):
            annotations.append({
                "marker": marker,
                "url": url,
                "title": getattr(citation, "title", None) or url,
            })

    return annotations


def _annotate_answer(answer_text, annotations):
    """Replaces each inline marker (e.g. '【5:4†source】') with a numbered
    footnote like '[1]', numbered by first-appearance of each unique URL, and
    returns (annotated_text, sources) where sources is an ordered list of
    {"n", "url", "title"} — this is what makes "where did this answer come
    from" visible instead of opaque SDK markers."""
    url_to_n = {}
    sources = []
    for a in annotations:
        if a["url"] not in url_to_n:
            url_to_n[a["url"]] = len(sources) + 1
            sources.append({"n": url_to_n[a["url"]], "url": a["url"], "title": a["title"]})

    annotated = answer_text
    for a in annotations:
        annotated = annotated.replace(a["marker"], f"[{url_to_n[a['url']]}]")

    return annotated, sources


class BingGroundedTester:
    """Thin wrapper around the Foundry agent/thread/run lifecycle for this
    experiment. One agent + one thread for the process lifetime; call
    cleanup() when done (server.py does this on shutdown, including on
    startup failure)."""

    def __init__(self):
        endpoint = os.environ["AGENT_PROJECT_ENDPOINT"]
        model = os.environ["AGENT_MODEL_DEPLOYMENT"]
        bing_connection_id = os.environ["AGENT_BING_CONNECTION_ID"]

        # Note: this installed azure-ai-agents version exposes AgentsClient
        # directly against the project endpoint — AIProjectClient in this
        # version has no .agents property (verified against the actual
        # installed package, not assumed from docs).
        self.agents_client = AgentsClient(
            endpoint=endpoint,
            credential=DefaultAzureCredential(),
        )

        bing_tool = BingGroundingTool(connection_id=bing_connection_id)

        self.agent = self.agents_client.create_agent(
            model=model,
            name=AGENT_NAME,
            instructions=AGENT_INSTRUCTIONS,
            tools=bing_tool.definitions,
        )
        self.thread = self.agents_client.threads.create()

    def ask(self, question, domains):
        query = build_scoped_query(question, domains)
        start = time.perf_counter()

        self.agents_client.messages.create(
            thread_id=self.thread.id,
            role=MessageRole.USER,
            content=query,
        )
        run = self.agents_client.runs.create_and_process(
            thread_id=self.thread.id,
            agent_id=self.agent.id,
        )
        elapsed = time.perf_counter() - start

        if run.status == "failed":
            raise RuntimeError(f"Agent run failed: {run.last_error}")

        messages = self.agents_client.messages.list(
            thread_id=self.thread.id, order=ListSortOrder.ASCENDING
        )
        answer_text = ""
        annotations = []
        for msg in messages:
            if msg.role == MessageRole.AGENT and msg.text_messages:
                answer_text = msg.text_messages[-1].text.value
                annotations = _extract_citations(msg)

        annotated_answer, sources = _annotate_answer(answer_text, annotations)
        citations = [s["url"] for s in sources]

        usage = run.usage
        return {
            "question": question,
            "query": query,
            "answer": answer_text,
            "annotated_answer": annotated_answer,
            "sources": sources,
            "citations": citations,
            "latency_sec": round(elapsed, 3),
            "prompt_tokens": usage.prompt_tokens if usage else None,
            "completion_tokens": usage.completion_tokens if usage else None,
            "total_tokens": usage.total_tokens if usage else None,
        }

    def cleanup(self):
        try:
            self.agents_client.delete_agent(self.agent.id)
        except Exception as exc:
            print(f"[cleanup] failed to delete agent {self.agent.id}: {exc}")
        finally:
            self.agents_client.close()
