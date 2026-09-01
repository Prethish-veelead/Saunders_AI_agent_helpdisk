# Saunders Helpdesk Assistant — Client Deployment

This document tracks the **client's** Azure deployment (ICTSND tenant) — what's
been created, current configuration, and what's still pending. It does not
cover ad-hoc testing/debugging done against our own account; only real,
lasting client-side setup.

For how the code itself actually works (the search/answer logic, sync
pipeline, file-by-file breakdown), see **[ARCHITECTURE.md](ARCHITECTURE.md)**.

Last updated: 2026-08-28

---

## 1. Azure account

| | |
|---|---|
| Subscription | `Azure subscription 1` (`c9f6e54d-63af-4048-a809-01247a5e63ce`) |
| Tenant | `ICTSND.onmicrosoft.com` |
| Resource group | `saunders-ai-agent` |
| Region | Australia East |

---

## 2. Azure resources created

| Resource | Name | Purpose | Status |
|---|---|---|---|
| Resource Group | `saunders-ai-agent` | Holds everything below | ✅ Active |
| Storage Account | `saundersaistorage` | Required by the Function App | ✅ Active |
| Azure AI Search | `saunders-ai-search` (Basic tier) | Search index for tickets + library docs | ✅ Active, index created |
| Azure OpenAI | `Saundersaimodels` | Answer generation + embeddings | ✅ Active |
| Function App | `saunders-helpdesk-func` (Linux, Python 3.11, Consumption) | The actual helpdesk app (`ask`, `sync_tickets_timer`, `sync_library_docs_timer`) | ✅ Deployed and running |
| Container Registry | `saundersaiacr` (Basic) | For a future SearXNG image | ⏸ Created, empty — on hold |
| Container Apps Environment | `saunders-ai-env` | For a future SearXNG container | ⏸ Created, empty — on hold |

**Note**: `saunders-travel-classify-func` in the same resource group is a
**separate, unrelated application** (not part of this project) — do not
confuse it with `saunders-helpdesk-func`.

---

## 3. Azure OpenAI deployments

| Deployment name | Model | Used for |
|---|---|---|
| `gpt-4o` | gpt-4o | Final answer generation |
| `text-embedding-3-large` | text-embedding-3-large | Search index embeddings |
| — | gpt-5.1 / gpt-4o-mini | **Not yet deployed** — needed for the live-URL-search classify/decision step, which is on hold anyway (see §6) |

---

## 4. Authentication — Managed Identity (no client secret)

`saunders-helpdesk-func` uses a **System-Assigned Managed Identity** to read
SharePoint via Microsoft Graph — there is no stored client secret for this.

- Managed Identity: enabled on the Function App
- Graph permission granted: `Sites.Read.All` (application permission,
  granted directly via a Graph app-role assignment — this doesn't appear in
  the Portal's normal "API permissions" screen since it's a managed
  identity, not an app registration)

If the Function App is ever deleted and recreated, this grant is lost and
must be redone (see §7 for the exact commands).

---

## 5. SharePoint configuration

| | |
|---|---|
| Site | `https://ictsnd.sharepoint.com/sites/ICT/Helpdesk` |
| Hostname | `ictsnd.sharepoint.com` |
| Site path | `/sites/ICT/Helpdesk` |

| List / Library | Purpose | List ID |
|---|---|---|
| Knowledge Documents (`HDKnowledgeDocuments`) | Library docs synced into the index | `6d6c3314-38f4-4324-b182-baeb7a2f5a86` |
| Tickets (`HDTickets`) | Resolved/closed tickets synced into the index | `41883d23-4e68-4d12-9926-99a41559bd9e` |
| Ticket Comments (`HD_TicketComments`) | Comments appended to ticket content | `547d34ef-8363-460d-bac4-fa2195739f16` |
| PublicKBArticle | Would become the tracked-URL list for live web search | `236cc015-b44c-4cea-89de-7fafa72184a9` — **not wired in yet**, live URL search is on hold |

**Field names**: currently using the same internal column names as the
previous deployment (`Status`, `TicketID0`, `Title`, `Description`,
`Department`, `SubCategory`, `CommentText`, `TicketLookupLookupId`) as a
best guess, since the exact schema couldn't be verified in advance. **Confirmed
correct for `Status`** — a real ticket (`HD-2`) was successfully matched and
indexed. Other fields should be spot-checked as more real data is added.

---

## 6. Feature status

| Feature | Status |
|---|---|
| Answer questions from resolved/closed tickets | ✅ Working — verified with real data |
| Answer questions from Library documents | ✅ Working — connection verified, library currently has 0 documents to test with |
| Live URL search (SearXNG-backed) | ⏸ **On hold** — pending the team's decision on the URL-based-answering approach. Container Registry/Environment exist but no container is deployed. |

---

## 7. Sync schedule

| Timer | Schedule | Setting |
|---|---|---|
| Ticket sync | Every 15 minutes | `TICKET_SYNC_CRON = 0 */15 * * * *` |
| Library doc sync | Every hour | `LIBRARY_SYNC_CRON = 0 0 * * * *` |

**To change**: Azure Portal → `saunders-helpdesk-func` → **Settings** →
**Environment variables** → edit the value → **Save** (restarts the app).
Format is NCRONTAB: `{second} {minute} {hour} {day} {month} {day-of-week}`.

---

## 8. Testing

```
Endpoint: https://saunders-helpdesk-func.azurewebsites.net/api/ask
Method:   POST
Header:   Content-Type: application/json
Query:    ?code=<function-key>
```

A key named `team-test` exists specifically for sharing with the team
(rather than handing out the `default` key). **Never commit its value** —
get it from Azure Portal → `saunders-helpdesk-func` → **App keys** →
`team-test`, and share it directly with teammates outside of git/docs.

```bash
curl -X POST "https://saunders-helpdesk-func.azurewebsites.net/api/ask?code=<team-test-key>" \
  -H "Content-Type: application/json" \
  -d '{"question": "how do I reset my password"}'
```

A simple browser-based test form also exists in this repo: `test-ask-azure.html`.

---

## 9. Known pending items

- Deploy a classify-model (gpt-5.1 or gpt-4o-mini) on `Saundersaimodels` — needed only once live URL search is picked back up.
- Verify the remaining field names (`TicketID0`, `Department`, `SubCategory`, etc.) once more real ticket/comment data exists.
- Decide on and implement the URL-based answering approach (SearXNG vs. a paid search API), then wire up `PublicKBArticle` and deploy the container.

---

## Change log

- **2026-08-28** — Initial client deployment: resource group, Storage, Azure
  AI Search + index, Azure OpenAI (gpt-4o + embeddings), Function App with
  Managed Identity auth, all 4 SharePoint List IDs resolved, sync schedules
  configured and verified working against real ticket data.
