"""Build a Solana project against Certora's forks of Anchor and ``fixed``, not the crates.io ones.

Anchor is the framework most Solana programs are written in. A program uses it through the
``anchor-lang`` crate, and usually ``anchor-spl`` for token accounts, both from crates.io. The
Solana Prover cannot analyze those crates as published. Anchor's error type,
``anchor_lang::error::Error``, moves a struct built on the stack into a heap allocation
(``Box::new``), and the Prover rejects that as [3006], "illegal store of a stack pointer".
Anchor's generated entry point runs that code, and so does any handler that returns an error with
``?``. Against upstream Anchor, in practice, no instruction of the program can be verified.

``Certora/anchor`` is a copy of the Anchor repository with that fixed. For each Anchor release it
supports, it has one branch, ``certora-v<version>``, holding that release plus Certora's changes:
an ``Error`` that does not box, a simpler ``require!``, an ``emit!`` that does nothing, and public
constructors a harness needs, such as ``new_unchecked`` for ``anchor-spl``'s ``TokenAccount`` and
``Mint``, whose fields upstream keeps private. Otherwise the branch has the same API as the
release, so the program compiles unchanged against the branch for the Anchor version it already
uses. This module reads that version from the resolved dependency graph and adds a
``[patch.crates-io]`` entry to the workspace manifest pointing cargo at the matching branch.

``Certora/fixed`` is organized the same way, for the ``fixed`` fixed-point crate, for a different
reason: it adds conversions a harness needs to construct values, such as ``From<u64>`` for
``FixedU64``, which upstream does not provide.

The supported versions are listed explicitly (:data:`ANCHOR_FORK`, :data:`FIXED_FORK`) rather than
derived from the version number. A version with no branch blocks the plan with a message saying
which versions are covered. A derived name would send cargo after a branch that does not exist,
and the failure would be a git fetch error that says nothing about coverage.

A project set up by hand usually stays on the crates.io crates, and hits [3006] with nothing in
the error pointing at the fork.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from composer.cargo.manifest import AddTable, Comment, Manifest, TableItem
from composer.cargo.metadata import (
    CratePackage,
    GitSource,
    OtherSource,
    RegistrySource,
    Workspace,
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ForkOverride:
    """A repository of verification-oriented forks, and the crates in it a target may need.

    ``crates`` is more than one name because a fork is a workspace: ``Certora/anchor`` publishes
    ``anchor-lang`` and ``anchor-spl`` from one branch. A target has every one of them it uses
    redirected, or the ones left out stay upstream.

    ``branches`` maps an exact resolved version to a branch name, listed rather than derived; see
    the module docstring.

    ``upstream_failure`` is what goes wrong when the target is verified against the crates.io
    release instead, quoted when a version the fork does not cover blocks the plan.
    """

    repo: str
    crates: tuple[str, ...]
    branches: Mapping[str, str]
    why: str
    upstream_failure: str

    def branch_for(self, version: str) -> str | None:
        return self.branches.get(version)

    def covered(self) -> str:
        return ", ".join(self.branches)


@dataclass(frozen=True)
class Blocked:
    """Why an override cannot be applied, and what would resolve it."""

    crate: str
    problem: str
    resolution: str


@dataclass(frozen=True)
class AlreadySourced:
    """The target already decides where this crate comes from, so nothing was changed.

    The source is part of the report. A project already on this fork needs nothing. A project on
    some other fork is left alone, and if a handler then will not analyze, this is the first place
    to look.
    """

    crate: str
    #: ``None`` for a path in this workspace.
    source: GitSource | OtherSource | None
    #: The repository the override would have used. ``points_at_fork`` is derived from this
    #: and ``source``.
    fork_repo: str

    @property
    def points_at_fork(self) -> bool:
        return isinstance(self.source, GitSource) and self.source.is_from(self.fork_repo)

    @property
    def origin(self) -> str:
        return self.source.spelling if self.source is not None else "a path in this workspace"

    def describe(self) -> str:
        if self.points_at_fork:
            return f"{self.crate} already comes from {self.fork_repo}; nothing to do"
        return (
            f"{self.crate} already comes from {self.origin} rather than {self.fork_repo}, so it "
            f"was left alone — a source in the manifest is somebody's decision. If a handler will "
            f"not analyze, this is the first thing to check."
        )


@dataclass(frozen=True)
class AlreadyRedirected:
    """This workspace's ``[patch.crates-io]`` table already names the crate.

    Separate from :class:`AlreadySourced` because the table entry has not been resolved to a
    source URL. The next graph read is what says whether it points at the fork.
    """

    crate: str

    def describe(self) -> str:
        return (
            f"{self.crate} is already redirected in this workspace's [patch.crates-io] table"
        )


#: What the planner reports when a crate is already in the manifest's patch table but not yet in the
#: resolved graph — the graph is a snapshot, and it can predate the table.
type LeftAlone = AlreadySourced | AlreadyRedirected


@dataclass(frozen=True)
class Override:
    """One dependency's replacement, resolved against a particular target."""

    crate: str
    version: str
    repo: str
    branch: str
    why: str

    def redirect(self) -> tuple[TableItem, ...]:
        """The ``[patch.crates-io.<crate>]`` body that points the graph at the fork.

        A branch, not a commit. The lockfile records the commit, so the build stays reproducible
        without editing the manifest every time the fork moves.
        """
        return (("git", self.repo), ("branch", self.branch))


@dataclass(frozen=True)
class ForkPlan:
    """What redirecting this target at the forks would change. Empty when nothing needs it."""

    overrides: tuple[Override, ...] = ()
    #: Crates the target does not resolve. Kept in the plan: "Anchor was not replaced" is what a
    #: reader of a [3006] failure needs, and omitting it looks like success.
    inapplicable: tuple[str, ...] = ()
    #: Crates left alone because the target already decides where they come from. See
    #: :data:`LeftAlone`.
    already: tuple[LeftAlone, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.overrides)

    def notes(self) -> list[str]:
        """What the plan left unchanged, for the scaffold's review output."""
        return [f"{crate} is not a dependency of this project" for crate in self.inapplicable] + [
            a.describe() for a in self.already
        ]


@dataclass(frozen=True)
class ForkRefused:
    """Why this target cannot be redirected at the forks: every reason, not the first.

    Nothing is redirected. Replacing one crate while another stays upstream leaves a build whose
    failure has two causes.
    """

    blocked: tuple[Blocked, ...]


# ---------------------------------------------------------------------------------------------
# the overrides


#: Anchor. Branch names taken from the fork. Listed, not derived, so a release the fork has not
#: been updated for is reported as uncovered. The fork has 0.30.1 and not 0.30.0, and nothing
#: after 0.32.1.
ANCHOR_FORK = ForkOverride(
    repo="https://github.com/Certora/anchor.git",
    crates=("anchor-lang", "anchor-spl"),
    branches={
        "0.26.0": "certora-v0.26.0",
        "0.27.0": "certora-v0.27.0",
        "0.28.0": "certora-v0.28.0",
        "0.29.0": "certora-v0.29.0",
        "0.30.1": "certora-v0.30.1",
        "0.31.1": "certora-v0.31.1",
        "0.32.0": "certora-v0.32.0",
        "0.32.1": "certora-v0.32.1",
    },
    why=(
        "Use the Certora fork of Anchor. It avoids the boxing the official library does, which is "
        "hard to analyze."
    ),
    upstream_failure=(
        "upstream Anchor's error type boxes a stack value, which the Prover rejects as [3006] in "
        "the entry point and in every handler that returns an error"
    ),
)

#: The fixed-point crate. The fork adds conversions verification code needs and upstream does not
#: provide. One branch is listed, ``certora-v1.23.1`` for 1.23.1, because that is the release the
#: fork is known to cover. Any other version blocks instead of inventing a branch name.
FIXED_FORK = ForkOverride(
    repo="https://github.com/Certora/fixed.git",
    crates=("fixed",),
    branches={"1.23.1": "certora-v1.23.1"},
    why=(
        "Use the Certora fork of fixed. It adds conversions the official library lacks, such as "
        "From<u64> for FixedU64, which verification code needs to build fixed-point values."
    ),
    upstream_failure=(
        "upstream fixed lacks the conversions, such as From<u64> for FixedU64, that harness code "
        "uses to build fixed-point values, so that code does not compile"
    ),
)

#: The forks written into a Solana project's workspace manifest.
SOLANA_OVERRIDES: tuple[ForkOverride, ...] = (ANCHOR_FORK, FIXED_FORK)


# ---------------------------------------------------------------------------------------------
# planning


def already_patched(manifest: Manifest) -> frozenset[str]:
    """The crates a workspace manifest's ``[patch.crates-io]`` table already redirects.

    Parsed, not searched. Projects write one ``[patch.crates-io]`` header with an inline table
    per crate. This module writes a ``[patch.crates-io.<crate>]`` sub-table. The two are the same
    TOML and share no text, so a search for either misses the other. A second entry for a key
    TOML already has is a manifest cargo refuses.

    :func:`plan_overrides` also checks the resolved graph, which is what cargo computed. This covers
    the case the graph cannot: a snapshot taken before the patch table was applied.
    """
    return frozenset(manifest.patch.get("crates-io", {}))


def _copies(copies: tuple[CratePackage, ...]) -> str:
    return ", ".join(
        f"{c.version} from {c.source.spelling if c.source is not None else 'this workspace'}"
        for c in copies
    )


def plan_overrides(
    workspace: Workspace,
    overrides: tuple[ForkOverride, ...] = SOLANA_OVERRIDES,
    already_redirected: frozenset[str] = frozenset(),
) -> ForkPlan | ForkRefused:
    """What redirecting ``workspace`` at the forks would change, without changing anything.

    Each crate lands in one of four outcomes:

    * inapplicable — not in the resolved graph.
    * already sourced — a workspace member, a path dependency, a git dependency, or a crate named
      in ``already_redirected``. Overriding it would replace a choice, which may already be this
      fork. The graph is what cargo computed. ``already_redirected``
      (:func:`already_patched`) covers a snapshot taken before the patch table was applied.
    * blocked — resolved more than once, since one patch entry redirects one of the copies; or
      resolved at a version the fork has no branch for, since skipping the fork leaves the
      failure it exists to fix (:attr:`ForkOverride.upstream_failure`).
    * overridden — redirected at the fork.

    Any blocked crate makes the whole result a :class:`ForkRefused`.
    """
    resolved_overrides: list[Override] = []
    blocked: list[Blocked] = []
    inapplicable: list[str] = []
    already: list[LeftAlone] = []

    for fork in overrides:
        for crate in fork.crates:
            copies = workspace.resolved(crate)
            if not copies:
                inapplicable.append(crate)
                continue
            if crate in already_redirected:
                already.append(AlreadyRedirected(crate=crate))
                continue
            resolved, *others = copies
            if others:
                blocked.append(
                    Blocked(
                        crate=crate,
                        problem=(
                            f"this project resolves {crate} more than once "
                            f"({_copies(copies)}), and one patch entry redirects one of them — "
                            f"the build would still link the other"
                        ),
                        resolution=(
                            f"unify the project on one {crate} release, then re-run — which "
                            f"dependent moves is a decision about the project"
                        ),
                    )
                )
                continue
            if not isinstance(resolved.source, RegistrySource):
                left = AlreadySourced(crate=crate, source=resolved.source, fork_repo=fork.repo)
                already.append(left)
                _log.info("%s already comes from %s; leaving it alone", crate, left.origin)
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
                            f"against {crate} {resolved.version} from crates.io: "
                            f"{fork.upstream_failure}"
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

    if blocked:
        return ForkRefused(tuple(blocked))
    return ForkPlan(tuple(resolved_overrides), tuple(inapplicable), tuple(already))


_NOT_DEPLOYED = (
    "Verification-only dependency replacements. These are NOT the deployed program's "
    "dependencies: a property proved against a fork is a property of the fork, and whether it "
    "carries over is a judgement about the specific difference."
)


def patch_tables(plan: ForkPlan) -> tuple[tuple[Override, AddTable], ...]:
    """The ``[patch.crates-io.<crate>]`` table this plan adds to the workspace manifest for each
    override.

    Each fork's reason is written once, in the table of its
    first crate: crates from one fork share one reason, and repeating it under each reads like
    two unrelated edits. The first table also says what these replacements are.
    """
    tables: list[tuple[Override, AddTable]] = []
    preamble = [Comment(line) for line in _wrapped(_NOT_DEPLOYED)] + [Comment("")]
    for why in dict.fromkeys(o.why for o in plan.overrides):
        first, *rest = [o for o in plan.overrides if o.why == why]
        explanation = [Comment(f"{o.crate} {o.version} -> {o.branch}") for o in (first, *rest)]
        explanation += [Comment(f"  {line}") for line in _wrapped(why)]
        body = (*preamble, *explanation, *first.redirect())
        tables.append((first, AddTable(("patch", "crates-io", first.crate), body)))
        preamble = []
        tables += [(o, AddTable(("patch", "crates-io", o.crate), o.redirect())) for o in rest]
    return tuple(tables)


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
