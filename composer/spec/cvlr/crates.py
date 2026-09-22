"""Which CVLR crates this build resolves, and the source trees they resolve to.

The version comes from the resolved graph. ``RUST_FORBIDDEN_READ`` hides ``Cargo.lock`` from
agents, and source for a different version than the build compiles is worse than no source.
:func:`resolve` reports each crate together with the directory it came from.

:mod:`composer.spec.cvlr_reference` records the releases the knowledge corpus was written against.
A project on another version may not have symbols that corpus names. :meth:`CvlrSources.gaps`
reports the disagreement. The project's own pin is left as it is.
"""

from dataclasses import dataclass
from pathlib import Path

from composer.cargo.metadata import CratePackage, Workspace
from composer.spec.cvlr_reference import ChainReference

#: The crate-name prefix that spells the CVLR family. Its members are not declared anywhere — ``cvlr``
#: pulls in ``cvlr-asserts``, ``cvlr-log``, ``cvlr-nondet``, ``cvlr-mathint`` and more as ordinary
#: dependencies — so the family is recognized by name, which is also how a reader recognizes it.
CVLR_PREFIX = "cvlr"


@dataclass(frozen=True)
class VersionGap:
    """The target builds a CVLR release the knowledge corpus was not written against."""

    crate: str
    #: What the reference set — and therefore the corpus and its compile gate — records.
    reference: str
    #: What this project's build resolves. ``None`` when the project does not depend on it at all,
    #: which is a different statement: a target with no ``cvlr-solana`` is not on an old chain crate,
    #: it is on none.
    resolved: str | None

    def describe(self) -> str:
        if self.resolved is None:
            return (
                f"{self.crate} is not a dependency of this project; corpus guidance that uses it "
                f"does not apply here"
            )
        return (
            f"{self.crate} {self.resolved} is what this project builds; the knowledge corpus was "
            f"written against {self.reference}"
        )


@dataclass(frozen=True)
class CvlrSources:
    """The CVLR crates this build resolves, with the source trees they resolve to."""

    crates: tuple[CratePackage, ...]

    @property
    def core(self) -> CratePackage | None:
        return next((c for c in self.crates if c.name == CVLR_PREFIX), None)

    def roots(self) -> tuple[Path, ...]:
        """The crate directories, one per family member.

        ``cvlr_assert!`` expands in ``cvlr-asserts``. The ``cvlr`` crate only re-exports it.
        """
        return tuple(c.root for c in self.crates)

    def gaps(self, reference: ChainReference) -> tuple[VersionGap, ...]:
        """Where this build and the corpus's reference set disagree.

        Only crates the reference set names are compared. A dependency the reference does not
        mention is not a disagreement. A capability with no published crate is recorded on the
        reference itself, as :class:`~composer.spec.cvlr_reference.UnpublishedCapability`.
        """
        resolved = {c.name: c.version for c in self.crates}
        return tuple(
            VersionGap(crate=r.name, reference=r.version, resolved=resolved.get(r.name))
            for r in reference.crates()
            if resolved.get(r.name) != r.version
        )


def resolve(workspace: Workspace) -> CvlrSources:
    """Every CVLR crate in ``workspace``'s resolved graph, in name order.

    Cargo's order is not stable. This list is written into run metadata, where a shuffle looks
    like a change.
    """
    return CvlrSources(tuple(sorted(workspace.family(CVLR_PREFIX), key=lambda c: c.name)))
