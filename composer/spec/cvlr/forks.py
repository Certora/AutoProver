"""Redirect a dependency at a Certora-maintained fork when the crates.io crate cannot be analyzed.

Upstream ``anchor_lang::error::Error`` boxes its payload. The Solana Prover rejects the
``Box::new`` of that stack-built struct as [3006], "illegal store of a stack pointer", on Anchor
dispatch and on handlers that use ``?``. The fork's ``Error`` is unboxed. The same fork simplifies
``require!``, silences ``emit!``, and adds public constructors. ``anchor-spl`` there adds
``new_unchecked`` for ``TokenAccount`` and ``Mint``, whose fields upstream keeps private.

``Certora/anchor`` has a branch per upstream release it covers. ``Certora/fixed`` is the same kind
of fork for the ``fixed`` crate: it adds conversions upstream does not provide, such as
``From<u64>`` for ``FixedU64``.

Branches are an explicit list, not a pattern. A version with no branch blocks the plan. A derived
name would send cargo after a branch that does not exist, and the error would be about git.

The recommended starting template does not mention these forks. A project scaffolded from it stays
on the crates.io crates and hits [3006] with nothing pointing at a fork.
"""

import logging
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass

from composer.cargo.metadata import GitSource, OtherSource, RegistrySource, Workspace

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ForkOverride:
    """A repository of verification-oriented forks, and the crates in it a target may need.

    ``crates`` is more than one name because a fork is a workspace. ``Certora/anchor`` publishes
    ``anchor-lang`` and ``anchor-spl`` from one branch, and a target that uses both needs both
    redirected. Patching only ``anchor-lang`` clears [3006] (the boxing is in
    ``anchor_lang::error``) and leaves ``anchor-spl`` upstream. Its ``TokenAccount`` and ``Mint``
    are newtypes with a private field. The fork adds ``new_unchecked`` for those, so a harness can
    build a token account.

    ``branches`` maps an exact resolved version to a branch name. The names follow a pattern, but a
    missing branch is the case that matters: deriving one would produce a plausible name, cargo
    would fail to fetch it, and the error would be about git. Listing the versions that exist
    makes that case a message about coverage.
    """

    repo: str
    crates: tuple[str, ...]
    branches: Mapping[str, str]
    why: str

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
        return isinstance(self.source, GitSource) and _repo_key(self.fork_repo) in _repo_key(
            self.source.spelling
        )

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


def _repo_key(url: str) -> str:
    """A git URL reduced to the part two spellings of one repository share.

    ``cargo metadata`` reports a patched dependency as
    ``git+https://github.com/Certora/anchor.git?branch=certora-v0.31.1#<sha>``. The comparison
    drops the scheme prefix, the query, and the fragment.
    """
    return url.removeprefix("git+").split("?")[0].split("#")[0].removesuffix(".git").lower()


@dataclass(frozen=True)
class Override:
    """One dependency's replacement, resolved against a particular target."""

    crate: str
    version: str
    repo: str
    branch: str
    why: str

    def manifest_addition(self) -> str:
        """The ``[patch.crates-io]`` entry that redirects the graph at the fork.

        A branch, not a commit. The lockfile records the commit, so the build stays reproducible
        without editing this file every time the fork moves.
        """
        return (
            f"\n[patch.crates-io.{self.crate}]\n"
            f'git = "{self.repo}"\n'
            f'branch = "{self.branch}"\n'
        )


@dataclass(frozen=True)
class ForkPlan:
    """What redirecting this target at the forks would change. Empty when nothing needs it."""

    overrides: tuple[Override, ...] = ()
    blocked: tuple[Blocked, ...] = ()
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


class ForkBlocked(RuntimeError):
    """A blocked plan was applied. Carries every reason, not the first."""

    def __init__(self, blocked: tuple[Blocked, ...]) -> None:
        self.blocked = blocked
        super().__init__("; ".join(f"{b.crate}: {b.problem}" for b in blocked))


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
        "Upstream anchor_lang::error::Error boxes its payload, and the Solana Prover rejects the "
        "resulting Box::new of a stack-built struct as [3006] 'illegal store of a stack pointer' — "
        "on every path through Anchor dispatch. The fork's Error is unboxed, and carries other "
        "verification-oriented changes besides: anchor-spl gains public new_unchecked constructors "
        "for TokenAccount and Mint, whose upstream newtypes a harness cannot otherwise build."
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
        "The Certora-maintained fork of `fixed` carries conversions verification code needs (e.g. "
        "From<u64> for FixedU64) that upstream does not provide, so a harness over a program using "
        "fixed-point arithmetic cannot construct its own values without it."
    ),
)

#: The forks written into a Solana project's workspace manifest.
SOLANA_OVERRIDES: tuple[ForkOverride, ...] = (ANCHOR_FORK, FIXED_FORK)


# ---------------------------------------------------------------------------------------------
# planning


def already_patched(manifest_text: str) -> frozenset[str]:
    """The crates a workspace manifest's ``[patch.crates-io]`` table already redirects.

    Parsed, not searched. Projects write one ``[patch.crates-io]`` header with an inline table
    per crate. This module writes a ``[patch.crates-io.<crate>]`` sub-table. The two are the same
    TOML and share no text, so a search for either misses the other. A second entry for a key
    TOML already has is a manifest cargo refuses.

    :func:`plan_overrides` also checks the resolved graph, which is what cargo computed. This covers
    the case the graph cannot: a snapshot taken before the patch table was applied.
    """
    try:
        parsed = tomllib.loads(manifest_text)
    except tomllib.TOMLDecodeError:
        # cargo parsed this manifest to produce the graph. A failure here means this reader
        # disagrees with cargo. Log it and continue; the graph still shows the redirect.
        _log.warning("could not parse the workspace manifest to look for existing patches")
        return frozenset()
    patch = parsed.get("patch")
    if not isinstance(patch, dict):
        return frozenset()
    crates_io = patch.get("crates-io")
    return frozenset(crates_io) if isinstance(crates_io, dict) else frozenset()


def plan_overrides(
    workspace: Workspace,
    overrides: tuple[ForkOverride, ...] = SOLANA_OVERRIDES,
    already_redirected: frozenset[str] = frozenset(),
) -> ForkPlan:
    """What redirecting ``workspace`` at the forks would change, without changing anything.

    Each crate lands in one of four outcomes:

    * inapplicable — not in the resolved graph.
    * already sourced — a workspace member, a path dependency, a git dependency, or a crate named
      in ``already_redirected``. Overriding it would replace a choice, which may already be this
      fork. The graph is what cargo computed. ``already_redirected``
      (:func:`already_patched`) covers a snapshot taken before the patch table was applied.
    * blocked — a version the fork has no branch for. Skipping it would leave the boxed error
      in the build.
    * overridden — redirected at the fork.
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

    return ForkPlan(
        tuple(resolved_overrides), tuple(blocked), tuple(inapplicable), tuple(already)
    )


def manifest_additions(plan: ForkPlan) -> str:
    """The ``[patch.crates-io]`` section this plan needs in the workspace manifest.

    A blocked plan raises instead of emitting a partial section. Replacing one of two crates
    leaves a build whose failure has two causes.
    """
    if plan.blocked:
        raise ForkBlocked(plan.blocked)
    if not plan.overrides:
        return ""
    header = (
        "\n# === Certora CVLR — added by AutoProver ===\n"
        "# Verification-only dependency replacements. These are NOT the deployed program's\n"
        "# dependencies: a property proved against a fork is a property of the fork, and whether it\n"
        "# carries over is a judgement about the specific difference.\n"
    )
    # Crates from one fork share one reason. Repeating the paragraph under each of them
    # reads like two unrelated edits.
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
