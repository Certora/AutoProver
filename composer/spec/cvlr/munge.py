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
import posixpath
import re
import textwrap
import tomllib
from collections.abc import Iterator
from pathlib import PurePosixPath

from composer.cargo.metadata import Workspace
from composer.layout import INTERNAL_DIR
from composer.spec.cvlr.conf import DEFAULT_FEATURE
from composer.spec.cvlr.rust_source import DERIVE_LIST, body_span, code_positions

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

    @property
    def created(self) -> dict[str, str]:
        """No new files: this kind rewrites a file the developer already has."""
        return {}

    @property
    def subject(self) -> str:
        """The item this munge is about, for a report or a prompt.

        Named rather than reached for as ``.function``, because the vocabulary no longer addresses
        only functions: :class:`DeriveSwap` is about a type. Callers that render a munge want "what
        was edited", which is what this is.
        """
        return self.function

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


@dataclasses.dataclass(frozen=True)
class DeriveNotFound:
    """A derive the swap names is not on the type, so the swap would be a silent no-op.

    Refused rather than skipped: the whole point of removing ``BorshSerialize`` is that the harness
    supplies it instead, and a swap that removed nothing leaves both impls absent or both present
    depending on which half was wrong. ``present`` is what the type actually derives.
    """

    type_name: str
    missing: tuple[str, ...]
    present: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class DeriveAttributeUnreadable:
    """The captured text has no single ``#[derive(..)]`` list to rewrite.

    Two ``derive`` attributes on one item, or none, or one this module's reader cannot parse. All
    three are refused for the same reason: the swap has to know exactly which derives survive, and
    a guess here silently drops one.
    """

    type_name: str
    why: str


@dataclasses.dataclass(frozen=True)
class ModuleNotFound:
    """No ``mod <name>;`` declaration in the file. ``nearby`` is what the file does declare.

    A *declaration* specifically — ``mod name { .. }`` with a body is not redirectable, since
    ``#[path]`` names the file a module is loaded from and an inline module is not loaded from one.
    Such a module is reported here rather than half-matched, because the attribute would compile and
    do nothing.
    """

    module: str
    nearby: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class ModuleOutsideCrateSource:
    """The declaring file is not under a crate's ``src/``, so the substitute has nowhere to go.

    The substitute's location is derived rather than supplied (:func:`mirror_path`), and the
    derivation needs the crate's source root to mirror *from*. A ``mod`` declared outside one —
    a ``build.rs``, a path this module misread — is refused rather than given an invented home.
    """

    path: str


#: What applying an attribute can come to. ``FunctionNotFound`` is here and not in
#: :data:`ExtractionAttempt` because an attribute is located by name, where an extraction is located
#: by the text it captured — a name that has gone takes the text with it, so the extraction reports
#: the more specific :class:`SourceDrifted`.
type AttributeAttempt = Munged | FunctionNotFound | FunctionAmbiguous | AlreadyMunged

#: What applying an extraction can come to.
type ExtractionAttempt = Munged | FunctionAmbiguous | AlreadyMunged | SourceDrifted

#: What applying a derive swap can come to. It is located by captured text like an extraction,
#: so it reports :class:`SourceDrifted`; the two extra refusals are about the derive list itself.
type DeriveSwapAttempt = (
    Munged | AlreadyMunged | SourceDrifted | FunctionAmbiguous | DeriveNotFound
    | DeriveAttributeUnreadable
)

#: What applying a module redirect can come to. Located by declaration rather than by captured
#: text, so it reports :class:`ModuleNotFound` the way an attribute does rather than
#: :class:`SourceDrifted`; :class:`ModuleOutsideCrateSource` is about where the substitute would go.
type ModuleRedirectAttempt = (
    Munged | ModuleNotFound | FunctionAmbiguous | AlreadyMunged | ModuleOutsideCrateSource
)

type MungeAttempt = (
    AttributeAttempt | ExtractionAttempt | DeriveSwapAttempt | ModuleRedirectAttempt
)


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
# a rewrite needs the whole definition. The scanner that answers it — which braces are code and which
# are inside a string, a character literal or a comment — is `rust_source`, shared with the reader of
# the target's Anchor surface.


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
    body = body_span(source, match.start())
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

    @property
    def created(self) -> dict[str, str]:
        """No new files: this kind rewrites a file the developer already has."""
        return {}

    @property
    def subject(self) -> str:
        return self.function

    def describe(self) -> str:
        return f"split so `{self.extracted_name}` is a function a rule can drive"


# ---------------------------------------------------------------------------------------------
# the derive swap: the kind for code no function names
#
# ``docs/the-state-behind-the-bytes.md``. Everything above addresses a *function* — annotate it,
# replace it, split it. A derived trait impl has no function to name: `#[derive(BorshSerialize)]`
# generates the code, and the only handle on it is the derive itself. That is the whole of why this
# kind exists, and §9.3 of that note is why it is narrow — on every framework except native-plus-
# borsh the seam is a function or a method, which ``mock_fn`` already reaches.


#: A ``derive`` list, captured from ``#[derive(A, B, C)]``. Nested generics do not occur in derive
#: lists, so splitting on commas is exact rather than approximate.
@dataclasses.dataclass(frozen=True)
class SwappedDerive:
    """One type's derives, as they read before and as they should read under the feature."""

    type_name: str
    #: The ``#[derive(..)]`` line through the item's declaration line, verbatim. Both halves are
    #: needed: the derive is what gets rewritten, and the declaration is what makes the capture
    #: unique — a bare derive list recurs many times in a state module.
    original: str
    #: Derives that move behind ``cfg_attr(not(feature))``, because the harness supplies them.
    removed: tuple[str, ...]
    #: Derives added under the feature. ``Copy`` in practice, because an impl that assigns
    #: ``*self`` to a global needs it.
    added: tuple[str, ...] = ()

    def _list(self) -> tuple[str, ...] | DeriveAttributeUnreadable:
        found = DERIVE_LIST.findall(self.original)
        if len(found) != 1:
            return DeriveAttributeUnreadable(
                self.type_name,
                f"expected exactly one #[derive(..)] in the captured text, found {len(found)}",
            )
        return tuple(d.strip() for d in found[0].split(",") if d.strip())

    def render(self, feature: str) -> str | DeriveAttributeUnreadable | DeriveNotFound:
        """The captured text with its derive line replaced by the gated trio.

        Derives this swap does not name stay on an ungated ``derive``, so the deployed build sees
        exactly what it saw before — the two ``cfg_attr`` lines are additions, not a rewrite of what
        was there.
        """
        match self._list():
            case DeriveAttributeUnreadable() as bad:
                return bad
            case derives:
                pass
        if missing := tuple(d for d in self.removed if d not in derives):
            return DeriveNotFound(self.type_name, missing, derives)

        kept = tuple(d for d in derives if d not in self.removed)
        indent = self.original[: len(self.original) - len(self.original.lstrip(" \t"))]
        lines: list[str] = []
        if self.removed:
            lines.append(
                f'{indent}#[cfg_attr(not(feature = "{feature}"), '
                f'derive({", ".join(self.removed)}))]'
            )
        if self.added:
            lines.append(
                f'{indent}#[cfg_attr(feature = "{feature}", derive({", ".join(self.added)}))]'
            )
        rest = DERIVE_LIST.sub(
            f'#[derive({", ".join(kept)})]' if kept else "", self.original, count=1
        )
        # Dropping every derive leaves a blank line where the attribute was; the item still needs
        # to land immediately under the gates.
        lines.append(rest if kept else "\n".join(l for l in rest.splitlines() if l.strip()))
        return "\n".join(lines)

    def describe(self) -> str:
        parts = []
        if self.removed:
            parts.append(f"stops deriving {', '.join(self.removed)}")
        if self.added:
            parts.append(f"derives {', '.join(self.added)}")
        return f"`{self.type_name}` " + " and ".join(parts or ["is unchanged"])


@dataclasses.dataclass(frozen=True)
class DeriveSwap:
    """Derives moved behind the unit's feature so the harness can supply the impls instead.

    One record covers **every type in one file** the swap needs, because it cascades and the pieces
    stand or fall together: ``derive(Copy)`` on a struct requires every field type to be ``Copy``,
    so swapping ``StakePool`` alone does not compile until ``AccountType`` is swapped too. Reverting
    half of that leaves a program that does not build, which is why it is one reviewable unit rather
    than several (``docs/the-state-behind-the-bytes.md`` §5).

    A cascade that crosses files is *not* expressible in one record — :func:`replay` applies munges
    per file — and is recorded as one swap per file. The refusal for a half-applied cross-file
    cascade is the compiler's, which is loud and immediate, so this is a sharp edge rather than a
    hazard.

    Unlike :class:`FunctionExtraction` this does not gate an *item*, only its attributes, so the
    ``cfg_attr`` form is safe here: a derive list is one attribute, and gating it cannot leave two
    definitions of one name.
    """

    path: str
    swaps: tuple[SwappedDerive, ...]
    why: str
    feature: str = DEFAULT_FEATURE

    @property
    def edit_id(self) -> str:
        """Identity for deduplication, the report, and what a review approves.

        Digests the rendered result like an extraction's does, because the compiler sees a derive
        list this record computed rather than a name it spelled out.
        """
        material = "|".join(
            f"{sw.type_name}:{','.join(sw.removed)}:{','.join(sw.added)}" for sw in self.swaps
        )
        digest = hashlib.sha256(material.encode()).hexdigest()[:8]
        names = "+".join(sw.type_name for sw in self.swaps)
        return f"{self.feature}:derives[{names}:{digest}]@{self.path}"

    @property
    def created(self) -> dict[str, str]:
        """No new files: this kind rewrites a file the developer already has."""
        return {}

    @property
    def subject(self) -> str:
        return ", ".join(sw.type_name for sw in self.swaps)

    def describe(self) -> str:
        return "; ".join(sw.describe() for sw in self.swaps)


#: Where a redirected module's substitute file lives, relative to the crate's source root. Mirrors
#: the original module hierarchy, which is the convention the scaffold's own ``certora/mocks/mod.rs``
#: already states and which every corpus project follows.
MOCKS_DIR = PurePosixPath("certora/mocks")


def mirror_path(declaring_file: str, module: str) -> PurePosixPath | None:
    """Where the substitute for ``module`` goes, given the file that declares it.

    ``programs/lending/src/invokes/mod.rs`` + ``liquidity_layer`` becomes
    ``programs/lending/src/certora/mocks/invokes/liquidity_layer.rs`` — the mocks tree mirrors the
    source tree beneath it. Derived rather than supplied because a substitute in the wrong place is
    a ``#[path]`` that resolves to nothing, and the compiler's message for that names the *module*,
    not the mistake.

    ``None`` when the declaring file is not under a crate's ``src/``, which the caller reports as
    :class:`ModuleOutsideCrateSource`.
    """
    parts = PurePosixPath(declaring_file).parts
    if "src" not in parts:
        return None
    cut = len(parts) - 1 - parts[::-1].index("src")
    src_root = PurePosixPath(*parts[: cut + 1])
    within = PurePosixPath(*parts[cut + 1 : -1])
    return src_root / MOCKS_DIR / within / f"{module}.rs"


def _path_attr_value(declaring_file: str, substitute: PurePosixPath) -> str:
    """The ``#[path]`` string, which Rust resolves relative to the declaring file's directory.

    Minimal rather than merely correct: climbing to the workspace root and back down resolves to the
    same file, and would break the moment the crate moved within the workspace. Both trees share the
    crate's ``src/``, so the common prefix is always at least that.
    """
    return posixpath.relpath(str(substitute), str(PurePosixPath(declaring_file).parent))


def forwards_feature(program_manifest: str, dependency: str, feature: str) -> bool:
    """Whether the program's ``feature`` enables the same feature in ``dependency``.

    The precondition for a cross-crate redirect, and worth checking rather than assuming, because
    its absence is silent: ``#[cfg_attr(feature = "certora", ..)]`` inside a dependency compiles
    perfectly and simply never activates, so the munge lands, the build succeeds, and the prover
    reports exactly what it reported before. :func:`~composer.spec.cvlr.scaffold._plan_feature_forwarding`
    writes the forward for a project it scaffolds from scratch, and cannot for one that already had
    a ``certora`` feature — nothing here overwrites a feature somebody else defined.
    """
    try:
        parsed = tomllib.loads(program_manifest)
    except tomllib.TOMLDecodeError:
        return False
    enables = parsed.get("features", {}).get(feature)
    return isinstance(enables, list) and f"{dependency}/{feature}" in enables


@dataclasses.dataclass(frozen=True)
class ModuleRedirect:
    """A whole module compiled from a different file behind the unit's feature.

    The kind the other seven cannot express, and the one the corpus reaches for most. ``mock_fn``
    replaces an item with ``use <stand-in> as <name>;``, and a ``use`` inside an ``impl`` is not a
    method — so **no attribute munge can reach an inherent or trait method**, which is where a large
    share of Solana state logic lives. Every normative project answers that the same way, by
    redirecting the file the methods are *in*:

    .. code-block:: rust

        #[cfg_attr(feature = "unit_x", path = "../certora/mocks/invokes/liquidity_layer.rs")]
        pub mod liquidity_layer;

    Nothing is aliased, so the resolution problem never arises: a different file is compiled in the
    module's place, carrying its own types and ``impl`` blocks
    (``docs/cvlr-backend-plan.md`` §7.12 item 3).

    **The substitute is arbitrary code, and that makes this the most dangerous kind.** The other
    seven are bounded — an attribute cannot change what a function computes, and an extraction and a
    derive swap both reproduce the original text verbatim in the deployed half. A substitute module
    is written from scratch, so a rule proved against one is a rule about *the substitute* unless
    what it stands for is stated. :attr:`why` therefore carries a heavier burden here than elsewhere
    and the judge is told so.

    Where it goes is derived (:func:`mirror_path`), not supplied. The deployed build is untouched:
    with the feature off the ``mod`` declaration is exactly what the developer wrote, and the
    substitute file is never named by anything.
    """

    path: str
    module: str
    #: The substitute file's full contents.
    substitute: str
    why: str
    feature: str = DEFAULT_FEATURE

    @property
    def substitute_path(self) -> PurePosixPath | None:
        return mirror_path(self.path, self.module)

    @property
    def created(self) -> dict[str, str]:
        """The files this munge adds to the tree, which no pristine source backs.

        The only kind with any: the tree derives a munged file by replaying onto the developer's
        copy, and there is no developer's copy of a substitute. :meth:`SharedTree.reconcile` puts
        these straight into the overlay.
        """
        target = self.substitute_path
        return {} if target is None else {str(target): self.substitute}

    @property
    def edit_id(self) -> str:
        """Identity for deduplication, the report, and what a review approves.

        Digests the substitute, because that is what the compiler sees and what a reviewer approved.
        Re-authoring the stand-in is a different edit and must not inherit the old approval.
        """
        digest = hashlib.sha256(self.substitute.encode()).hexdigest()[:8]
        return f"{self.feature}:module[{self.module}:{digest}]@{self.path}"

    @property
    def subject(self) -> str:
        return self.module

    def describe(self) -> str:
        return f"`{self.module}` is compiled from a stand-in module"


#: One edit to the program under verification: an attribute on a function, a function split in two,
#: a type's derives moved, or a whole module compiled from elsewhere. All are gated on the recording
#: unit's cargo feature and all replay onto the pristine project, which is the whole of what the
#: rest of the backend needs to know about the difference.
type Munge = FunctionMunge | FunctionExtraction | DeriveSwap | ModuleRedirect


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
        case DeriveSwap():
            return apply_derive_swap(source, munge)
        case ModuleRedirect():
            return apply_module_redirect(source, munge)


#: A ``mod`` **declaration** — the form ``#[path]`` can redirect. ``mod name { .. }`` is excluded by
#: requiring the semicolon: an inline module is not loaded from a file, so the attribute would be
#: accepted by rustc and change nothing.
def _module_declaration(module: str) -> re.Pattern[str]:
    return re.compile(
        rf"^[ \t]*(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+{re.escape(module)}\s*;",
        re.MULTILINE,
    )


_ANY_MODULE = re.compile(
    r"^[ \t]*(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;", re.MULTILINE
)


def apply_module_redirect(source: str, edit: ModuleRedirect) -> ModuleRedirectAttempt:
    """Put the ``cfg_attr`` immediately above the module's declaration.

    Located by declaration rather than by captured text, so a file that has grown or lost unrelated
    modules replays cleanly — the same property an attribute munge has, and for the same reason: the
    edit is an insertion above a line rather than a rewrite of a region.
    """
    target = edit.substitute_path
    if target is None:
        return ModuleOutsideCrateSource(path=edit.path)

    found = list(_module_declaration(edit.module).finditer(source))
    if not found:
        return ModuleNotFound(
            module=edit.module, nearby=tuple(m.group(1) for m in _ANY_MODULE.finditer(source))
        )
    if len(found) > 1:
        return FunctionAmbiguous(
            function=edit.module,
            lines=tuple(source[: m.start()].count("\n") + 1 for m in found),
        )

    at = found[0]
    line = source[: at.start()].count("\n") + 1
    attribute = f'#[cfg_attr(feature = "{edit.feature}", path = "{_path_attr_value(edit.path, target)}")]'
    before = source[: at.start()]
    if before.rstrip("\n").endswith(attribute):
        return AlreadyMunged(function=edit.module, line=line)

    indent = at.group(0)[: len(at.group(0)) - len(at.group(0).lstrip())]
    return Munged(
        source=f"{before}{indent}{attribute}\n{source[at.start():]}", line=line + 1
    )


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


def apply_derive_swap(source: str, edit: DeriveSwap) -> DeriveSwapAttempt:
    """Rewrite each captured derive line into its gated trio.

    All of the file's swaps land or none do: a cascade that applied halfway would leave a struct
    deriving ``Copy`` over a field type that does not, and the compiler's message for that names the
    field rather than the munge. So the refusals are checked against the *original* source first and
    the writes happen afterwards.
    """
    rendered: list[tuple[str, str]] = []
    for swap in edit.swaps:
        if _already(source, swap, edit.feature):
            continue
        match swap.render(edit.feature):
            case (DeriveNotFound() | DeriveAttributeUnreadable()) as refusal:
                return refusal
            case str(text):
                rendered.append((swap.original, text))

    if not rendered:
        first = edit.swaps[0]
        return AlreadyMunged(first.type_name, _line_of(source, first.type_name))

    for original, _ in rendered:
        match _occurrences(source, original):
            case ():
                return SourceDrifted(edit.swaps[0].type_name)
            case (_,):
                pass
            case hits:
                return FunctionAmbiguous(
                    edit.swaps[0].type_name,
                    tuple(source.count("\n", 0, h) + 1 for h in hits),
                )

    at = 0
    for original, text in rendered:
        at = source.find(original)
        source = source[:at] + text + source[at + len(original) :]
    return Munged(source, source.count("\n", 0, at) + 1)


def _already(source: str, swap: SwappedDerive, feature: str) -> bool:
    """Is this type's declaration already sitting under a gate naming this feature?

    Asked of the file's shape rather than by looking for a rendered string, because a swap that has
    landed can no longer render: its derives have moved behind the gate, so :meth:`SwappedDerive
    .render` would report them missing. Which is the same reason this is consulted *before* a
    refusal is believed.
    """
    decl = swap.original.splitlines()[-1]
    at = source.find(decl)
    if at < 0:
        return False
    above = source[:at].splitlines()
    for line in reversed(above):
        if not line.strip():
            continue
        if f'feature = "{feature}"' in line:
            return True
        if not line.lstrip().startswith("#["):
            return False
    return False


def _line_of(source: str, needle: str) -> int:
    at = source.find(needle)
    return source.count("\n", 0, at) + 1 if at >= 0 else 1


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
