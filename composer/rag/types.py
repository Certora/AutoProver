from collections.abc import Sequence
from typing import TypedDict
from dataclasses import dataclass

@dataclass
class ManualRef:
    headers: list[str]
    content: str
    similarity: float

@dataclass
class ManualSectionHit:
    headers: list[str]
    relevance: float

@dataclass
class BlockChunk:
    headers: list[str]
    part: int
    code_refs: list[str]
    chunk: str


#: A section's header path as the six h1..h6 columns both backends store: depth beyond the path is
#: None.
type HeaderColumns = tuple[str | None, str | None, str | None, str | None, str | None, str | None]


def header_columns(headers: Sequence[str]) -> HeaderColumns:
    """``headers`` as the six header columns. An empty header counts as absent, and depth beyond
    six is dropped, the same on insert and on lookup, so a path finds what it was stored as."""
    cols = [h or None for h in headers[:6]] + [None] * (6 - min(len(headers), 6))
    return (cols[0], cols[1], cols[2], cols[3], cols[4], cols[5])
