"""Search over ``cvlr_api_kb`` — the CVLR API, generated from the crates.

The tool names are the point. A retrieval hit carries no provenance — :meth:`find_refs` returns
headers, content and a similarity score, and nothing about where the row came from — so the only
thing telling an agent whether an answer is the crates' own account of themselves or a paragraph
of hand-written prose is **which tool returned it** (``docs/cvlr-api-docs-plan.md`` §4.1). Hence
``cvlr_api_*`` rather than more ``cvlr_*`` tools over a shared corpus.

Three tools for three questions:

* *"what helper does X?"* — natural language over the item docs.
* *"what is the exact signature of X, and which crate defines it?"* — one call, by name. Full-text
  search cannot decide it: Postgres splits an identifier at ``_``, so ``cvlr_assert`` would match
  every ``cvlr_assert_*`` entry. An entry is found by its header leaf, or, for a name only a
  re-export spells (``clog`` for ``cvlr_log``), by full-text candidates whose entry lists that
  path.
* *"does X exist?"* — the closed-world read. A vector search always returns its nearest rows, so
  "nothing came back" cannot distinguish absence from an oddly phrased query. The producer emits
  one section per crate listing its whole public surface, and this is the tool that fetches it.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from langchain_core.tools import BaseTool
from pydantic import Field

from composer.rag.cvlr_api import API_ROOT, ApiPath
from composer.rag.db import ComposerRAGDB
from composer.rag.render import header_path, render_refs
from graphcore.tools.schemas import WithAsyncDependencies

_AUTHORITY = (
    "This corpus is generated from the CVLR crates at the releases this build pins, so it is "
    "authoritative on what exists and what its signature is. Where it disagrees with the CVLR "
    "manual, with a past project, or with recall, it is right and they are stale. Its code "
    "examples are labelled: one 'compiled against the pinned crates' was type-checked against "
    "those releases; one 'not compiled' is a sketch."
)


#: How many rows to ask the database for, per row a tool returns. Rows of another platform
#: generation are dropped after the query (:meth:`ApiCorpus.admits`), and with two generations up to
#: half of them can be.
_OVERFETCH = 3

#: How many full-text candidates :func:`_reexported_as` reads. A short name like ``clog`` occurs in
#: many entries' prose, not only in the one that lists it as a path.
_REEXPORT_CANDIDATES = 50


@dataclass(frozen=True)
class ApiCorpus:
    """The corpus as one run sees it: the releases that run builds.

    The corpus holds every platform generation's crates (``cvlr-solana`` 0.5.x and 0.6.x side by
    side). A row for a crate the run pins on another release line is another generation's, and a
    tool that showed it would describe, with full authority, an API the run's build does not have
    (:meth:`admits`).
    """

    db: ComposerRAGDB
    #: Crate name → the version this run builds. A crate the run does not pin, such as one of the
    #: core's sub-crates, is admitted at any version: the generations share it.
    pinned: Mapping[str, str]

    def admits(self, path: ApiPath) -> bool:
        """A pinned crate's row is admitted from an earlier release of the pin's semver line, not
        only from the pin itself.

        An earlier compatible release describes a subset of what the build has, so the corpus need
        not be re-seeded for a patch release; :meth:`patch_gap` is how a tool says so. A later
        release may describe items the build lacks, and is refused like another line."""
        pin = self.pinned.get(path.crate)
        if pin is None or path.version == pin:
            return True
        row, pinned = _semver(path.version), _semver(pin)
        if row is None or pinned is None:
            return False
        return _release_line(row) == _release_line(pinned) and row <= pinned

    def patch_gap(self, path: ApiPath) -> str | None:
        """The pinned version, when ``path`` is from an earlier release than the pin."""
        pin = self.pinned.get(path.crate)
        return pin if pin is not None and pin != path.version else None

    def newest(self, paths: Iterable[ApiPath]) -> set[ApiPath]:
        """Of the admitted ``paths``, the newest release's row for each crate and leaf.

        The corpus may hold both the pin and an earlier patch release of it, and the earlier one
        describes less."""
        best: dict[tuple[str, str], ApiPath] = {}
        for path in paths:
            if not self.admits(path):
                continue
            key = (path.crate, path.leaf)
            prior = best.get(key)
            if prior is None or _recency(path) > _recency(prior):
                best[key] = path
        return set(best.values())

    async def rows(self) -> set[ApiPath]:
        """Every row this run is shown, at its newest admitted release."""
        listed = await self.db.manual_headers([API_ROOT])
        return self.newest(p for h in listed if (p := ApiPath.parse(h)) is not None)


def _semver(version: str) -> tuple[int, int, int] | None:
    """``major.minor.patch``, or ``None`` for anything else (a pre-release, build metadata)."""
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    major, minor, patch = (int(p) for p in parts)
    return major, minor, patch


def _recency(path: ApiPath) -> tuple[int, int, int]:
    """Ordering among admitted rows of one crate. Only a pinned version can fail to parse, and
    then it is the only admitted one."""
    return _semver(path.version) or (0, 0, 0)


def _release_line(version: tuple[int, int, int]) -> tuple[int, ...]:
    """Cargo's caret compatibility: the leading components up to and including the first
    non-zero one. ``0.5.2`` is on ``0.5``, ``1.4.0`` on ``1``, ``0.0.3`` only on itself."""
    for i, part in enumerate(version):
        if part != 0:
            return version[: i + 1]
    return version


def _patch_note(corpus: ApiCorpus, path: ApiPath) -> str:
    pin = corpus.patch_gap(path)
    if pin is None:
        return ""
    return (
        f"\n\n(This is `{path.crate}` {path.version}. This build pins {pin}, a later patch "
        f"release on the same line, which may add items this does not describe.)"
    )


class CvlrApiSearch(WithAsyncDependencies[str, ApiCorpus]):
    __doc__ = (
        "Search the generated CVLR API reference with a natural-language question. Use it when "
        "you know what you want to *do* but not what it is called — 'how do I give an account "
        "field a nondeterministic value?', 'what gives me an unbounded integer?'.\n\n"
        f"{_AUTHORITY}\n\n"
        "If you already have an identifier, use `cvlr_api_lookup` instead; it returns the "
        "signature in one call. To ask whether something exists at all, use `cvlr_api_surface` — "
        "this tool always returns its nearest matches, so an unhelpful answer here is not "
        "evidence of absence."
    )

    query: str = Field(description="A single natural-language question about the CVLR API.")
    similarity_cutoff: float = Field(
        default=0.5, description="Minimum cosine similarity threshold for results."
    )
    max_results: int = Field(default=10, description="Maximum number of results to return.")

    async def run(self) -> str:
        with self.tool_deps() as corpus:
            refs = await corpus.db.find_refs(
                self.query,
                similarity_cutoff=self.similarity_cutoff,
                top_k=self.max_results * _OVERFETCH,
            )
            parsed = [(r, ApiPath.parse(r.headers)) for r in refs]
            shown = corpus.newest(p for _, p in parsed if p is not None)
            res = [r for r, p in parsed if p in shown][: self.max_results]
            if not res:
                return (
                    "(No results found. This does not mean the item does not exist — vector "
                    "search returns nearest matches, so rephrase, or call `cvlr_api_surface` for "
                    "the crate to settle existence.)"
                )
            return render_refs(res)


class CvlrApiLookup(WithAsyncDependencies[str, ApiCorpus]):
    __doc__ = (
        "Look up one CVLR item by name and get its entry in full: the signature, the crate that "
        "*defines* it, its feature gate, and whatever the crate documents about it. Use this "
        "whenever you have an identifier — a macro (`cvlr_assert_le`, `clog`), a function "
        "(`nondet`, `cvlr_deserialize_nondet_accounts`), a trait, a derive, a type, or a method "
        "(`NativeIntU64::u64_max`, or `u64_max` alone).\n\n"
        f"{_AUTHORITY}\n\n"
        "The defining crate is what you get back, not the facade path: `cvlr` re-exports almost "
        "everything, so the item you reach as `cvlr::x::y` usually lives in a sibling crate. "
        "Write what compiles, which is whichever path the entry gives."
    )

    name: str = Field(description=(
        "The exact identifier, without a `!` and without a module path — `cvlr_assert_le`, not "
        "`cvlr::cvlr_assert_le!`. A method may be qualified by its type."
    ))
    limit: int = Field(default=5, description="How many matching entries to return at most.")

    async def run(self) -> str:
        with self.tool_deps() as corpus:
            name = self.name.removesuffix("!")
            rows = await corpus.rows()
            matches = sorted(
                (p for p in rows if not p.is_surface and p.names(name)),
                key=lambda p: (p.leaf != name, p.crate, p.leaf),
            )[: self.limit]
            entries = [
                (path, content)
                for path in matches
                if (content := await corpus.db.get_manual_section(path.headers())) is not None
            ]
            if not matches:
                entries = await _reexported_as(corpus, rows, name, self.limit)
            if not matches and not entries:
                return (
                    f"No entry in the generated CVLR API reference is named {self.name!r}. Check "
                    f"the spelling, then call `cvlr_api_surface` for the crate you expect it in — "
                    f"that section lists the whole public surface and settles whether it exists."
                )
            if not entries:
                return (
                    f"Matched {len(matches)} entries for {self.name!r} but none could be read "
                    f"back; the corpus may be mid-rebuild."
                )
            return "\n".join(
                f"----\n{header_path(path.headers())}\n\n{content}{_patch_note(corpus, path)}"
                for path, content in entries
            )


async def _reexported_as(
    corpus: ApiCorpus, rows: set[ApiPath], name: str, limit: int
) -> list[tuple[ApiPath, str]]:
    """Entries that list a path ending in ``name``: a re-export under another name.

    Full text proposes the candidates, and only an entry whose own text spells the path is kept."""
    found: list[tuple[ApiPath, str]] = []
    for hit in await corpus.db.search_manual_keywords(name, limit=_REEXPORT_CANDIDATES):
        path = ApiPath.parse(hit.headers)
        if path is None or path.is_surface or path not in rows:
            continue
        content = await corpus.db.get_manual_section(path.headers())
        if content is not None and f"::{name}`" in content:
            found.append((path, content))
            if len(found) == limit:
                break
    return found


class CvlrApiSurface(WithAsyncDependencies[str, ApiCorpus]):
    __doc__ = (
        "List a CVLR crate's **complete** public surface — every item it exports, at the release "
        "this build pins or an earlier patch release of it, which the answer names.\n\n"
        "This is the tool that answers *does X exist?*, and it is the only one that can: it "
        "returns a closed list, so a name that is absent from it is a name you do not have. Do "
        "not infer absence from an empty search, and do not write a helper you could not find "
        "here.\n\n"
        f"{_AUTHORITY}"
    )

    crate: str = Field(description=(
        "The crate name as cargo spells it — `cvlr-asserts`, `cvlr-solana`, `cvlr-mathint`. Pass "
        "the crate that *defines* the item if you know it; the facade `cvlr` lists only what it "
        "defines itself, which is almost nothing."
    ))

    async def run(self) -> str:
        with self.tool_deps() as corpus:
            surface = next(
                (p for p in await corpus.rows() if p.is_surface and p.crate == self.crate), None
            )
            content = (
                await corpus.db.get_manual_section(surface.headers()) if surface is not None
                else None
            )
            if surface is None or content is None:
                return (
                    f"No public-surface listing for a crate named {self.crate!r}. Either the name "
                    f"is misspelled or this build does not pin that crate — in which case you "
                    f"cannot use it, whatever it offers."
                )
            return f"{header_path(surface.headers())}\n\n{content}{_patch_note(corpus, surface)}"


def get_tools(db: ComposerRAGDB, pinned: Mapping[str, str]) -> Iterable[BaseTool]:
    """The three tools over ``db``, showing only the releases in ``pinned`` (:class:`ApiCorpus`)."""
    corpus = ApiCorpus(db, pinned)
    return [
        CvlrApiSearch.bind(corpus).as_tool("cvlr_api_search"),
        CvlrApiLookup.bind(corpus).as_tool("cvlr_api_lookup"),
        CvlrApiSurface.bind(corpus).as_tool("cvlr_api_surface"),
    ]
