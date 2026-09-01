"""Standalone experiment #2: instead of asking Bing to search the open web
and hoping it stays inside our domains (see bing_search.py — it mostly
doesn't), crawl ONLY within the provided seed URLs and their own same-domain
sub-pages, rank those sub-pages by title match against the question, fetch
the real content of the best match(es), and generate the answer strictly
from that fetched content.

Isolated on purpose — nothing here is imported by, or imports from,
function_app.py / helpdesk_answer.py / live_url_search.py / sharepoint_client.py.
Config comes only from environment variables (see bing_search.load_env_file),
never hardcoded.
"""

import os
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient

# Same fixed seed URLs as bing_search.py — the "provided websites" this
# experiment is only allowed to look inside.
FIXED_URLS = [
    "https://en.wikipedia.org/wiki/Lenovo",
    "https://en.wikipedia.org/wiki/History_of_Python",
    "https://intaglaptops.com/blogs/laptops-blogs",
]

REQUEST_TIMEOUT = 10
MAX_SUBLINKS_PER_SEED = 30
MAX_CONTENT_CHARS = 6000
MAX_PAGES_FOR_ANSWER = 2
USER_AGENT = "Mozilla/5.0 (compatible; crawl-search-test/1.0)"

ANSWER_INSTRUCTIONS = (
    "You are a support assistant. Answer the user's question using ONLY the "
    "page content provided below — never outside/prior knowledge. If none "
    "of the provided pages answer the question, say so clearly instead of "
    "guessing. When you use a fact, mention which numbered source it came "
    "from, e.g. [Source 1]."
)


def fetch_html(url):
    resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.text


def extract_title(html, fallback):
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    return fallback


def _is_utility_link(url):
    """Filters out navigation/admin chrome (e.g. Wikipedia's "Page
    information", "Talk:", "Special:", print views, action=... links) that
    otherwise pollute the candidate pool — these appear on every page with
    generic English titles and can out-rank real content pages."""
    parsed = urlparse(url)
    if parsed.query:
        return True
    last_segment = parsed.path.rsplit("/", 1)[-1]
    if ":" in last_segment:
        return True
    return False


def extract_links(html, base_url):
    """Same-domain links only — this is what keeps the crawl inside the
    provided website instead of wandering off across the open web."""
    soup = BeautifulSoup(html, "html.parser")
    base_domain = urlparse(base_url).netloc
    seen = set()
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        full_url = urljoin(base_url, href).split("#")[0]
        if urlparse(full_url).netloc != base_domain:
            continue
        if full_url in seen or full_url == base_url or _is_utility_link(full_url):
            continue
        seen.add(full_url)
        links.append({"url": full_url, "link_text": a.get_text(strip=True)})
    return links


def extract_text(html, max_chars=MAX_CONTENT_CHARS):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:max_chars]


def build_candidate_pool(seed_urls=None, max_sublinks_per_seed=MAX_SUBLINKS_PER_SEED):
    """Fetches each seed URL and lists it plus its same-domain sub-links as
    candidates — the pool of pages this experiment is allowed to consider."""
    seed_urls = seed_urls if seed_urls is not None else FIXED_URLS
    candidates = []
    seen_urls = set()

    for seed_url in seed_urls:
        try:
            html = fetch_html(seed_url)
        except requests.RequestException as exc:
            print(f"[crawl] failed to fetch seed {seed_url}: {exc}")
            continue

        title = extract_title(html, seed_url)
        if seed_url not in seen_urls:
            candidates.append({"url": seed_url, "title": title, "seed": seed_url})
            seen_urls.add(seed_url)

        for link in extract_links(html, seed_url)[:max_sublinks_per_seed]:
            if link["url"] in seen_urls:
                continue
            seen_urls.add(link["url"])
            candidates.append({
                "url": link["url"],
                "title": link["link_text"] or link["url"],
                "seed": seed_url,
            })

    return candidates


def rank_candidates(question, candidates, top_n=MAX_PAGES_FOR_ANSWER):
    """Scores each candidate's title against the question — this is the
    'look at all the titles and see which one is suitable' step."""
    scored = [(fuzz.token_set_ratio(question, c["title"]), c) for c in candidates]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[:top_n]


class CrawlAnswerer:
    def __init__(self):
        endpoint = os.environ["AGENT_PROJECT_ENDPOINT"]
        self.model = os.environ["AGENT_MODEL_DEPLOYMENT"]
        self.project_client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
        self.openai_client = self.project_client.get_openai_client()

    def answer_from_pages(self, question, pages):
        """pages: list of {url, title, text} already-fetched page content."""
        blocks = [
            f"[Source {i}] {p['title']} ({p['url']})\n{p['text']}"
            for i, p in enumerate(pages, 1)
        ]
        context = "\n\n---\n\n".join(blocks)

        completion = self.openai_client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": ANSWER_INSTRUCTIONS},
                {"role": "user", "content": f"Question: {question}\n\n{context}"},
            ],
        )
        choice = completion.choices[0]
        usage = completion.usage
        return {
            "answer": choice.message.content,
            "prompt_tokens": usage.prompt_tokens if usage else None,
            "completion_tokens": usage.completion_tokens if usage else None,
            "total_tokens": usage.total_tokens if usage else None,
        }

    def close(self):
        self.project_client.close()


def search_and_answer(question, answerer, top_n=MAX_PAGES_FOR_ANSWER):
    """Full pipeline for one question: crawl -> rank -> fetch top pages ->
    answer strictly from their content. Returns a dict with everything the
    CLI/HTML layer needs to show its work."""
    start = time.perf_counter()

    candidates = build_candidate_pool()
    ranked = rank_candidates(question, candidates, top_n=top_n)

    pages = []
    for score, c in ranked:
        try:
            html = fetch_html(c["url"])
        except requests.RequestException as exc:
            print(f"[crawl] failed to fetch candidate {c['url']}: {exc}")
            continue
        pages.append({
            "url": c["url"],
            "title": c["title"],
            "seed": c["seed"],
            "match_score": score,
            "text": extract_text(html),
        })

    if not pages:
        elapsed = time.perf_counter() - start
        return {
            "question": question,
            "candidates_considered": len(candidates),
            "ranked": [{"url": c["url"], "title": c["title"], "score": s} for s, c in ranked],
            "pages_used": [],
            "answer": "No page could be fetched from the provided sites for this question.",
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "latency_sec": round(elapsed, 3),
        }

    result = answerer.answer_from_pages(question, pages)
    elapsed = time.perf_counter() - start

    return {
        "question": question,
        "candidates_considered": len(candidates),
        "ranked": [{"url": c["url"], "title": c["title"], "score": s} for s, c in ranked],
        "pages_used": [{"url": p["url"], "title": p["title"], "match_score": p["match_score"]} for p in pages],
        "answer": result["answer"],
        "prompt_tokens": result["prompt_tokens"],
        "completion_tokens": result["completion_tokens"],
        "total_tokens": result["total_tokens"],
        "latency_sec": round(elapsed, 3),
    }
