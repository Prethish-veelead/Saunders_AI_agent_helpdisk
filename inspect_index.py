"""
Search index inspector.
---------------------------

After running the crawler or library sync locally, this is how you confirm
processing actually happened — not by trusting log lines, but by looking
directly at what's in the index: how many chunks, what content, from which
source, with what metadata.

Usage:
    python inspect_index.py                              # everything (up to 50)
    python inspect_index.py --domain example.com
    python inspect_index.py --source-type library_doc
    python inspect_index.py --page-id "https://example.com/blog-post"
    python inspect_index.py --count-only                 # just totals, no content
"""

import argparse

from search_index import get_search_client


def build_filter(args: argparse.Namespace) -> str | None:
    clauses = []
    if args.domain:
        clauses.append(f"domain eq '{args.domain}'")
    if args.source_type:
        clauses.append(f"source_type eq '{args.source_type}'")
    if args.page_id:
        clauses.append(f"page_id eq '{args.page_id}'")
    return " and ".join(clauses) if clauses else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", default=None)
    parser.add_argument("--source-type", default=None, help="crawled_url | library_doc | list_qa")
    parser.add_argument("--page-id", default=None)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--count-only", action="store_true")
    args = parser.parse_args()

    client = get_search_client()
    filter_str = build_filter(args)

    if args.count_only:
        results = client.search(search_text="*", filter=filter_str, include_total_count=True, top=0)
        print(f"Total matching documents: {results.get_count()}")
        return

    results = client.search(
        search_text="*",
        filter=filter_str,
        select=[
            "chunk_id", "page_id", "title", "url", "domain",
            "content_type", "source_type", "crawl_depth", "crawled_at", "content",
        ],
        top=args.limit,
    )

    count = 0
    domains_seen: dict[str, int] = {}
    for doc in results:
        count += 1
        domains_seen[doc.get("domain", "")] = domains_seen.get(doc.get("domain", ""), 0) + 1
        preview = (doc.get("content") or "").replace("\n", " ")[:150]
        print(f"--- chunk {count} ---")
        print(f"  chunk_id     : {doc.get('chunk_id')}")
        print(f"  page_id      : {doc.get('page_id')}")
        print(f"  title        : {doc.get('title')}")
        print(f"  url          : {doc.get('url')}")
        print(f"  domain       : {doc.get('domain')}")
        print(f"  source_type  : {doc.get('source_type')}")
        print(f"  content_type : {doc.get('content_type')}")
        print(f"  crawl_depth  : {doc.get('crawl_depth')}")
        print(f"  crawled_at   : {doc.get('crawled_at')}")
        print(f"  content      : {preview}...")
        print()

    print(f"Shown: {count} chunk(s)")
    if domains_seen:
        print("By domain:", dict(sorted(domains_seen.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()
