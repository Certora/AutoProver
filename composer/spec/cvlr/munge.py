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
import logging
import tomllib

from composer.cargo.metadata import Workspace

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
