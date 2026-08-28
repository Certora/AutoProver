"""Producer: the CVLR crate sources → a ``cvlr_kb`` RAG manifest.

The second half of the public CVLR corpus (``docs/cvlr-backend-plan.md`` §5.4 item 2). The published
manual covers the library's *methodology* well and its *surface* thinly — the capture survey measured
``cvlr-solana`` at 4 of 28 functions and 0 of 7 macros named, ``cvlr-log`` at 5 of 35 — and the gap
is exactly where an authoring agent invents a helper. This script closes it from the one source that
cannot be out of date: the crates themselves, at the versions
:mod:`composer.spec.cvlr_reference` pins.

**Two gates, both properties rather than plausibilities.** The lesson the capture pass paid for is
that generated corpus content needs a check that can fail for a reason nobody had to notice:

* **Compile.** Every emitted example goes through :class:`~composer.spec.cvlr.probe.ReferenceProbe`
  *inside* the retry loop, so the compiler's own message is the next attempt's input. Placing this
  after generation instead of inside it is what left the capture pass at 4 of 48 examples compiling.
* **Completeness.** Every public item :mod:`composer.spec.cvlr.inventory` finds in a module must be
  named by some entry for that module. This is what makes "grouped" coverage honest: one entry may
  legitimately cover twenty mechanical variants, and the gate demands it *name* the twenty rather
  than quietly documenting three of them.

**Grouping is the model's judgement, bounded by the gate.** 310 public items would make 310 entries,
most of them "the ``Add`` impl for ``NativeIntU64``" — a corpus that answers a question nobody asks
while burying the ones people do. So a module is documented as a handful of entries, one per
*distinct idea*, and the completeness gate is what stops that becoming an excuse to skip things.

**Macro expansions are quoted, not described.** The crates ship 58 ``macrotest`` snapshot pairs — an
invocation beside its expansion. "What exactly does this macro expand to" is the question §5.4 says
a corpus is the wrong tool for, and here the crate answers it exactly, in the pinned version. An
agent asked to describe those expansions would be inferring what is sitting on disk.

Run it as::

    source ~/.autoProverAnthropicApiKey.sh && \\
      uv run --no-sync python -m composer.scripts.cvlr_crate_reference --out scripts/cvlr-crates

``--dry-run`` prints the module plan and the item counts without calling a model, which is the
reviewable step before any tokens are spent.
"""

import argparse
import asyncio
import dataclasses
import json
import logging
import pathlib
import re
from collections import defaultdict
from collections.abc import Sequence

from pydantic import BaseModel, Field, model_validator

from composer.cargo.metadata import read_workspace
from composer.diagnostics.budget import token_cost_budget
from composer.input.types import ModelConfiguration
from composer.llm.registry import get_provider_for
from composer.rag.import_format import (
    EmbeddedBlock,
    EmbeddedBlockKind,
    EmbeddedGroup,
    ManualBlock,
    ManualBlockKind,
    ManualSection,
    RagManifest,
)
from composer.spec.cvlr.crates import resolve
from composer.spec.cvlr_reference import ChainReference, reference_for
from composer.spec.cvlr.inventory import ExpansionPair, Item, expansion_pairs, inventory, uncovered
from composer.spec.cvlr.probe import Compiles, ReferenceProbe
from composer.spec.cvlr.source_tools import MountedCrates, mount

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

#: The corpus tag both CVLR manifests share.
KNOWLEDGE_BASE = "cvlr_kb"

#: The top of every header path this producer emits. Distinct from the docs producer's ``solana >
#: The Certora Solana Prover > …`` so a reader can tell a reference entry from a manual section, and
#: from the private half's ``CVLR practice`` for the same reason.
CORPUS_ROOT = "CVLR reference"

#: Where the quoted expansion pairs live.
EXPANSION_SECTION = "Macro expansions"

#: Attempts per module. Each retry carries the previous attempt's gate failures, so the third is
#: working from two rounds of specific complaints rather than trying again blind.
MAX_ATTEMPTS = 3

#: Modules generated at once. Bounded because each attempt ends in a ``cargo check`` and the probe
#: serializes those anyway — more concurrency here just queues on the lock.
CONCURRENCY = 4

#: A module with more items than this is documented in two passes rather than one overlong call.
#: ``cvlr-mathint``'s operator modules are the only ones that reach it.
MAX_ITEMS_PER_CALL = 40


def _first_json_value(text: str) -> object | None:
    """The first complete JSON value in ``text``, ignoring anything after it.

    ``json.loads`` refuses the whole string when a complete value is followed by trailing content —
    "Extra data at N" — which is the shape a model's stringified array arrives in often enough to
    lose modules over. ``raw_decode`` reads one value and stops, which is what was wanted."""
    try:
        value, _ = json.JSONDecoder().raw_decode(text.strip())
    except json.JSONDecodeError:
        return None
    return value


class ReferenceEntry(BaseModel):
    """One documented idea from one module."""

    title: str = Field(description="A short noun phrase naming what this entry is about.")
    symbols: list[str] = Field(
        description="Every public item this entry documents, spelled exactly as a caller writes it."
    )
    summary: str = Field(
        description="What it is and when to reach for it, in two or three sentences. State what it "
        "does to the verification problem, not just what the Rust does."
    )
    signature: str = Field(
        description="The exact form a caller writes: a function signature, or a macro invocation "
        "with its argument shapes. Copy it from the source rather than paraphrasing."
    )
    example: str = Field(
        description="A short Rust snippet that uses it and compiles against the reference set. "
        "Prefer a fragment that reads as part of a rule over a whole file."
    )
    notes: str = Field(
        default="",
        description="Only what a caller would get wrong without being told: a soundness "
        "consequence, an interaction with another macro, a name that looks like a sibling but is "
        "not. Empty when there is nothing.",
    )

    def text(self) -> str:
        """Everything the completeness gate reads. Ordered as the section will render."""
        return "\n".join(
            [self.title, " ".join(self.symbols), self.summary, self.signature, self.example, self.notes]
        )


class ModuleEntries(BaseModel):
    """The structured reply for one module."""

    entries: list[ReferenceEntry]

    @model_validator(mode="before")
    @classmethod
    def _accept_json_text(cls, value: object) -> object:
        """Take the entry list however deeply it arrives wrapped.

        A schema declaring an array and a model sending a string for it is a failure this codebase
        has already paid for once — the capture pass shipped 12,275 single-character corpus blocks
        because a string was iterated where a list was expected. Here it shows up as the *whole
        object* stuffed into the array field, sometimes more than one layer deep, so unwrapping
        repeats rather than happening once and the loop stops at the first list it finds.

        Written as tolerance rather than as a shape-by-shape patch on purpose: each malformation
        looks different and chasing them individually costs a model call each time. What still
        cannot be coerced is retried with the raw arguments logged, so this is the shortcut and the
        retry is the safety net."""
        for _ in range(6):
            if isinstance(value, str):
                parsed = _first_json_value(value)
                if parsed is None:
                    return value
                value = parsed
            elif isinstance(value, list):
                return {"entries": value}
            elif isinstance(value, dict):
                if isinstance(value.get("entries"), list):
                    return value
                if "entries" in value:
                    value = value["entries"]
                elif "title" in value:
                    # A single entry sent where the wrapper was asked for.
                    return {"entries": [value]}
                else:
                    return value
            else:
                return value
        return value


@dataclasses.dataclass(frozen=True)
class Module:
    """One source file's worth of items — the unit a single generation call covers."""

    crate: str
    path: str
    items: tuple[Item, ...]
    source: str
    expansions: tuple[ExpansionPair, ...]
    #: The facade crate's ``lib.rs`` — the map from "defined in this sub-crate" to "callable as
    #: ``cvlr::…``". Carried on every module because a module documented in isolation cannot know
    #: its own public spelling: ``nondet_option`` is defined in ``cvlr-nondet`` and reached as
    #: ``cvlr::nondet::nondet_option``, and nothing in the defining file says so. Empty for the
    #: facade's own module, where it *is* the source.
    facade: str = ""
    #: The *defining* crate's own ``lib.rs``, when this module is not it. The second half of the
    #: same question: the facade says how a caller reaches the crate, and this says which of the
    #: crate's modules are reachable at all and under what name — ``pub mod havoc`` sits behind
    #: ``#[cfg(feature = "std")]`` and ``nondet_option`` is re-exported to the crate root while its
    #: siblings are not. Neither fact is visible from inside the file that defines them.
    crate_root: str = ""

    @property
    def label(self) -> str:
        return self.path


@dataclasses.dataclass
class ScriptOptions:
    """A :class:`~composer.input.types.ModelConfiguration` for a producer script.

    Thinking off: the work is transcription and judgement over source that is fully in the prompt,
    which is not what an extended thinking budget buys. Memory and interleaved thinking are for
    tool-using agents, and this makes one structured call per module."""

    tokens: int = 16_000
    thinking_tokens: int | None = None
    memory_tool: bool = False
    interleaved_thinking: bool = False


_SYSTEM_TEMPLATE = """\
You are writing the API reference for CVLR, the Rust specification library used with the Certora
Solana Prover. Your readers are two: a verification engineer looking a helper up, and an LLM agent
about to write a rule that uses it. Both fail the same way — by reaching for a helper that does not
exist, or by using a real one with the wrong shape — so accuracy about *form* matters more here than
prose quality.

You are given one module's complete source. Everything you write must be derivable from it. Do not
describe behaviour the source does not show, and do not invent a helper you did not read.

Group by idea, not by symbol. A module exporting twenty mechanical variants of one operation is one
entry that names all twenty in `symbols` and shows one of them in `example` — not twenty entries. A
module exporting three unrelated things is three entries. Every public item you are given must
appear in some entry's `symbols`, because a reader searching for it needs to land somewhere.

Examples must compile against the pinned reference set. They are checked, and you will be told what
the compiler said. Prefer the smallest fragment that shows real use; a fragment of statements is
fine — it will be wrapped.

**Import only these crates: {importable}.** A project under verification depends on those and gets
everything else through them, so an example that writes `use cvlr_asserts::…` or
`use cvlr_early_panic::…` teaches an import path no real target has — even when you are documenting
that very crate. Reach the item through `cvlr` (`use cvlr::prelude::*;` covers most of it). The
expansion snapshots below import the defining crate directly because they are *its own tests*; that
import line is test setup, not something to copy.

Write `notes` only when a caller would get something wrong without being told. An empty `notes` is
better than a restatement of `summary`."""


def _system_prompt(reference: ChainReference) -> str:
    """The system prompt, with the importable crate list filled in from the reference set.

    Derived rather than written down, because the two must agree: the probe crate declares exactly
    these dependencies, so a list stated by hand would eventually name a crate the gate rejects — and
    the model would spend every retry rediscovering that."""
    importable = ", ".join(
        f"`{c.name.replace('-', '_')}`" for c in (*reference.crates(), *reference.platform.crates)
    )
    return _SYSTEM_TEMPLATE.format(importable=importable)


def _raw_arguments(raw: object) -> str:
    """What the tool arguments actually were, in the one line a log can hold.

    Says whether a string-valued ``entries`` is *parseable* rather than showing its first 200
    characters, because the two failures it distinguishes have different fixes and look identical
    from the front: a well-formed array sent as a string is a coercion gap, and a truncated one is
    an output-budget problem."""
    message = raw.get("raw") if isinstance(raw, dict) else None
    stop = getattr(message, "response_metadata", {}).get("stop_reason", "?")
    calls = getattr(message, "tool_calls", None) or []
    if not calls:
        return f"stop={stop} (no tool call)"
    args = calls[0].get("args")
    if isinstance(args, dict) and isinstance(args.get("entries"), str):
        body = args["entries"]
        try:
            json.loads(body)
            shape = "entries is a well-formed JSON string"
        except json.JSONDecodeError as exc:
            shape = f"entries is a TRUNCATED/invalid JSON string ({exc.msg} at {exc.pos})"
        return f"stop={stop} len={len(body)} {shape}"
    return f"stop={stop} args={repr(args)[:160]}"


def _module_prompt(module: Module, versions: str) -> str:
    items = "\n".join(
        f"- {i.qualified} ({i.kind})" + (f" [line {i.line}]" if i.line else "")
        for i in module.items
    )
    expansions = "\n\n".join(
        f"### {p.name}\n\nInvocation:\n```rust\n{p.invocation}\n```\n"
        f"Expands to:\n```rust\n{p.expansion}\n```"
        for p in module.expansions
    )
    parts = [
        f"Reference set: {versions}",
        f"Module: `{module.path}`",
        "",
        "Public items you must cover (every one must appear in some entry's `symbols`):",
        items,
        "",
        "Source:",
        f"```rust\n{module.source}\n```",
    ]
    if module.crate_root:
        parts += [
            "",
            f"The defining crate's own `lib.rs`. It says which modules are public and under what "
            f"name, and which are behind a `cfg` — an item in a module that is not `pub`, or is "
            f"gated off, is not callable however public the item itself looks:",
            f"```rust\n{module.crate_root}\n```",
        ]
    if module.facade:
        parts += [
            "",
            "How a caller reaches these items. This is the facade crate's `lib.rs`, which is the "
            "only place the public path is written down — an item defined here may be re-exported "
            "under a different name, may sit behind a module, or may not be in the prelude at all. "
            "Every path in your `signature` and `example` must be justified by these two files:",
            f"```rust\n{module.facade}\n```",
        ]
    if expansions:
        parts += [
            "",
            "The crate ships these macro-expansion snapshots for this module. They are exact — use "
            "them where an entry needs to say what a macro expands to, rather than inferring it:",
            expansions,
        ]
    return "\n".join(parts)


def _gate_complaint(
    missing: Sequence[str], broken: Sequence[tuple[str, str]]
) -> str:
    """The retry prompt: what failed, and nothing else.

    Two failures with opposite fixes, so they are reported apart. A missing symbol wants a *changed
    entry* (or a name added to one); a broken example wants *changed Rust* in the entry that has it.
    """
    parts: list[str] = ["Your previous answer did not pass. Fix exactly these and resubmit."]
    if missing:
        parts += [
            "",
            "These public items appear in no entry's `symbols`. A reader searching for them lands "
            "nowhere. Add them to the entry they belong to, or add an entry:",
            *(f"- {name}" for name in missing),
        ]
    for title, diagnostics in broken:
        parts += [
            "",
            f"The example for {title!r} does not compile. The compiler said:",
            "```",
            diagnostics.strip()[-2500:],
            "```",
        ]
    return "\n".join(parts)


async def _generate_module(
    llm, module: Module, probe: ReferenceProbe, versions: str, system: str
) -> list[ReferenceEntry]:
    """One module's entries, retried against both gates until they pass or attempts run out.

    The retry grows **one user turn** rather than appending an assistant turn and a follow-up.
    ``with_structured_output`` makes the model answer with a tool call, so a reconstructed assistant
    message would be plain text where a ``tool_use`` block belongs — a conversation the API cannot
    continue, and one whose symptom is that attempt 1 parses and every retry does not. Folding the
    previous answer and the complaint into the next user message keeps the shape valid and leaves the
    system prompt at a stable cacheable prefix.
    """
    # ``include_raw`` so an unparseable reply is a *diagnosable* event rather than a mystery: the
    # coercion above was written blind twice before the raw tool arguments were in the log, and each
    # guess cost a round of calls to disprove.
    structured = llm.with_structured_output(ModuleEntries, include_raw=True)
    prompt = _module_prompt(module, versions)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        raw = await structured.ainvoke(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        )
        reply = raw.get("parsed") if isinstance(raw, dict) else raw
        if not isinstance(reply, ModuleEntries):
            # A reply that does not fit the schema is a gate failure like any other, not a lost
            # module: it is exactly the case a retry is for.
            error = raw.get("parsing_error") if isinstance(raw, dict) else None
            logger.info(
                "%s: attempt %d did not parse (%s) args=%s",
                module.label,
                attempt,
                str(error).splitlines()[0] if error else "no parsed value",
                _raw_arguments(raw),
            )
            if attempt == MAX_ATTEMPTS:
                logger.warning("%s: giving up — reply never parsed", module.label)
                return []
            prompt = (
                f"{prompt}\n\n---\n\nYour previous reply did not match the required schema and was "
                f"rejected:\n{error}\n\nCall the tool with `entries` as a JSON **array of "
                f"objects**, not as a string and not wrapped in another object."
            )
            continue

        entries = list(reply.entries)
        if not entries:
            logger.warning("%s: attempt %d produced no entries", module.label, attempt)
            return []

        broken: list[tuple[str, str]] = []
        for entry in entries:
            verdict = await probe.check(entry.example)
            if not isinstance(verdict, Compiles):
                broken.append((entry.title, verdict.diagnostics))

        covered = "\n".join(e.text() for e in entries)
        missing = uncovered(module.items, covered)

        if not broken and not missing:
            logger.info(
                "%s: %d entr%s on attempt %d",
                module.label,
                len(entries),
                "y" if len(entries) == 1 else "ies",
                attempt,
            )
            return entries

        logger.info(
            "%s: attempt %d — %d uncompiled example(s), %d uncovered item(s)",
            module.label,
            attempt,
            len(broken),
            len(missing),
        )
        if attempt == MAX_ATTEMPTS:
            # Ship what passed rather than nothing: an entry whose example compiles is usable even
            # when a sibling's does not, and the manifest records which examples were gated.
            kept = [e for e in entries if e.title not in {t for t, _ in broken}]
            logger.warning(
                "%s: giving up after %d attempts, keeping %d of %d entries; uncovered: %s",
                module.label,
                MAX_ATTEMPTS,
                len(kept),
                len(entries),
                ", ".join(missing) or "none",
            )
            return kept
        # The retry does not echo the whole previous reply. Everything that passed is already
        # derivable from the module source that is still in the prompt, and re-sending it grows the
        # turn by the one thing the model does not need to reconsider — while an entry whose example
        # failed *does* need its own text back to fix it.
        prompt = (
            f"{prompt}\n\n---\n\n{_gate_complaint(missing, broken)}\n\n"
            f"For reference, {'these entries' if len(broken) != 1 else 'this entry'} produced the "
            f"failing example(s):\n"
            + "\n".join(
                f"- {e.title}:\n```rust\n{e.example}\n```"
                for e in entries
                if e.title in {t for t, _ in broken}
            )
        )
    return []


def _invokes_any(pair: ExpansionPair, items: Sequence[Item]) -> bool:
    """Whether ``pair``'s invocation actually calls one of ``items``."""
    called = set(re.findall(r"\b(\w+)\s*!", pair.invocation))
    for attribute in re.finditer(r"#\[(?P<body>[^\]]*)\]", pair.invocation):
        # An attribute reaches an item two ways: as the attribute itself (``#[cvlr::rule]``) and as a
        # name inside a derive list (``#[derive(Nondet, CvlrLog)]``). Both are "this snapshot
        # exercises that item", and taking only the outer name loses every derive.
        called |= set(re.findall(r"\w+", attribute.group("body")))
    return any(i.name in called for i in items)


def facade_source(crates: MountedCrates, core: str) -> tuple[str, str]:
    """``(path, source)`` of the facade crate's ``lib.rs`` — the public-path map."""
    wanted = next((p for p in crates.paths() if p.startswith(f"{core}-") and p.endswith("/src/lib.rs")), None)
    return (wanted, crates.read(wanted) or "") if wanted else ("", "")


def plan_modules(crates: MountedCrates, *, core: str = "cvlr") -> list[Module]:
    """One module per source file that exports anything, largest first.

    Largest first so the modules most likely to need every retry start earliest, and a run that is
    interrupted has done the expensive ones."""
    facade_path, facade = facade_source(crates, core)
    roots = {
        p.split("/", 1)[0]: crates.read(p) or ""
        for p in crates.paths()
        if p.endswith("/src/lib.rs")
    }
    items = inventory(crates)
    by_path: dict[tuple[str, str], list[Item]] = defaultdict(list)
    for item in items:
        by_path[(item.crate, item.path)].append(item)

    pairs_by_crate: dict[str, list[ExpansionPair]] = defaultdict(list)
    for pair in expansion_pairs(crates):
        pairs_by_crate[pair.crate].append(pair)

    modules: list[Module] = []
    for (crate, path), module_items in by_path.items():
        source = crates.read(path)
        if source is None:
            continue
        for start in range(0, len(module_items), MAX_ITEMS_PER_CALL):
            chunk = module_items[start : start + MAX_ITEMS_PER_CALL]
            modules.append(
                Module(
                    crate=crate,
                    path=path,
                    items=tuple(chunk),
                    source=source,
                    # Expansion snapshots are per-crate, not per-file, and are only worth sending
                    # to the module defining the macro they exercise. Matched on word boundaries:
                    # a substring test sends every ``log`` snapshot to every module that exports
                    # anything containing "log", which is most of ``cvlr-log``.
                    expansions=tuple(
                        p
                        for p in pairs_by_crate.get(crate, ())
                        if _invokes_any(p, chunk)
                    ),
                    facade="" if path == facade_path else facade,
                    crate_root="" if path.endswith("/src/lib.rs") else roots.get(crate, ""),
                )
            )
    return sorted(modules, key=lambda m: -len(m.items))


def _entry_section(module: Module, entry: ReferenceEntry) -> ManualSection:
    body = [
        ManualBlock(kind=ManualBlockKind.TEXT, body=f"**Covers.** {', '.join(entry.symbols)}"),
        ManualBlock(kind=ManualBlockKind.TEXT, body=entry.summary),
        ManualBlock(kind=ManualBlockKind.TEXT, body=f"**Form.** {entry.signature}"),
        ManualBlock(kind=ManualBlockKind.CODE, body=entry.example),
    ]
    if entry.notes.strip():
        body.append(ManualBlock(kind=ManualBlockKind.TEXT, body=f"**Note.** {entry.notes}"))
    body.append(
        ManualBlock(
            kind=ManualBlockKind.TEXT,
            body=f"Defined in `{module.path}`.",
        )
    )
    return ManualSection(headers=[CORPUS_ROOT, module.crate, entry.title], blocks=body)


def _entry_group(module: Module, entry: ReferenceEntry) -> EmbeddedGroup:
    blocks = [
        EmbeddedBlock(
            kind=EmbeddedBlockKind.PARAGRAPH,
            # The symbol list joins the prose block rather than standing alone: a vector index over
            # a bare list of identifiers retrieves for everything and means nothing.
            body=f"{entry.summary} Covers {', '.join(entry.symbols)}. Form: {entry.signature}",
        ),
        EmbeddedBlock(kind=EmbeddedBlockKind.CODE, body=entry.example),
    ]
    if entry.notes.strip():
        blocks.append(EmbeddedBlock(kind=EmbeddedBlockKind.PARAGRAPH, body=entry.notes))
    return EmbeddedGroup(headers=[CORPUS_ROOT, module.crate, entry.title], blocks=blocks)


def _expansion_section(pair: ExpansionPair) -> ManualSection:
    return ManualSection(
        headers=[CORPUS_ROOT, EXPANSION_SECTION, f"{pair.crate}: {pair.name}"],
        blocks=[
            ManualBlock(
                kind=ManualBlockKind.TEXT,
                body=(
                    "A macro-expansion snapshot shipped by the crate itself, at the pinned version. "
                    "This is what the macro expands to, not a description of it."
                ),
            ),
            ManualBlock(kind=ManualBlockKind.CODE, body=pair.invocation),
            ManualBlock(kind=ManualBlockKind.TEXT, body="expands to:"),
            ManualBlock(kind=ManualBlockKind.CODE, body=pair.expansion),
        ],
    )


def build_manifest(
    generated: Sequence[tuple[Module, ReferenceEntry]],
    pairs: Sequence[ExpansionPair],
    *,
    source: str,
) -> RagManifest:
    return RagManifest(
        knowledge_base=KNOWLEDGE_BASE,
        source=source,
        manual_sections=[_entry_section(m, e) for m, e in generated]
        + [_expansion_section(p) for p in pairs],
        embedded_groups=[_entry_group(m, e) for m, e in generated],
    )


async def _run(args: argparse.Namespace) -> int:
    workdir = pathlib.Path(args.workdir).expanduser().resolve()
    probe = await ReferenceProbe.create(workdir, chain=args.chain)
    workspace = await read_workspace(workdir, offline=True)
    if workspace is None:
        logger.error("could not read the probe crate at %s", workdir)
        return 1
    sources = resolve(workspace)
    crates = mount(sources)
    if crates is None:
        logger.error("the probe crate resolved no CVLR sources")
        return 1

    versions = ", ".join(f"{c.name} {c.version}" for c in sources.crates)
    modules = plan_modules(crates, core=reference_for(args.chain).core.name)
    pairs = expansion_pairs(crates)
    if args.crate:
        modules = [m for m in modules if any(m.crate.startswith(c) for c in args.crate)]
        pairs = [p for p in pairs if any(p.crate.startswith(c) for c in args.crate)]

    total_items = sum(len(m.items) for m in modules)
    logger.info(
        "%d module(s), %d item(s), %d expansion snapshot(s) — %s",
        len(modules),
        total_items,
        len(pairs),
        versions,
    )
    if args.dry_run:
        for module in modules:
            logger.info(
                "  %-46s %3d items %2d snapshots",
                module.label,
                len(module.items),
                len(module.expansions),
            )
        return 0

    provider = get_provider_for(model_name=args.model, options=ScriptOptions())
    llm = provider.builder_for()
    system = _system_prompt(reference_for(args.chain))
    limiter = asyncio.Semaphore(CONCURRENCY)

    async def one(module: Module) -> list[tuple[Module, ReferenceEntry]]:
        async with limiter:
            return [
                (module, e)
                for e in await _generate_module(llm, module, probe, versions, system)
            ]

    with token_cost_budget(args.budget) as spend:
        settled = await asyncio.gather(
            *(one(m) for m in modules), return_exceptions=True
        )

    generated: list[tuple[Module, ReferenceEntry]] = []
    for module, outcome in zip(modules, settled):
        if isinstance(outcome, BaseException):
            logger.error("%s raised, skipping: %r", module.label, outcome)
            continue
        generated.extend(outcome)

    manifest = build_manifest(
        generated, pairs, source=f"Certora CVLR crate sources — {versions}"
    )
    out = pathlib.Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    target = out / "cvlr-crates.rag.json"
    target.write_text(json.dumps(manifest.model_dump(mode="json"), indent=2) + "\n")

    covered = "\n".join(e.text() for _, e in generated)
    # Against what this run was *asked* to cover, not the whole library: a ``--crate`` run that
    # reported the other thirteen crates as holes would make the gate's output unreadable exactly
    # when it is being used to iterate.
    planned = tuple(item for module in modules for item in module.items)
    still_missing = uncovered(planned, covered)
    logger.info(
        "wrote %s: %d section(s), %d group(s); %d entr(ies) from %d item(s); spend $%.2f",
        target,
        len(manifest.manual_sections),
        len(manifest.embedded_groups),
        len(generated),
        total_items,
        spend.curr_cost,
    )
    if still_missing:
        logger.warning(
            "%d item(s) are named by no entry: %s",
            len(still_missing),
            ", ".join(still_missing[:40]),
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workdir",
        default="~/.cache/autoprover/cvlr-reference-probe",
        help="Where the probe crate lives. Reused between runs so the dependency graph stays warm.",
    )
    parser.add_argument("--out", default="scripts/cvlr-crates", help="Directory for the manifest.")
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument("--chain", default="solana", help="Which reference set to document.")
    parser.add_argument(
        "--crate",
        action="append",
        default=[],
        help="Restrict to crates whose name starts with this (repeatable).",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=40.0,
        help="Cost ceiling in USD. The run reports what it actually spent.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the module plan and item counts without calling a model.",
    )
    raise SystemExit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
