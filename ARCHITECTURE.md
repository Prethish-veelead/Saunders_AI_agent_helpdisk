# How the Code Works

This explains the actual logic of the helpdesk assistant — not the Azure
deployment (see `README.md` for that), but what the code does when a
question comes in, and how data gets there in the first place.

---

## 1. The big picture

Three possible sources feed one search index, and one endpoint answers
questions from whichever source has the best content:

```
SharePoint Tickets  ──┐
SharePoint Library  ──┼──►  Azure AI Search index  ──►  /api/ask  ──►  Answer
(Live web search)  ───┘        (tickets + docs)
```

- **Tickets** and **Library documents** are synced into the index on a
  schedule (every 15 min / every hour).
- **Live web search** (currently on hold — see README §6) would answer
  questions from tracked external URLs in real time, with nothing stored.

---

## 2. Getting data INTO the index (the sync side)

Two scheduled timer functions in `function_app.py`:

### `sync_tickets_timer` (every 15 min)
1. Reads every ticket from the SharePoint Tickets list whose Status is
   "Closed" or "Resolved" — only finished resolutions are trustworthy
   enough to treat as knowledge.
2. For each ticket, pulls its comments too (`get_ticket_comments`) and
   combines subject + description + comments into one block of text.
3. Splits that text into chunks (`chunking.py`), turns each chunk into a
   vector embedding (`embedding_client.py`, using `text-embedding-3-large`),
   and writes it to the search index tagged `source_type="ticket"`.
4. **Skips re-indexing unchanged tickets** — a content hash is compared
   against what's already indexed, so nothing gets needlessly re-embedded
   every 15 minutes.
5. **Cleans up removed tickets** — if a ticket that was indexed is no
   longer Closed/Resolved (reopened, deleted, status changed), its old
   chunks are deleted from the index.

### `sync_library_docs_timer` (every hour)
Same idea, but for the Library:
1. Lists every file in the SharePoint Library.
2. Extracts text from it (`.docx`, `.pdf`, `.pptx`, `.md`, `.html` are all
   supported — see `SUPPORTED_LIBRARY_EXTENSIONS` in `sharepoint_client.py`).
3. Chunks, embeds, and indexes it tagged `source_type="library_doc"`.
4. Same unchanged-content skip and removed-document cleanup as tickets.

Both timers authenticate to SharePoint via the Function App's **Managed
Identity** (see README §4) — no stored password/secret.

---

## 3. Answering a question (`/api/ask`)

This is the interesting part — `helpdesk_answer.answer_question()` tries
sources in a specific order, and only falls through to the next one if the
previous one doesn't actually answer the question:

```
1. Search ONLY tickets for this question
   → found a good answer? Return it immediately. Done.
   → nothing good? Continue.

2. Try live web search (currently always returns "nothing" — on hold)
   → found something? Return it. Done.
   → nothing? Continue.

3. Search tickets AND library docs, but SEPARATELY, then merge
   → still nothing? Return "not_found".
```

**Why tickets are tried first, alone**: a real resolved support ticket is
often a better, more specific answer than generic documentation — if
someone already fixed this exact problem, that's usually the best answer.

**Why step 3 searches tickets and library docs separately, not combined**:
this was a real bug we found and fixed. A single combined search can let
noisy data from one source crowd out a better answer from another — e.g.
several near-identical spam test tickets repeating "vpn issue" scored
artificially high on keyword matching and filled every slot in the results,
before a well-written library document about the same topic (which scored
far better on relevance, just not on repetition) ever got a chance to be
considered. Searching each source on its own and merging the results
guarantees every source gets a fair shot regardless of how noisy the
others' data is. (See `search_chunks_all_sources()` in `helpdesk_answer.py`.)

**How "found a good answer" is decided**: every candidate answer goes
through one call to GPT-4o (`generate_structured_response()`), which is
told to answer *using only the reference content it was given* — if that
content genuinely doesn't contain enough to answer the question, it
returns `not_found` instead of guessing. This is why some questions
correctly come back as "not found" even when *some* loosely-related
content exists — the model is choosing not to make things up.

**Answer formatting**: answers are required to lead with a one-sentence
direct answer, then break multi-part content into bullet points — not one
dense paragraph.

**Follow-up questions**: the 3 suggested follow-ups are constrained to
only ones the model can already see the answer to in the same reference
content — so clicking one doesn't lead to a dead end. Fewer than 3 (even
zero) is preferred over padding with a guess.

---

## 4. Live URL search (on hold, but here's how it would work)

`live_url_search.py` implements a "find the site, then find the exact
page, then read its own relevant links" flow:

1. Fuzzy-match the question against a small tracked list of URLs (from a
   SharePoint List) to shortlist candidates.
2. Get a quick search snippet for each candidate via the search backend
   (SearXNG, self-hosted — see README §6), to help an AI model pick the
   single best domain (or occasionally two, if both genuinely help).
3. Within that one domain, run a bounded search-and-refine loop (max 3
   rounds) to find the *specific* page that answers the question — not
   just the homepage.
4. Once a page is found, also scan **its own outbound links** and pull in
   a few of the most relevant ones as extra grounding content (e.g. a
   product page linking to related products).
5. Feed all of that content into the same GPT-4o answer-generation step
   used for tickets/library docs.

This is fully built and tested, but not currently active — no tracked URLs
are configured for the new deployment, pending the team's decision on
whether to use this approach at all (see README §6/§9).

---

## 5. File-by-file map

| File | Role |
|---|---|
| `function_app.py` | Entry point — the 3 Azure Functions (`ask`, `sync_tickets_timer`, `sync_library_docs_timer`) |
| `sharepoint_client.py` | All SharePoint/Graph API access — auth, reading tickets/comments/library docs/URLs |
| `chunking.py` | Splits long text into overlapping chunks for embedding |
| `embedding_client.py` | Calls Azure OpenAI to turn text into vectors |
| `search_index.py` | Azure AI Search index schema + client setup |
| `helpdesk_answer.py` | The `/api/ask` logic — ticket-first search, fallback merge, answer generation |
| `live_url_search.py` | Live web search flow (on hold) |
| `searxng_client.py` | Thin client for the self-hosted SearXNG search backend |

---

## 6. Testing

Each file has a matching `test_*.py` — all run locally with no real Azure
connection needed (everything's mocked):
```bash
python test_ticket_sync.py
python test_library_extraction.py
python test_live_url_search.py
# ...etc — see the repo root for the full list
```
