"""Character-preserving, token-bounded chunks with paragraph/heading boundary preference."""

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache

import tiktoken

# cl100k_base is a local segmentation heuristic, NOT Gemini's token counter.
# Keep an additional conservative UTF-8 byte bound below the 2,048-token model limit.
MAX_EMBEDDING_BYTES = 1600


@lru_cache(maxsize=1)
def encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")


def token_count(text: str) -> int:
    return len(encoding().encode(text, disallowed_special=()))


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int
    chunk_id: str
    text: str
    content_hash: str
    start: int
    end: int
    tokens: int


def chunk_text(text: str, limit: int = 400, overlap: int = 60) -> tuple[ChunkDraft, ...]:
    if not text.strip() or not 8 <= limit <= 2048 or not 0 <= overlap < limit:
        raise ValueError("Invalid content or chunk limits")
    boundaries = [match.end() for match in re.finditer(r"\n\s*\n|\n(?=#{1,6} )", text)]
    chunks: list[ChunkDraft] = []
    start = 0
    while start < len(text):
        if len(chunks) >= 499:
            raise ValueError("Document exceeds the 499-chunk prototype limit")
        if token_count(text[start:]) <= limit and len(text[start:].encode()) <= MAX_EMBEDDING_BYTES:
            end = len(text)
            value = text[start:end]
            digest = hashlib.sha256(value.encode()).hexdigest()
            chunks.append(
                ChunkDraft(
                    len(chunks),
                    f"chunk-{len(chunks)}-{digest[:16]}",
                    value,
                    digest,
                    start,
                    end,
                    token_count(value),
                )
            )
            break
        low, high = start + 1, len(text)
        end = start
        while low <= high:
            mid = (low + high) // 2
            if (
                token_count(text[start:mid]) <= limit
                and len(text[start:mid].encode()) <= MAX_EMBEDDING_BYTES
            ):
                end = mid
                low = mid + 1
            else:
                high = mid - 1
        if end <= start:
            raise ValueError("Character cannot fit the token limit")
        if end < len(text):
            preferred = [
                boundary
                for boundary in boundaries
                if start < boundary <= end
                and limit // 2 <= token_count(text[start:boundary]) <= limit
            ]
            if preferred:
                end = preferred[-1]
        value = text[start:end]
        if not value.strip():
            raise ValueError("Whitespace-only chunk cannot be embedded")
        digest = hashlib.sha256(value.encode()).hexdigest()
        chunks.append(
            ChunkDraft(
                len(chunks),
                f"chunk-{len(chunks)}-{digest[:16]}",
                value,
                digest,
                start,
                end,
                token_count(value),
            )
        )
        if end == len(text):
            break
        next_start = end
        if overlap:
            low, high = start + 1, end
            while low <= high:
                mid = (low + high) // 2
                if token_count(text[mid:end]) <= overlap:
                    next_start = mid
                    high = mid - 1
                else:
                    low = mid + 1
        start = max(start + 1, next_start)
    return tuple(chunks)
