"""Translate the websearch-style keyword queries the search tools advertise into SQLite FTS5.

The tools describe their query language as Postgres's ``websearch_to_tsquery``, whose grammar is: a
query is a list of alternatives separated by ``or``; an alternative is a list of terms, all
required; a term is a word or a ``"quoted phrase"``, and a leading ``-`` makes it excluded instead
of required. So ``and`` binds tighter than ``or``: ``ghost hook or storage -mapping`` is
``(ghost AND hook) OR (storage AND NOT mapping)``.

Raw FTS5 syntax is far stricter — an unbalanced quote, a bare ``-``, a ``.`` or a ``:`` in an
LLM-written query is a syntax error — so a query is never passed through as-is. Every term is
re-emitted as an FTS5 string literal built only from the tokens FTS5's ``unicode61`` tokenizer
would produce, which makes the output valid for any input.

One construct has no FTS5 spelling: an alternative made only of exclusions (``storage or -hook``,
Postgres's ``storage | !hook``). FTS5's ``NOT`` needs a left operand, so such an alternative is
dropped, and the query matches what its other alternatives match. In Postgres that alternative
matches nearly every section, so a search using it was not selective to begin with.
"""

import re
from dataclasses import dataclass, field

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


@dataclass
class _Alternative:
    required: list[_Phrase] = field(default_factory=list)
    excluded: list[_Phrase] = field(default_factory=list)

    def render(self) -> str:
        # FTS5's NOT is binary and binds tighter than AND, so the required terms are grouped
        # and each exclusion applies to that group.
        expr = "(" + " AND ".join(p.render() for p in self.required) + ")"
        return expr + "".join(f" NOT {p.render()}" for p in self.excluded)


def _phrase(text: str) -> _Phrase | None:
    """The phrase for one term, or ``None`` if it has no searchable token. A single word that is a
    stop word is dropped, as Postgres drops it; a quoted phrase keeps its stop words, since FTS5
    indexes every word and the phrase then matches the text that was quoted."""
    tokens = tuple(_TOKEN.findall(text.lower()))
    if all(t in _STOP_WORDS for t in tokens):
        return None
    return _Phrase(tokens)


def _alternatives(query: str) -> list[_Alternative]:
    alternatives = [_Alternative()]
    for m in _ITEM.finditer(query):
        quoted_neg, quoted, bare_neg, bare = m.groups()
        if bare is not None and bare_neg == "" and bare.lower() == "or":
            alternatives.append(_Alternative())
            continue
        if quoted is not None:
            negated, phrase = quoted_neg == "-", _phrase(quoted)
        else:
            negated, phrase = bare_neg == "-", _phrase(bare)
        if phrase is None:
            continue
        if negated:
            alternatives[-1].excluded.append(phrase)
        else:
            alternatives[-1].required.append(phrase)
    return alternatives


def to_fts5_query(query: str) -> str | None:
    """The FTS5 ``MATCH`` expression for a websearch-style ``query``, or ``None`` when the query
    has nothing searchable in it (only stop words, punctuation, or exclusions)."""
    usable = [a for a in _alternatives(query) if a.required]
    if not usable:
        return None
    if len(usable) == 1:
        return usable[0].render()
    return " OR ".join(f"({a.render()})" for a in usable)
