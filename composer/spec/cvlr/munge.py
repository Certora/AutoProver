"""Replacing a dependency with the verification-oriented fork of it that Certora maintains.

"Munge" is Certora's word for a modified copy of the code under verification. For Anchor the modified
copy already exists and is maintained: `Certora/anchor <https://github.com/Certora/anchor>`_ carries a
branch per upstream release — ``certora-v0.26.0`` through ``certora-v0.32.1`` — and a verification
project depends on that instead of the crates.io crate. This module is the wiring for that, and
nothing more.

**Why it is needed at all.** Upstream ``anchor_lang::error::Error`` boxes its payload, and the Solana
Prover rejects the resulting ``Box::new`` of a stack-built struct as
**[3006] "illegal store of a stack pointer"** — on every path through Anchor dispatch and every
handler that uses ``?``. The fork's ``Error`` is unboxed, which is what makes an Anchor handler
analyzable. Measured both ways against a real program: with the crates.io crate a rule that calls a
handler cannot be analyzed at all; with the fork the same rule is analyzed and returns a
counterexample.

**Why the fork rather than a patch of our own.** An earlier version of this module derived the unboxing
as a set of textual edits applied to a copy of the registry source. It worked, and it was the wrong
thing: the fork already does it, carries several other verification-oriented changes we had not
derived (a simplified ``require!``, a silenced ``emit!``, public constructors), and is kept current
with upstream releases by people who own it. `docs/cvlr-backend-plan.md` §7.6 records that detour and
what it cost. A locally-derived patch would be a second answer to a solved question, drifting.

**Why a target needs this written for it.** The recommended starting template does not mention the
fork, so a project scaffolded from it depends on crates.io Anchor and hits [3006] with no indication
that a fork exists. That is the actual gap this fills — see `docs/upstream-defects.md` T7.

**Anchor is not the only one, and the list is read rather than reasoned about.** A survey of the nine
verification projects that carry a ``[patch.crates-io]`` table found ``anchor-spl`` patched alongside
``anchor-lang`` everywhere ``anchor-lang`` was, and a second maintained fork nobody here knew about
(``Certora/fixed``). Both are declared below. ``docs/cvlr-backend-plan.md`` §7.6.5 has what each was
worth and §7.6.3 has the wider charter that survey produced; §7.6.7 has the standing rule, which is
that an error reproduced in a scaffold this backend wrote is evidence about the scaffold until
somebody checks it against a project the scaffold did not create.
"""

import dataclasses
import difflib
import hashlib
import logging
import re
import textwrap
import tomllib
from collections.abc import Iterator
from pathlib import PurePosixPath

from composer.cargo.metadata import Workspace
from composer.layout import INTERNAL_DIR
from composer.spec.cvlr.conf import DEFAULT_FEATURE

_log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ForkOverride:
    """A repository of verification-oriented forks, and the crates in it a target may need.

    ``crates`` is a tuple rather than one name because a fork is a *workspace*: ``Certora/anchor``
    publishes both ``anchor-lang`` and ``anchor-spl`` off one branch, and a target that uses both
    needs both redirected. Patching only ``anchor-lang`` does clear [3006] — the boxing is in
    ``anchor_lang::error`` — but leaves ``anchor-spl`` as the upstream crate, whose ``TokenAccount``
    and ``Mint`` are newtypes with a private field. The fork adds ``new_unchecked`` constructors for
    exactly those, so without it a harness cannot build a token account at all. Both projects in the
    corpus that verify an Anchor program patch both crates.

    ``branches`` maps an exact resolved version to a branch name rather than deriving one from a
    pattern. The names do follow a pattern today, but a version with no branch is the case that
    matters: deriving would produce a plausible name, cargo would fail to fetch it, and the error a
    caller sees would be about git rather than about the fork not covering their Anchor. Naming the
    versions that exist means that case blocks with a sentence somebody can act on.
    """

    repo: str
    crates: tuple[str, ...]
    branches: tuple[tuple[str, str], ...]
    why: str

    def branch_for(self, version: str) -> str | None:
        return next((branch for v, branch in self.branches if v == version), None)

    def covered(self) -> str:
        return ", ".join(v for v, _ in self.branches)


@dataclasses.dataclass(frozen=True)
class Blocked:
    """A reason the override cannot be applied, phrased for whoever has to resolve it."""

    crate: str
    problem: str
    resolution: str


@dataclasses.dataclass(frozen=True)
class AlreadySourced:
    """The target already decides where this crate comes from, so nothing was changed.

    Reported rather than dropped, and reported with the source in it, because the two cases read
    identically from the outside and only one of them is fine. A project already pointing at the
    fork needs nothing. A project pointing at some *other* fork of the same crate has made a
    deliberate choice this module will not override — and if a handler then refuses to analyze, this
    line is where somebody should look first.
    """

    crate: str
    source: str
    #: The repository the override would have used, so ``points_at_fork`` can be derived rather
    #: than stored as a flag alongside it.
    fork_repo: str

    @property
    def points_at_fork(self) -> bool:
        return _repo_key(self.fork_repo) in _repo_key(self.source)

    def describe(self) -> str:
        if self.points_at_fork:
            return f"{self.crate} already comes from {self.fork_repo}; nothing to do"
        return (
            f"{self.crate} already comes from {self.source} rather than {self.fork_repo}, so it "
            f"was left alone — a source in the manifest is somebody's decision. If a handler will "
            f"not analyze, this is the first thing to check."
        )


@dataclasses.dataclass(frozen=True)
class AlreadyRedirected:
    """This workspace's own ``[patch.crates-io]`` table already names the crate.

    A separate case from :class:`AlreadySourced` rather than one with a stand-in URL in it, because
    there is genuinely less to say: the table names a redirect, and whether it points at the fork is
    a question about text this module did not resolve. The graph answers that on the next run.
    """

    crate: str

    def describe(self) -> str:
        return (
            f"{self.crate} is already redirected in this workspace's [patch.crates-io] table"
        )


#: What the planner reports when a crate is already in the manifest's patch table but not yet in the
#: resolved graph — the graph is a snapshot, and it can predate the table.
type LeftAlone = AlreadySourced | AlreadyRedirected


def _repo_key(url: str) -> str:
    """A git URL reduced to what two spellings of the same repository share.

    ``cargo metadata`` reports a patched dependency as
    ``git+https://github.com/Certora/anchor.git?branch=certora-v0.31.1#<sha>``, so a comparison
    against the declared repo has to survive the scheme prefix, the query and the fragment.
    """
    return url.removeprefix("git+").split("?")[0].split("#")[0].removesuffix(".git").lower()


@dataclasses.dataclass(frozen=True)
class Override:
    """One dependency's replacement, resolved against a particular target."""

    crate: str
    version: str
    repo: str
    branch: str
    why: str

    def manifest_addition(self) -> str:
        """The ``[patch.crates-io]`` entry that redirects the graph at the fork.

        A branch rather than a pinned commit, which is what the reference project does: the lockfile
        records the commit, so the build is reproducible without this file having to be edited every
        time the fork picks up a fix.
        """
        return (
            f"\n[patch.crates-io.{self.crate}]\n"
            f'git = "{self.repo}"\n'
            f'branch = "{self.branch}"\n'
        )


@dataclasses.dataclass(frozen=True)
class MungePlan:
    """What munging this target would do. Empty when nothing needs it, which is the common case."""

    overrides: tuple[Override, ...] = ()
    blocked: tuple[Blocked, ...] = ()
    #: Crates the target does not resolve at all. Reported rather than dropped: "Anchor was not
    #: replaced" is a fact a reader of a [3006] failure needs, and silence looks like success.
    inapplicable: tuple[str, ...] = ()
    #: Crates left alone because the target already decides where they come from. See
    #: :data:`LeftAlone`.
    already: tuple[LeftAlone, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.overrides)

    def notes(self) -> list[str]:
        """Everything the plan decided not to change, in words, for the scaffold's review output."""
        return [f"{crate} is not a dependency of this project" for crate in self.inapplicable] + [
            a.describe() for a in self.already
        ]


class MungeBlocked(RuntimeError):
    """A munge was applied that could not be. Carries every reason, not the first."""

    def __init__(self, blocked: tuple[Blocked, ...]) -> None:
        self.blocked = blocked
        super().__init__("; ".join(f"{b.crate}: {b.problem}" for b in blocked))


# ---------------------------------------------------------------------------------------------
# the overrides


#: Anchor. Branch names read off the fork; the list is what exists rather than what a pattern would
#: generate, so a project on a release the fork has not been updated for gets told that.
#:
#: Note the gaps, because they are the point of listing versions instead of deriving them: the fork
#: covers 0.30.1 but not 0.30.0, and 0.32.1 but not — as far as this list knows — anything later.
ANCHOR_FORK = ForkOverride(
    repo="https://github.com/Certora/anchor.git",
    crates=("anchor-lang", "anchor-spl"),
    branches=(
        ("0.26.0", "certora-v0.26.0"),
        ("0.27.0", "certora-v0.27.0"),
        ("0.28.0", "certora-v0.28.0"),
        ("0.29.0", "certora-v0.29.0"),
        ("0.30.1", "certora-v0.30.1"),
        ("0.31.1", "certora-v0.31.1"),
        ("0.32.0", "certora-v0.32.0"),
        ("0.32.1", "certora-v0.32.1"),
    ),
    why=(
        "Upstream anchor_lang::error::Error boxes its payload, and the Solana Prover rejects the "
        "resulting Box::new of a stack-built struct as [3006] 'illegal store of a stack pointer' — "
        "on every path through Anchor dispatch. The fork's Error is unboxed, and carries other "
        "verification-oriented changes besides: anchor-spl gains public new_unchecked constructors "
        "for TokenAccount and Mint, whose upstream newtypes a harness cannot otherwise build. "
        "See docs/upstream-defects.md P1."
    ),
)

#: The fixed-point arithmetic crate, forked for the same class of reason as Anchor: the fork adds
#: conversions verification code needs and upstream does not provide.
#:
#: One branch, because one is what there is evidence for — two corpus projects resolve ``fixed``
#: 1.23.1 to ``certora-v1.23.1``, at the same commit. Anything else blocks, which is the design:
#: a project on a `fixed` release nobody has forked should be told so rather than have a plausible
#: branch name invented for it.
FIXED_FORK = ForkOverride(
    repo="https://github.com/Certora/fixed.git",
    crates=("fixed",),
    branches=(("1.23.1", "certora-v1.23.1"),),
    why=(
        "The Certora-maintained fork of `fixed` carries conversions verification code needs (e.g. "
        "From<u64> for FixedU64) that upstream does not provide, so a harness over a program using "
        "fixed-point arithmetic cannot construct its own values without it."
    ),
)

#: The overrides the Solana backend writes. Both were read off real verification projects rather
#: than reasoned about — see ``docs/cvlr-backend-plan.md`` §7.6.5, and the rule in §7.6.4 about
#: errors reproduced in a scaffold this backend wrote.
SOLANA_OVERRIDES: tuple[ForkOverride, ...] = (ANCHOR_FORK, FIXED_FORK)


# ---------------------------------------------------------------------------------------------
# planning


def already_patched(manifest_text: str) -> frozenset[str]:
    """The crates a workspace manifest's ``[patch.crates-io]`` table already redirects.

    Parsed rather than searched for, because the two spellings are the same TOML and look nothing
    alike as text: a shared ``[patch.crates-io]`` header with one inline table per crate, which is
    what every project in the corpus writes, and a ``[patch.crates-io.<crate>]`` sub-table per
    crate, which is what this module writes. A search for either misses the other, and appending a
    second entry for a key TOML already has is a manifest cargo refuses outright.

    The resolved graph is the better source for this — it is what cargo actually computed — and
    :func:`plan_munge` uses it. This covers the case the graph cannot: a ``Workspace`` snapshot
    taken before the patch table was applied.
    """
    try:
        parsed = tomllib.loads(manifest_text)
    except tomllib.TOMLDecodeError:
        # cargo parsed this manifest to produce the graph, so failing here means this reader
        # disagrees with cargo's. Worth a line; not worth refusing to scaffold over.
        _log.warning("could not parse the workspace manifest to look for existing patches")
        return frozenset()
    patch = parsed.get("patch")
    if not isinstance(patch, dict):
        return frozenset()
    crates_io = patch.get("crates-io")
    return frozenset(crates_io) if isinstance(crates_io, dict) else frozenset()


def plan_munge(
    workspace: Workspace,
    overrides: tuple[ForkOverride, ...] = SOLANA_OVERRIDES,
    already_redirected: frozenset[str] = frozenset(),
) -> MungePlan:
    """What munging ``workspace`` would change, without changing anything.

    Four outcomes per crate, and the distinctions all cost something to get wrong:

    * **inapplicable** — not in the resolved graph. Most targets are not Anchor programs.
    * **already sourced** — a workspace member, a path dependency, a git dependency, or a crate
      named in ``already_redirected``. Somebody decided where this crate comes from; overriding it
      would replace a choice, quite possibly *this same fork*. Read from the resolved graph, which
      is what cargo itself computed; ``already_redirected`` (see :func:`already_patched`) covers the
      one case the graph cannot, a snapshot taken before the patch table was applied.
    * **blocked** — resolves at a version the fork has no branch for. Blocked rather than skipped,
      because the alternative is a build that silently keeps the boxing.
    * **overridden** — the case this module exists for.
    """
    resolved_overrides: list[Override] = []
    blocked: list[Blocked] = []
    inapplicable: list[str] = []
    already: list[LeftAlone] = []

    for fork in overrides:
        for crate in fork.crates:
            resolved = workspace.resolved(crate)
            if resolved is None:
                inapplicable.append(crate)
                continue
            if crate in already_redirected:
                already.append(AlreadyRedirected(crate=crate))
                continue
            if resolved.source is None or not resolved.source.startswith("registry+"):
                source = resolved.source or "a path in this workspace"
                already.append(AlreadySourced(crate=crate, source=source, fork_repo=fork.repo))
                _log.info("%s already comes from %s; leaving it alone", crate, source)
                continue
            branch = fork.branch_for(resolved.version)
            if branch is None:
                blocked.append(
                    Blocked(
                        crate=crate,
                        problem=(
                            f"this project resolves {crate} {resolved.version}, and {fork.repo} "
                            f"has no branch recorded for it (have: {fork.covered()})"
                        ),
                        resolution=(
                            f"pin {crate} to a covered version, or ask for a branch covering "
                            f"{resolved.version} on the fork and add it here — do not verify "
                            f"against the unforked crate, which cannot analyze an Anchor handler"
                        ),
                    )
                )
                continue
            resolved_overrides.append(
                Override(
                    crate=crate,
                    version=resolved.version,
                    repo=fork.repo,
                    branch=branch,
                    why=fork.why,
                )
            )

    return MungePlan(
        tuple(resolved_overrides), tuple(blocked), tuple(inapplicable), tuple(already)
    )


def manifest_additions(plan: MungePlan) -> str:
    """The ``[patch.crates-io]`` section this plan needs in the workspace manifest.

    Raises on a blocked plan rather than emitting a partial section: a manifest that replaces one of
    two crates is a build whose failure has two candidate causes.
    """
    if plan.blocked:
        raise MungeBlocked(plan.blocked)
    if not plan.overrides:
        return ""
    header = (
        "\n# === Certora CVLR — added by AutoProver ===\n"
        "# Verification-only dependency replacements. These are NOT the deployed program's\n"
        "# dependencies: a property proved against a fork is a property of the fork, and whether it\n"
        "# carries over is a judgement about the specific difference.\n"
    )
    # Grouped by reason, because two crates from one fork share one: repeating a paragraph verbatim
    # under each of them reads like two unrelated changes that happen to say the same thing.
    reasons = ""
    for why in dict.fromkeys(o.why for o in plan.overrides):
        redirects = [o for o in plan.overrides if o.why == why]
        reasons += "#\n" + "".join(
            f"# {o.crate} {o.version} -> {o.branch}\n" for o in redirects
        )
        reasons += "".join(f"#   {line}\n" for line in _wrapped(why))
    return header + reasons + "".join(o.manifest_addition() for o in plan.overrides)


def _wrapped(text: str, width: int = 88) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


# ---------------------------------------------------------------------------------------------
# the source half: verification-only attributes on the program's own functions
#
# ``docs/cvlr-backend-plan.md`` §7.6.3 has the charter these two kinds were read off, and §7.6.4 the
# boundary. Both are CVLR attributes rather than rewrites, which is what makes applying one a
# mechanical edit with a compile gate behind it instead of an agent editing a program.


@dataclasses.dataclass(frozen=True)
class EarlyPanic:
    """Every ``?`` in the function becomes ``.unwrap()``.

    Literally that — ``cvlr_early_panic`` walks the body and rewrites ``Expr::Try``. So it is the
    ``.unwrap()``-versus-``.is_ok()`` choice the author prompt teaches, reached one level down: a
    ``?`` inside the program, below the handler a rule calls, whose error construction the pointer
    analysis then refuses ([3308]). The most common munge in the corpus by a wide margin.

    What it cannot do is make an *acceptance* property statable. It removes the failure path rather
    than exposing one, so "this handler accepts a zero amount" is no more provable after it.
    """

    def attribute(self) -> str:
        return "cvlr::early_panic"

    def describe(self) -> str:
        return "`?` rewritten to `.unwrap()` throughout, so error paths panic and are pruned"


@dataclasses.dataclass(frozen=True)
class MockFn:
    """The function is replaced by a named stand-in.

    Differs from a summary (:mod:`composer.spec.cvlr.tuning`) in the direction that matters: a
    summary havocs the return, a mock *computes* one. So a property downstream of a mocked function
    still means something, where downstream of a summary it usually does not.
    """

    #: Path to the replacement, as the munged file must be able to spell it. The munged file is the
    #: program's own, so the path has to resolve from *outside* ``certora`` — which is why
    #: ``specs``/``mocks`` and each authored unit's module are declared ``pub``
    #: (:data:`composer.spec.cvlr.scaffold._HARNESS_FILES`).
    stand_in: str

    def attribute(self) -> str:
        return f"cvlr::mock_fn(with = {self.stand_in})"

    def describe(self) -> str:
        return f"replaced by {self.stand_in}"


@dataclasses.dataclass(frozen=True)
class InlineNever:
    """The function is kept as its own frame rather than being inlined into its callers.

    Behaviour-preserving outright — ``inline`` is a hint about codegen and nothing else — which makes
    it the cheapest thing in the vocabulary to justify. It earns its place because a summary, an
    inlining directive and a counterexample's call stack all address a function *by symbol*, and a
    function the optimizer folded away has no symbol to address. The corpus uses it exactly that way,
    three of five times paired with ``early_panic``.
    """

    def attribute(self) -> str:
        return "inline(never)"

    def describe(self) -> str:
        return "kept out of line, so it keeps a symbol the prover can name"


@dataclasses.dataclass(frozen=True)
class HookOnEntry:
    """A call inserted as the function's first statement.

    ``cvlr::hook_on_entry`` (``cvlr-hook``, in the pinned reference set) rewrites the body to run
    ``call`` before anything else. Sound when the call only *observes* — records a flag, checks an
    invariant, logs — which is every use in the corpus; a call that mutates the program's state is a
    rewrite wearing an attribute and is outside the charter.

    It is the vocabulary's only instrument for reaching a point *inside* an execution. Everything
    else states a property at a function boundary, so a property about a state transition buried in a
    handler has, until now, had nowhere to attach.
    """

    call: str

    def attribute(self) -> str:
        return f"cvlr::hook_on_entry({self.call})"

    def describe(self) -> str:
        return f"calls {self.call} on entry"


@dataclasses.dataclass(frozen=True)
class HookOnExit:
    """A call inserted before the function returns. The exit half of :class:`HookOnEntry`."""

    call: str

    def attribute(self) -> str:
        return f"cvlr::hook_on_exit({self.call})"

    def describe(self) -> str:
        return f"calls {self.call} on exit"


#: The declared munge kinds — the charter, as types. A change that would need a *new* one is the
#: give-up boundary (§7.6.4): not an edit budget, because the one real source munge in the corpus is
#: 1097 routine lines and one hand-unrolled loop, and only the loop needed a person.
#:
#: Five, not the two this backend shipped first. The three added here are the rest of what a corpus
#: survey found projects actually writing, minus one that cannot be had: ``certora_make_pub`` is a
#: *project-local* proc macro in the one project that uses it and has no counterpart in ``cvlr``,
#: and Rust cannot make visibility conditional on a feature without duplicating the item.
#:
#: The sixth kind a corpus survey found is **not** an attribute and so is not in this union at all —
#: see :class:`FunctionExtraction`, which is a sibling of :class:`FunctionMunge` rather than one
#: more entry here.
type MungeKind = EarlyPanic | MockFn | InlineNever | HookOnEntry | HookOnExit


@dataclasses.dataclass(frozen=True)
class FunctionMunge:
    """One verification-only attribute on one of the program's functions, and what turns it on.

    ``feature`` is the cargo feature the attribute is gated on, and it is what makes a shared
    working tree possible (``docs/single-working-tree.md`` §2.3). A munge recorded by one unit is a
    **dormant** line for every other: with the feature off, ``cfg_attr`` contributes no attribute and
    the compiled function is the one the project shipped. So two units' munges of the same function
    coexist as two lines, and the union of every unit's munges is the single well-defined content of
    that file.

    Gating the whole ``cfg_attr`` rather than using ``cvlr::mock_fn``'s own ``when`` parameter — the
    corpus idiom — because it is one mechanism for both kinds: ``cvlr::early_panic`` has no ``when``,
    and the corpus gates *it* by wrapping the ``cfg_attr`` condition anyway. The effect is identical
    and there is one thing to read.
    """

    #: The file, relative to the workspace root, so the record means the same thing in the report as
    #: on disk.
    path: str
    function: str
    kind: MungeKind
    #: Why the prover could not analyze the function as written, and why this attribute is sound for
    #: the properties in this batch. The only account anybody gets, exactly as with a summary.
    why: str
    #: The cargo feature that activates it. Defaulted to the whole-harness feature so a record
    #: written without one still means what it used to; every munge this backend records names the
    #: recording unit's own feature.
    feature: str = DEFAULT_FEATURE

    @property
    def edit_id(self) -> str:
        """Identity for deduplication and for the report.

        The feature is part of it: two units munging the same function the same way are two distinct
        lines in the file, each dormant for the other, and collapsing them would drop one.
        """
        return f"{self.feature}:{self.kind.attribute()}@{self.path}::{self.function}"

    def describe(self) -> str:
        return self.kind.describe()

    def attribute_line(self, indent: str) -> str:
        return f'{indent}#[cfg_attr(feature = "{self.feature}", {self.kind.attribute()})]'


#: Top-level directories inside a unit's workdir that are **not** the project's source.
#:
#: Two uses, and they are the same fact: these are what a workspace copy leaves behind
#: (:data:`composer.spec.cvlr.pipeline._NOT_COPIED`) and what a munge may not touch. A munge is a
#: modification of *the program under verification*, and containment in the workdir does not
#: establish that — confinement puts the run's private ``CARGO_HOME`` under
#: :data:`~composer.sandbox.recipes.SANDBOX_CARGO_DIR`, so every dependency's unpacked source is
#: inside the workdir too. Without this, ``munge_function`` would happily rewrite Anchor.
#:
#: That is the same failure ``validate_rule_subjects`` exists to prevent one axis over: a rule that
#: drives a dependency proves a property of the dependency, and a munge of one modifies a
#: dependency's behaviour for every crate that uses it, including the ones the property is about.
#:
#: The cargo home needs no entry of its own: it lives under :data:`INTERNAL_DIR`, and the match is
#: on the first path component.
NOT_PROJECT_SOURCE = (
    "target",
    ".git",
    INTERNAL_DIR.name,
    "certora_out",
    ".cvlr_work",
)


def is_project_source(relative: PurePosixPath | str) -> bool:
    """Whether a workdir-relative path names a file the project shipped rather than one a build made.

    Judged on the first path component, which is where every entry in :data:`NOT_PROJECT_SOURCE`
    sits — a nested ``target/`` belonging to a vendored crate is already excluded by its parent.
    """
    parts = PurePosixPath(relative).parts
    return bool(parts) and parts[0] not in NOT_PROJECT_SOURCE


@dataclasses.dataclass(frozen=True)
class NotProjectSource:
    """The path is inside the working tree but is not the project's source.

    Almost always a dependency: confinement puts the run's ``CARGO_HOME`` under
    ``.certora_internal/sandbox/cargo``, so every crate the build resolves has its unpacked source
    in the tree, a few directories away from the program.
    """

    path: str
    directory: str

    def describe(self) -> str:
        return f"{self.path} is under {self.directory}, which is not the project's source"


@dataclasses.dataclass(frozen=True)
class Munged:
    """The attribute was inserted.

    ``line`` is where the function's signature sits **in the returned source**, 1-indexed — so it
    is what a message about the munge should quote, and the attribute is the line above it.
    """

    source: str
    line: int


@dataclasses.dataclass(frozen=True)
class FunctionNotFound:
    """No function of that name in the file. ``nearby`` is what the file does define."""

    function: str
    nearby: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class FunctionAmbiguous:
    """Several functions of that name — a trait impl and an inherent one, or two impl blocks.

    Refused rather than guessed at: munging the wrong one of two same-named functions produces a
    build that compiles, a rule that still fails, and no indication which of them was changed.
    """

    function: str
    lines: tuple[int, ...]


@dataclasses.dataclass(frozen=True)
class AlreadyMunged:
    """The edit is already in the file, so it is unchanged."""

    function: str
    line: int


@dataclasses.dataclass(frozen=True)
class SourceDrifted:
    """The exact text an extraction was recorded against is no longer in the file.

    Only :class:`FunctionExtraction` can report this, and it is the price of the one kind that is
    not an attribute. An attribute is *inserted* above a signature, so replaying it needs only the
    signature to still be there; a rewrite replaces a region and has to know the region is
    unchanged. Content addressing is what makes a moved region loud rather than a rewrite applied to
    code nobody read (``docs/who-edits-the-program.md`` §8.4).
    """

    function: str


@dataclasses.dataclass(frozen=True)
class NoFunctionBody:
    """The signature has no body — a trait method declaration, or an ``extern`` block entry.

    An attribute on one of those is legal and occasionally wanted; an extraction of one is not a
    thing, because there is nothing to extract.
    """

    function: str


#: What applying an attribute can come to. ``FunctionNotFound`` is here and not in
#: :data:`ExtractionAttempt` because an attribute is located by name, where an extraction is located
#: by the text it captured — a name that has gone takes the text with it, so the extraction reports
#: the more specific :class:`SourceDrifted`.
type AttributeAttempt = Munged | FunctionNotFound | FunctionAmbiguous | AlreadyMunged

#: What applying an extraction can come to.
type ExtractionAttempt = Munged | FunctionAmbiguous | AlreadyMunged | SourceDrifted

type MungeAttempt = AttributeAttempt | ExtractionAttempt


def _signature_pattern(function: str) -> re.Pattern[str]:
    """Where a named function's signature starts.

    Deliberately anchored at the line and permissive about what precedes ``fn``: visibility, and the
    ``const`` / ``async`` / ``unsafe`` / ``extern "C"`` qualifiers, in the order Rust requires them.
    The trailing ``[(<]`` is what keeps ``fn deposit`` from matching ``fn deposit_fee``.
    """
    return re.compile(
        r"^(?P<indent>[ \t]*)"
        r"(?:pub(?:\([^)]*\))?[ \t]+)?"
        r"(?:default[ \t]+)?"
        r"(?:const[ \t]+)?"
        r"(?:async[ \t]+)?"
        r"(?:unsafe[ \t]+)?"
        r"(?:extern[ \t]+\"[^\"]*\"[ \t]+)?"
        rf"fn[ \t]+{re.escape(function)}[ \t]*[(<]",
        re.MULTILINE,
    )


_ANY_FN = re.compile(r"\bfn[ \t]+([A-Za-z_][A-Za-z0-9_]*)")


def function_names(source: str) -> tuple[str, ...]:
    """Every function name the file mentions a definition of, in order, deduplicated."""
    return tuple(dict.fromkeys(_ANY_FN.findall(source)))


# ---------------------------------------------------------------------------------------------
# extraction: the one kind that is not an attribute
#
# ``docs/who-edits-the-program.md`` §8.4. Everything above attaches a line to a function that already
# exists, which is why applying one is a mechanical insert. Extraction *splits* a function so a rule
# can drive a piece of it directly, and there is no attribute for that — so it is a rewrite, and a
# rewrite needs two things the attribute kinds get for free: it has to be content-addressed, so a
# replay onto source that has moved fails loudly instead of quietly rewriting the wrong region; and
# it cannot be *inert by accident*, which in Rust means the gated-pair form below rather than a
# ``cfg_attr``.
#
# Finding where an item ends is the machinery that costs: an attribute needs only the signature line,
# a rewrite needs the whole definition. Hence a small Rust scanner — enough to know which braces are
# code and which are inside a string, a character literal or a comment.


#: A raw string opener, ``r"``/``r#"``/``br##"`` — the closing delimiter carries the same hash count.
_RAW_STRING = re.compile(r'b?r(?P<hashes>#*)"')

#: A character or byte-character literal. The escapes are spelled out rather than left to ``\\.``
#: because ``'\u{1F600}'`` contains a brace, and a scanner that fell through to counting it would
#: mismatch every brace after it.
_CHAR_LITERAL = re.compile(r"b?'(?:\\(?:x[0-9a-fA-F]{2}|u\{[0-9a-fA-F_]{1,6}\}|.)|[^\\'\n])'")


def _code_positions(source: str, start: int) -> Iterator[int]:
    """Indices of the characters of ``source`` from ``start`` that are code.

    Skips line and (nesting) block comments, strings, raw strings, and character literals — the four
    places a brace can appear without opening or closing a block. A lifetime is *not* skipped and
    does not need to be: ``'a`` fails :data:`_CHAR_LITERAL` for want of a closing quote, and the
    characters it yields are not braces.
    """
    i, n = start, len(source)
    while i < n:
        c = source[i]
        if source.startswith("//", i):
            newline = source.find("\n", i)
            if newline < 0:
                return
            i = newline
            continue
        if source.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if source.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif source.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            continue
        if (raw := _RAW_STRING.match(source, i)) is not None:
            closing = '"' + raw["hashes"]
            end = source.find(closing, raw.end())
            i = n if end < 0 else end + len(closing)
            continue
        if c == '"' or source.startswith('b"', i):
            i += 2 if c == "b" else 1
            while i < n:
                if source[i] == "\\":
                    i += 2
                elif source[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
            continue
        if (char := _CHAR_LITERAL.match(source, i)) is not None:
            i = char.end()
            continue
        yield i
        i += 1


def _body_span(source: str, signature_start: int) -> tuple[int, int] | None:
    """The body of the signature at ``signature_start``: its opening brace, and one past its close.

    The body's opening brace is the first one at bracket depth zero, which is what keeps a ``{`` in
    a parameter's type or a ``pub(crate)`` from being mistaken for it. A ``;`` at depth zero first
    means there is no body at all.
    """
    depth, opened = 0, None
    for i in _code_positions(source, signature_start):
        match source[i]:
            case "(" | "[":
                depth += 1
            case ")" | "]":
                depth -= 1
            case "{" if depth == 0:
                opened = i
                break
            case ";" if depth == 0:
                return None
            case _:
                pass
    if opened is None:
        return None
    depth = 0
    for i in _code_positions(source, opened):
        match source[i]:
            case "{":
                depth += 1
            case "}":
                depth -= 1
                if depth == 0:
                    return opened, i + 1
            case _:
                pass
    return None


@dataclasses.dataclass(frozen=True)
class FunctionItem:
    """A whole function definition as it stands in some source.

    ``signature`` is whitespace-collapsed so two spellings of one contract compare equal. That is
    the check that makes an extraction a *refactoring*: the enclosing function keeps its name, its
    parameters and its return type, so every caller in the program — including the ones no rule
    mentions — is unaffected by which half of the gated pair it compiles.
    """

    text: str
    signature: str
    line: int


def function_item(
    source: str, function: str
) -> FunctionItem | FunctionNotFound | FunctionAmbiguous | NoFunctionBody:
    """The one definition of ``function`` in ``source``, or why it cannot be pinned down.

    The refusals are :func:`apply_munge`'s, for the same reasons: a name that matches nothing is a
    typo worth reporting by name, and two functions of one name is a choice this module will not
    make on somebody's behalf.
    """
    matches = list(_signature_pattern(function).finditer(source))
    if not matches:
        return FunctionNotFound(
            function=function,
            nearby=tuple(
                difflib.get_close_matches(function, function_names(source), n=3, cutoff=0.5)
            ),
        )
    if len(matches) > 1:
        return FunctionAmbiguous(
            function, tuple(source.count("\n", 0, m.start()) + 1 for m in matches)
        )
    (match,) = matches
    body = _body_span(source, match.start())
    if body is None:
        return NoFunctionBody(function)
    opened, end = body
    return FunctionItem(
        text=source[match.start() : end],
        signature=" ".join(source[match.start() : opened].split()),
        line=source.count("\n", 0, match.start()) + 1,
    )


def _reindent(text: str, indent: str) -> str:
    """Model-written Rust, re-laid-out to sit where the function it replaces sat."""
    return textwrap.indent(textwrap.dedent(text).strip("\n"), indent)


@dataclasses.dataclass(frozen=True)
class FunctionExtraction:
    """A function split into a feature-gated pair so a rule can drive a piece of it.

    The kind two of the last gate run's eight skips wanted and the vocabulary could not express: a
    property about a state transition buried inside a handler has no function boundary to attach to,
    and no attribute creates one. :class:`HookOnEntry` reaches such a point to *observe* it; this is
    for the case where the rule has to **call** it.

    What replaces the original function is three items rather than one::

        #[cfg(not(feature = "unit_x"))]
        fn settle(..) { /* the original, untouched */ }
        #[cfg(feature = "unit_x")]
        fn settle(..) { settle_transition(..) }
        #[cfg(feature = "unit_x")]
        pub fn settle_transition(..) { /* what the rule descends to */ }

    Ugly, and it keeps every property the attribute kinds have. The deployed build compiles the
    original verbatim, because :attr:`original` is captured text rather than something a model
    retyped. Every sibling unit compiles it too, since the pair is gated on the requesting unit's
    own feature. And it cannot be inert by accident: :meth:`render` writes both ``cfg`` lines, so
    there is no spelling of this record in which the feature-off build sees anything new.

    :attr:`original` is also what makes replay safe. It is the drift detector the attribute kinds
    get from ``FunctionNotFound``: a rewrite has to know the region it replaces is still the region
    it was recorded against, so the text is stored and a replay that cannot find it reports
    :class:`SourceDrifted` rather than rewriting whatever is there now.

    One thing the pair does not carry across: attributes and doc comments already above the function
    stay above the ``#[cfg(not(..))]`` line, so they apply to the original half and not to the
    feature-on half. That is right for a ``#[cfg_attr]`` recorded by *another* unit — that unit
    builds with this feature off — and it is why :func:`replay` applies attributes first.
    """

    path: str
    function: str
    #: The new function's name, as :attr:`extracted` spells it and as the author's rule will call it.
    extracted_name: str
    #: The pristine definition of :attr:`function`, captured verbatim when the extraction was
    #: recorded. Not authored by anybody: retyping it is how the deployed half stops being the
    #: deployed half.
    original: str
    #: :attr:`function` as it reads under the feature — same signature, body delegating to the
    #: extracted item.
    replacement: str
    #: The extracted item itself, ``pub`` so the harness module can name it.
    extracted: str
    why: str
    feature: str = DEFAULT_FEATURE

    def render(self) -> str:
        """The gated triple, indented to sit where the original sat."""
        indent = self.original[: len(self.original) - len(self.original.lstrip(" \t"))]
        gate_on = f'{indent}#[cfg(feature = "{self.feature}")]'
        return "\n".join(
            [
                f'{indent}#[cfg(not(feature = "{self.feature}"))]',
                self.original,
                gate_on,
                _reindent(self.replacement, indent),
                gate_on,
                _reindent(self.extracted, indent),
            ]
        )

    @property
    def edit_id(self) -> str:
        """Identity for deduplication, for the report, and for what a review is approval *of*.

        Carries a digest of the rewrite itself, which the attribute kinds have no need of: an
        attribute's whole content is its name, so ``edit_id`` spells it out. Here the compiler sees
        two blocks of model-written Rust, and an id blind to them would let a re-recorded extraction
        inherit the previous one's review and the previous one's prover stamp.
        """
        digest = hashlib.sha256(self.render().encode()).hexdigest()[:8]
        return f"{self.feature}:extract[{self.extracted_name}:{digest}]@{self.path}::{self.function}"

    def describe(self) -> str:
        return f"split so `{self.extracted_name}` is a function a rule can drive"


#: One edit to the program under verification: an attribute on a function, or a function split in
#: two. Both are gated on the recording unit's cargo feature and both replay onto the pristine
#: project, which is the whole of what the rest of the backend needs to know about the difference.
type Munge = FunctionMunge | FunctionExtraction


def apply_munge(source: str, munge: Munge) -> MungeAttempt:
    """Apply one recorded edit to ``source``, or say why it did not take.

    The refusals are the drift detector for a resumed or replayed run
    (``docs/single-working-tree.md`` §4.3). Replay happens against the *pristine* project, which can
    have moved since the edit was recorded; a function that is gone, has acquired a same-named
    sibling, or whose body no longer reads as it did is reported here rather than silently
    mis-applied, which is what a text overlay of the old file would have done.
    """
    match munge:
        case FunctionMunge():
            return apply_attribute(source, munge)
        case FunctionExtraction():
            return apply_extraction(source, munge)


def apply_attribute(source: str, munge: FunctionMunge) -> AttributeAttempt:
    """Insert the attribute immediately above its function's signature.

    Above the signature puts it *below* any doc comment and any attribute already there — legal, and
    it keeps the insertion a single-line edit whose diff reads as one. The compile gate is what
    catches a signature this misjudged; these three refusals are the cases a compile would accept
    and a reader would not.
    """
    matches = list(_signature_pattern(munge.function).finditer(source))
    if not matches:
        return FunctionNotFound(
            function=munge.function,
            nearby=tuple(
                difflib.get_close_matches(munge.function, function_names(source), n=3, cutoff=0.5)
            ),
        )
    lines = source.splitlines(keepends=True)
    starts = [source.count("\n", 0, m.start()) for m in matches]
    if len(matches) > 1:
        return FunctionAmbiguous(munge.function, tuple(n + 1 for n in starts))
    (index,) = starts
    attribute = munge.attribute_line(matches[0].group("indent"))
    if index > 0 and lines[index - 1].strip() == attribute.strip():
        return AlreadyMunged(munge.function, index + 1)
    lines.insert(index, attribute + "\n")
    # The signature moved down by the line just inserted above it.
    return Munged("".join(lines), index + 2)


def _occurrences(haystack: str, needle: str) -> tuple[int, ...]:
    found: list[int] = []
    at = haystack.find(needle)
    while at >= 0:
        found.append(at)
        at = haystack.find(needle, at + 1)
    return tuple(found)


def apply_extraction(source: str, edit: FunctionExtraction) -> ExtractionAttempt:
    """Replace the captured definition with the gated triple.

    The rendered block *contains* the original verbatim, so "already applied" has to be asked before
    "is the original still here" — otherwise a second replay would find the original inside the
    ``#[cfg(not(..))]`` half and nest one triple inside another.
    """
    rendered = edit.render()
    if (at := source.find(rendered)) >= 0:
        return AlreadyMunged(edit.function, source.count("\n", 0, at) + 2)
    match _occurrences(source, edit.original):
        case ():
            return SourceDrifted(edit.function)
        case (at,):
            pass
        case hits:
            return FunctionAmbiguous(
                edit.function, tuple(source.count("\n", 0, h) + 1 for h in hits)
            )
    updated = source[:at] + rendered + source[at + len(edit.original) :]
    # One line below the `#[cfg(not(..))]` the triple opens with.
    return Munged(updated, updated.count("\n", 0, at) + 2)


def munge_history(munges: tuple[Munge, ...]) -> tuple[str, ...]:
    """The munges as ``version_history`` tokens, so a stamp predating one goes stale with it.

    Keyed on what the prover sees differently — the file, the function, and the attribute or the
    rewrite — and not on ``why``, so correcting the wording of a justification does not cost a
    submission. Same trade as :func:`composer.spec.cvlr.tuning.summary_history`, for the same
    reason.
    """
    return tuple(f"munge:{m.edit_id}" for m in munges)


@dataclasses.dataclass(frozen=True)
class DropMunges:
    """A write that *removes* munges rather than adding them.

    A distinct type rather than a flag on the write, because the two operations carry different
    payloads and a list that sometimes meant "remove these" would be indistinguishable from one that
    meant "add these". It is what makes a munge undoable: the working tree is rebuilt from the
    pristine copy and replayed from this list on every build, so an ``edit_id`` that leaves the list
    leaves the file (``docs/single-working-tree.md`` §4).
    """

    edit_ids: frozenset[str]


type MungeWrite = list[Munge] | DropMunges


def merge_munges(left: list[Munge], right: MungeWrite) -> list[Munge]:
    """State reducer for the munge list: append deduplicating by :attr:`edit_id`, or remove.

    A reducer for the reason ``merge_summaries`` is one — several tool calls can land in one graph
    step, and LangGraph refuses two writes to an unreduced key.
    """
    if isinstance(right, DropMunges):
        return [m for m in left if m.edit_id not in right.edit_ids]
    merged = list(left)
    seen = {m.edit_id for m in merged}
    for munge in right:
        if munge.edit_id in seen:
            continue
        merged.append(munge)
        seen.add(munge.edit_id)
    return merged
