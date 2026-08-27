"""
Text chunking for embedding/indexing — pure logic, no I/O.

Character-based chunking (not token-based) is a deliberate simplification:
roughly 4 characters per token for English prose is a good enough
approximation for chunk sizing, and avoids pulling in a tokenizer
dependency just to split text into pieces.
"""


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 150) -> list[str]:
    """Splits text into overlapping chunks, breaking on whitespace where
    possible so words aren't cut mid-way. Overlap preserves context across
    chunk boundaries so a fact split across two chunks is still findable
    from either one.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    text_len = len(text)
    while start < text_len:
        end = start + chunk_size
        if end < text_len:
            last_space = text.rfind(" ", start, end)
            if last_space > start:
                end = last_space
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= text_len:
            break
        # Guarantee forward progress even if overlap >= chunk_size or the
        # chunk boundary lands awkwardly — otherwise this can loop forever.
        next_start = max(end - overlap, start + 1)
        # Snap forward to the next word boundary so a chunk never starts
        # mid-word (e.g. "ord119" instead of "word119").
        if next_start < end:
            next_space = text.find(" ", next_start, end)
            if next_space != -1:
                next_start = next_space + 1
        start = next_start
    return chunks
