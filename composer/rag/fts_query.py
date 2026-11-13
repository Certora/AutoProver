"""Translate the websearch-style keyword queries the search tools advertise into SQLite FTS5.

The tools describe their query language as Postgres's ``websearch_to_tsquery``: unquoted terms are
ANDed, ``"quoted text"`` is a phrase, ``or`` between two terms makes them alternatives, and a
leading ``-`` excludes a term. Raw FTS5 syntax is far stricter — an unbalanced quote, a bare
``-``, a ``.`` or a ``:`` in an LLM-written query is a syntax error — so a query is never passed
through as-is. Every term is re-emitted as an FTS5 string literal built only from the tokens
FTS5's ``unicode61`` tokenizer would produce, which makes the output valid for any input.
"""

import re
from dataclasses import dataclass

#: Postgres's ``english`` text-search stop words (the Snowball list). ``websearch_to_tsquery``
#: drops these from a query, so ``how are invariants checked`` needs only ``invariants`` and
#: ``checked`` to match; FTS5 has no stop-word list of its own and would require every word.
_STOP_WORDS = frozenset("""
    i me my myself we our ours ourselves you your yours yourself yourselves he him his himself
    she her hers herself it its itself they them their theirs themselves what which who whom
    this that these those am is are was were be been being have has had having do does did
    doing a an the and but if or because as until while of at by for with about against between
    into through during before after above below to from up down in out on off over under again
    further then once here there when where why how all any both each few more most other some
    such no nor not only own same so than too very s t can will just don should now
""".split())

#: What ``unicode61`` keeps as token characters: letters and digits. ``_`` is a separator to it,
#: so ``require_invariant`` indexes as the two tokens ``require`` and ``invariant``.
_TOKEN = re.compile(r"[^\W_]+")

#: One query item: an optionally negated quoted phrase (closing quote optional, as in Postgres),
#: or an optionally negated bare word.
_ITEM = re.compile(r'(-?)"([^"]*)"?|(-?)(\S+)')


@dataclass(frozen=True)
class _Phrase:
    tokens: tuple[str, ...]

    def render(self) -> str:
        return '"' + " ".join(self.tokens) + '"'


def _bare_word(word: str) -> _Phrase | None:
    tokens = tuple(t for t in _TOKEN.findall(word.lower()) if t not in _STOP_WORDS)
    return _Phrase(tokens) if tokens else None


def _quoted(text: str) -> _Phrase | None:
    # Stop words stay in a quoted phrase: FTS5 indexes every word, so the phrase matches the
    # text the author actually quoted.
    tokens = tuple(_TOKEN.findall(text.lower()))
    return _Phrase(tokens) if tokens else None


def to_fts5_query(query: str) -> str | None:
    """The FTS5 ``MATCH`` expression for a websearch-style ``query``, or ``None`` when the query
    has nothing searchable in it (only stop words, punctuation, or exclusions)."""
    groups: list[list[_Phrase]] = []
    excluded: list[_Phrase] = []
    join_next = False
    for m in _ITEM.finditer(query):
        quoted_neg, quoted, bare_neg, bare = m.groups()
        if bare is not None and bare.lower() == "or":
            join_next = bool(groups)
            continue
        if quoted is not None:
            negated, phrase = quoted_neg == "-", _quoted(quoted)
        else:
            negated, phrase = bare_neg == "-", _bare_word(bare)
        if phrase is None:
            continue
        if negated:
            excluded.append(phrase)
        elif join_next:
            groups[-1].append(phrase)
        else:
            groups.append([phrase])
        join_next = False

    if not groups:
        return None
    # FTS5 binds NOT tighter than AND and AND tighter than OR, so each OR group is parenthesized.
    expr = " AND ".join("(" + " OR ".join(p.render() for p in g) + ")" for g in groups)
    return expr + "".join(f" NOT {p.render()}" for p in excluded)
