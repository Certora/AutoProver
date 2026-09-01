"""Munging a resolved dependency so the prover can analyze it.

"Munge" is Certora's word for a modified copy of the code under verification, kept beside the
original so the difference is reviewable. This module is the narrow, mechanical version of it: a
declared set of exact textual edits applied to a copy of a dependency's *registry source*, wired in
with ``[patch.crates-io]``.

The motivating case is `docs/upstream-defects.md` P1. ``anchor_lang::error::Error`` boxes its payload,
and the Solana Prover rejects the resulting ``Box::new`` of a stack-built struct as
**[3006] "illegal store of a stack pointer"** — on every path through Anchor dispatch and every
handler that uses ``?``. Measured against a real program: with the boxing, a rule that calls a handler
cannot be analyzed at all; with it removed, the same rule produces a counterexample. Un-inlining and
summarizing were both tried and neither works (P1's own list), so this is the only lever that is ours.

Three decisions are worth stating because each is the alternative to something that goes wrong
quietly.

**The source comes from the target's own resolved graph, not from this wheel.** ``cargo metadata``
reports each dependency's manifest path, which for a registry crate is the unpacked source directory.
So the munge patches the exact version the target builds against, ships no Rust, and cannot drift from
what the project resolves. A vendored copy here would be a second version to keep current, and would
be silently wrong the moment a target pinned a different one.

**An edit that does not apply is an error, never a skip.** Every :class:`Replacement` states how many
times it must match, and a mismatch blocks the whole munge. A patch that half-applies produces a build
that looks munged and is not — and the failure it then produces is the original defect, which reads as
"the munge does not work" rather than "the munge did not happen".

**Versions are declared, not assumed.** A patch names the release lines it was written against. Text
that happens to match in a version nobody checked is not evidence that the patch is right there.
"""

import dataclasses
import logging
import shutil
from pathlib import Path

from composer.cargo.metadata import Workspace

_log = logging.getLogger(__name__)

#: Where munged copies live, relative to the workspace root. Beside the project rather than in a
#: temp dir because ``[patch.crates-io]`` needs a stable relative path, and because a munge somebody
#: cannot look at is a munge nobody can review. Gitignored by the scaffold.
MUNGE_DIR = Path(".cvlr_munge")

#: Directories never copied out of a registry source. ``target`` should not be there at all, but a
#: crate unpacked by an older cargo sometimes carries one, and copying it makes the munge slow and
#: the diff unreadable.
_NOT_COPIED = shutil.ignore_patterns("target", ".git")


@dataclasses.dataclass(frozen=True)
class Replacement:
    """One exact textual edit, and how many times it must match.

    ``occurrences`` is required rather than defaulted to "all": the two failure modes of a textual
    patch are matching nothing and matching more than intended, and a count catches both.
    """

    old: str
    new: str
    occurrences: int = 1


@dataclasses.dataclass(frozen=True)
class CratePatch:
    """What to change in one dependency, and why.

    ``why`` is not decoration. A munged dependency is the least obvious thing in a verification
    project — a reader who finds ``.cvlr_munge`` needs to know what was changed and what it bought,
    and this string is what the plan prints and what the copy's own README carries.
    """

    crate: str
    #: Version prefixes this patch was written against, e.g. ``("0.31.", "0.30.")``. A resolved
    #: version outside them blocks rather than proceeding on textual luck.
    applies_to: tuple[str, ...]
    #: Crate-relative source path to the edits it needs.
    edits: tuple[tuple[str, tuple[Replacement, ...]], ...]
    why: str


@dataclasses.dataclass(frozen=True)
class Blocked:
    """A reason the munge cannot proceed, phrased for whoever has to resolve it."""

    crate: str
    problem: str
    resolution: str


@dataclasses.dataclass(frozen=True)
class MungedCrate:
    """One crate's munge, resolved against a particular target."""

    crate: str
    version: str
    source: Path
    #: Where the copy goes, relative to the workspace root.
    destination: Path
    patch: CratePatch

    def manifest_addition(self) -> str:
        """The ``[patch.crates-io]`` entry that redirects the graph at this copy."""
        # Forward slashes and double quotes: a TOML basic string, rather than whatever `repr`
        # would produce for a `Path` on the host that happens to be running.
        path = str(self.destination).replace("\\", "/")
        return f'\n[patch.crates-io.{self.crate}]\npath = "{path}"\n'


@dataclasses.dataclass(frozen=True)
class MungePlan:
    """What munging this target would do. Empty when nothing needs it, which is the common case."""

    crates: tuple[MungedCrate, ...] = ()
    blocked: tuple[Blocked, ...] = ()
    #: Patches whose crate the target does not resolve. Reported rather than dropped: "Anchor was
    #: not munged" is a fact a reader of a [3006] failure needs, and silence looks like success.
    inapplicable: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.crates)


class MungeBlocked(RuntimeError):
    """A munge was applied that could not be. Carries every reason, not the first."""

    def __init__(self, blocked: tuple[Blocked, ...]) -> None:
        self.blocked = blocked
        super().__init__("; ".join(f"{b.crate}: {b.problem}" for b in blocked))


# ---------------------------------------------------------------------------------------------
# the patches


#: Remove the boxing from ``anchor_lang::error::Error``.
#:
#: Every edit is in ``src/error.rs`` and the enum's own match arms need no change, because they bind
#: the payload by name and ``Box<T>`` derefs to ``T`` — so unboxing is invisible to them. The macro
#: crates need no change either: ``anchor-attribute-error`` constructs ``AnchorError { .. }`` and
#: relies on ``From``, and there is no ``Box::new`` anywhere in ``anchor-syn``'s codegen.
#:
#: The cost is that ``Error`` grows from 16 bytes to the size of ``AnchorError`` (160 on this line),
#: so every ``Result<(), Error>`` in the program gets bigger. That is a real change to the code under
#: verification and the reason this is a munge rather than a fix: it is acceptable because the
#: verification build is not the deployed build, and it must never be presented as if the deployed
#: program had this shape.
ANCHOR_UNBOX = CratePatch(
    crate="anchor-lang",
    # 0.30 through 0.32, verified edit-by-edit against the local registry sources by
    # `test_the_patch_matches_the_real_crate_if_it_is_on_this_machine`, which is how 0.29 came off
    # this list: it has no `From<TryFromIntError> for Error`, so the last two edits match nothing
    # there. A 0.29 target blocks with that named rather than being patched partially.
    applies_to=("0.30.", "0.31.", "0.32."),
    why=(
        "anchor_lang::error::Error boxes its payload, and the Solana Prover rejects the resulting "
        "Box::new of a stack-built struct as [3006] 'illegal store of a stack pointer' — on every "
        "path through Anchor dispatch. See docs/upstream-defects.md P1."
    ),
    edits=(
        (
            "src/error.rs",
            (
                Replacement(
                    "    AnchorError(Box<AnchorError>),",
                    "    AnchorError(AnchorError),",
                ),
                Replacement(
                    "    ProgramError(Box<ProgramErrorWithOrigin>),",
                    "    ProgramError(ProgramErrorWithOrigin),",
                ),
                Replacement("Self::AnchorError(Box::new(ae))", "Self::AnchorError(ae)"),
                Replacement(
                    "Self::ProgramError(Box::new(program_error.into()))",
                    "Self::ProgramError(program_error.into())",
                ),
                Replacement(
                    "Error::ProgramError(Box::new(ProgramError::from(error).into()))",
                    "Error::ProgramError(ProgramError::from(error).into())",
                ),
                Replacement("Self::ProgramError(Box::new(pe))", "Self::ProgramError(pe)"),
                # This one opens a block, so its closing `}))` loses a level too. Both halves are
                # separate replacements on purpose: if upstream reformats the literal, the count
                # check fails here rather than producing a file that does not parse.
                Replacement(
                    "Self::AnchorError(Box::new(AnchorError {",
                    "Self::AnchorError(AnchorError {",
                ),
                Replacement(
                    "            compared_values: None,\n        }))\n    }\n}",
                    "            compared_values: None,\n        })\n    }\n}",
                ),
            ),
        ),
    ),
)

#: The patches the Solana backend applies, in order. A tuple rather than a registry keyed by crate:
#: the order a munge is applied in is part of its meaning once two patches touch one crate.
SOLANA_PATCHES: tuple[CratePatch, ...] = (ANCHOR_UNBOX,)


# ---------------------------------------------------------------------------------------------
# planning and applying


def _version_covered(version: str, applies_to: tuple[str, ...]) -> bool:
    return any(version.startswith(prefix) for prefix in applies_to)


def plan_munge(
    workspace: Workspace, patches: tuple[CratePatch, ...] = SOLANA_PATCHES
) -> MungePlan:
    """What munging ``workspace`` would change, without changing anything.

    A patch whose crate is not in the resolved graph is *inapplicable*, not blocked — most targets
    are not Anchor programs. A patch whose crate resolves at a version the patch does not name is
    blocked, because proceeding would apply edits to a file nobody checked.
    """
    munged: list[MungedCrate] = []
    blocked: list[Blocked] = []
    inapplicable: list[str] = []

    for patch in patches:
        resolved = workspace.resolved(patch.crate)
        if resolved is None:
            inapplicable.append(patch.crate)
            continue
        if not _version_covered(resolved.version, patch.applies_to):
            blocked.append(
                Blocked(
                    crate=patch.crate,
                    problem=(
                        f"this project resolves {patch.crate} {resolved.version}, and the munge was "
                        f"written against {', '.join(f'{p}x' for p in patch.applies_to)}"
                    ),
                    resolution=(
                        f"check the patch against {resolved.version} and add its line to "
                        f"`applies_to`, or pin {patch.crate} to a covered line"
                    ),
                )
            )
            continue
        source = resolved.manifest_path.parent
        if not (source / patch.edits[0][0]).is_file():
            blocked.append(
                Blocked(
                    crate=patch.crate,
                    problem=(
                        f"{patch.crate} {resolved.version} resolves to {source}, which has no "
                        f"{patch.edits[0][0]} — so it is not an unpacked registry source"
                    ),
                    resolution=(
                        "a path or git dependency cannot be munged this way; patch it in place or "
                        "remove the override"
                    ),
                )
            )
            continue
        munged.append(
            MungedCrate(
                crate=patch.crate,
                version=resolved.version,
                source=source,
                destination=MUNGE_DIR / patch.crate,
                patch=patch,
            )
        )

    return MungePlan(tuple(munged), tuple(blocked), tuple(inapplicable))


def _patched(text: str, edits: tuple[Replacement, ...], where: str) -> str:
    for edit in edits:
        found = text.count(edit.old)
        if found != edit.occurrences:
            raise MungeBlocked(
                (
                    Blocked(
                        crate=where,
                        problem=(
                            f"expected {edit.occurrences} occurrence(s) of {edit.old!r} and found "
                            f"{found}"
                        ),
                        resolution=(
                            "the upstream source changed; re-derive the munge against this version "
                            "rather than loosening the match"
                        ),
                    ),
                )
            )
        text = text.replace(edit.old, edit.new)
    return text


_README = """\
# Munged dependencies — generated, do not edit

Modified copies of this project's own resolved dependencies, used only by the verification build.
`../Cargo.toml` redirects the dependency graph here with `[patch.crates-io]`.

**These are not the deployed program's dependencies.** A property proved against a munged copy is a
property of the munged copy; whether it carries over is a judgement about the specific change, and
each one records what it was and why below.

{entries}
"""


def apply_munge(plan: MungePlan, root: Path) -> tuple[Path, ...]:
    """Carry out ``plan`` under ``root``, returning the copies it made.

    Refuses a plan with anything :class:`Blocked`, for the same reason the scaffold does: a
    half-munged project turns the next failure into a question with two candidate answers.

    A destination that already exists is replaced. The copies are derived state — the registry source
    plus a declared patch — so re-running must converge rather than accumulate, and an
    interrupted previous run must not leave a partly-copied crate in place.
    """
    if plan.blocked:
        raise MungeBlocked(plan.blocked)

    made: list[Path] = []
    for crate in plan.crates:
        destination = root / crate.destination
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(crate.source, destination, ignore=_NOT_COPIED)
        # Registry sources are checked out read-only.
        for path in destination.rglob("*"):
            if path.is_file():
                path.chmod(path.stat().st_mode | 0o200)

        for relative, edits in crate.patch.edits:
            target = destination / relative
            target.write_text(
                _patched(target.read_text(), edits, f"{crate.crate} {crate.version}")
            )
        made.append(crate.destination)
        _log.info("munged %s %s into %s", crate.crate, crate.version, crate.destination)

    if made:
        entries = "\n".join(
            f"## {c.crate} {c.version}\n\n{c.patch.why}\n\nCopied from `{c.source}`."
            for c in plan.crates
        )
        (root / MUNGE_DIR / "README.md").write_text(_README.format(entries=entries))

    return tuple(made)


def manifest_additions(plan: MungePlan) -> str:
    """The ``[patch.crates-io]`` section this plan needs in the workspace manifest."""
    if not plan.crates:
        return ""
    header = (
        "\n# === Certora CVLR munge — added by AutoProver ===\n"
        "# Verification-only replacements for resolved dependencies; see .cvlr_munge/README.md.\n"
    )
    return header + "".join(c.manifest_addition() for c in plan.crates)
