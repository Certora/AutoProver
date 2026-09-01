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
"""

import dataclasses
import logging

from composer.cargo.metadata import Workspace

_log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ForkOverride:
    """A dependency replaced by a maintained fork, matched to the version the target resolves.

    ``branches`` maps an exact resolved version to a branch name rather than deriving one from a
    pattern. The names do follow a pattern today, but a version with no branch is the case that
    matters: deriving would produce a plausible name, cargo would fail to fetch it, and the error a
    caller sees would be about git rather than about the fork not covering their Anchor. Naming the
    versions that exist means that case blocks with a sentence somebody can act on.
    """

    crate: str
    repo: str
    branches: tuple[tuple[str, str], ...]
    why: str

    def branch_for(self, version: str) -> str | None:
        return next((branch for v, branch in self.branches if v == version), None)


@dataclasses.dataclass(frozen=True)
class Blocked:
    """A reason the override cannot be applied, phrased for whoever has to resolve it."""

    crate: str
    problem: str
    resolution: str


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
    #: Overrides whose crate the target does not resolve. Reported rather than dropped: "Anchor was
    #: not replaced" is a fact a reader of a [3006] failure needs, and silence looks like success.
    inapplicable: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.overrides)


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
    crate="anchor-lang",
    repo="https://github.com/Certora/anchor.git",
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
        "verification-oriented changes besides. See docs/upstream-defects.md P1."
    ),
)

#: The overrides the Solana backend writes.
SOLANA_OVERRIDES: tuple[ForkOverride, ...] = (ANCHOR_FORK,)


# ---------------------------------------------------------------------------------------------
# planning


def plan_munge(
    workspace: Workspace, overrides: tuple[ForkOverride, ...] = SOLANA_OVERRIDES
) -> MungePlan:
    """What munging ``workspace`` would change, without changing anything.

    An override whose crate is not in the resolved graph is *inapplicable*, not blocked — most
    targets are not Anchor programs. An override whose crate resolves at a version the fork has no
    branch for is blocked, because the alternative is a build that silently keeps the boxing.
    """
    resolved_overrides: list[Override] = []
    blocked: list[Blocked] = []
    inapplicable: list[str] = []

    for override in overrides:
        resolved = workspace.resolved(override.crate)
        if resolved is None:
            inapplicable.append(override.crate)
            continue
        if resolved.source is None:
            # A path or git dependency: the project already decides where this crate comes from, and
            # overriding that would replace somebody's deliberate choice — including, quite possibly,
            # the fork itself.
            inapplicable.append(override.crate)
            _log.info(
                "%s comes from a path or git source already; leaving it alone", override.crate
            )
            continue
        branch = override.branch_for(resolved.version)
        if branch is None:
            blocked.append(
                Blocked(
                    crate=override.crate,
                    problem=(
                        f"this project resolves {override.crate} {resolved.version}, and "
                        f"{override.repo} has no branch recorded for it "
                        f"(have: {', '.join(v for v, _ in override.branches)})"
                    ),
                    resolution=(
                        f"pin {override.crate} to a covered version, or ask for a "
                        f"certora-v{resolved.version} branch on the fork and add it here — do not "
                        f"verify against the unforked crate, which cannot analyze an Anchor handler"
                    ),
                )
            )
            continue
        resolved_overrides.append(
            Override(
                crate=override.crate,
                version=resolved.version,
                repo=override.repo,
                branch=branch,
                why=override.why,
            )
        )

    return MungePlan(tuple(resolved_overrides), tuple(blocked), tuple(inapplicable))


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
    reasons = "".join(
        f"#\n# {o.crate} {o.version} -> {o.branch}\n"
        + "".join(f"#   {line}\n" for line in _wrapped(o.why))
        for o in plan.overrides
    )
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
