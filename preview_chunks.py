"""
Chunk previewer — zero Azure dependencies.
---------------------------------------------

Sanity-checks the chunking step in isolation, before anything touches
Cosmos DB, Blob, Search, or Azure OpenAI. Useful as the very first local
test: if chunking looks wrong here, nothing downstream will be right
either, and this is the cheapest, fastest place to catch it.

Usage:
    python preview_chunks.py path/to/sample.txt
    echo "some text" | python preview_chunks.py
    python preview_chunks.py path/to/sample.txt --chunk-size 500 --overlap 75
"""

import argparse
import sys

from chunking import chunk_text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("file", nargs="?", help="Path to a text file. Omit to read from stdin.")
    parser.add_argument("--chunk-size", type=int, default=1000)
    parser.add_argument("--overlap", type=int, default=150)
    parser.add_argument("--full", action="store_true", help="Print full chunk text, not just a preview.")
    args = parser.parse_args()

    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    chunks = chunk_text(text, chunk_size=args.chunk_size, overlap=args.overlap)

    print(f"Input length: {len(text)} characters")
    print(f"chunk_size={args.chunk_size}, overlap={args.overlap}")
    print(f"Produced {len(chunks)} chunk(s)\n")

    for i, c in enumerate(chunks):
        print(f"--- chunk {i} ({len(c)} chars, {len(c.split())} words) ---")
        print(c if args.full else c[:200] + ("..." if len(c) > 200 else ""))
        print()

    if len(chunks) > 1:
        lengths = [len(c) for c in chunks]
        print(f"Chunk length stats: min={min(lengths)}, max={max(lengths)}, avg={sum(lengths)//len(lengths)}")


if __name__ == "__main__":
    main()
