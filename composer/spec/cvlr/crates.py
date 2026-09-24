"""Which CVLR crates this build resolves, and the source trees they resolve to.

The version comes from the resolved graph. ``RUST_FORBIDDEN_READ`` hides ``Cargo.lock`` from
agents, and source for a different version than the build compiles is worse than no source.
:func:`resolve` reports each crate together with the directory it came from.

:mod:`composer.spec.cvlr_reference` records the one CVLR line this build supports.
:meth:`CvlrSources.gaps` reports where a project's graph and that line disagree, which
:mod:`composer.spec.cvlr.scaffold` refuses over before anything is written. It runs again after the
scaffold as a backstop: a :class:`Mismatched` at that point means a release reached the graph some
way the scaffold's gate does not see, such as a ``[patch]`` table.
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
class Mismatched:
    """The target builds a CVLR release other than the one this build supports.

    A refusal: everything the scaffold writes is :attr:`reference`'s, and pinning those crates
    beside :attr:`resolved` puts two CVLR generations in one graph.
    """

    crate: str
    #: What the reference set records.
    reference: str
    #: What this project's build resolves.
    resolved: str

    def describe(self) -> str:
        return (
            f"{self.crate} {self.resolved} is what this project builds; this build supports "
            f"{self.reference}"
        )


@dataclass(frozen=True)
class Absent:
    """The target does not depend on a reference-set crate at all.

    Not a refusal, and the reason the two are separate types. A project with no ``cvlr-solana`` is
    not on an old chain crate, it is on none — the ordinary state of a specialization it has no
    use for. Reported so a run can say which reference-set crates this project does not have.
    """

    crate: str
    reference: str

    def describe(self) -> str:
        return (
            f"{self.crate} is not a dependency of this project, so nothing that uses it "
            f"applies here"
        )


#: Where a build and the reference set differ. The two cases carry different fields because they
#: ask for different handling: one stops the run, the other is ordinary.
type Divergence = Mismatched | Absent


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

    def gaps(self, reference: ChainReference) -> tuple[Divergence, ...]:
        """Where this build and the reference set disagree.

        Only crates the reference set names are compared. A dependency the reference does not
        mention is not a disagreement. A capability with no published crate is recorded on the
        reference itself, as :class:`~composer.spec.cvlr_reference.UnpublishedCapability`.
        """
        resolved = {c.name: c.version for c in self.crates}
        return tuple(
            Absent(crate=r.name, reference=r.version)
            if (found := resolved.get(r.name)) is None
            else Mismatched(crate=r.name, reference=r.version, resolved=found)
            for r in reference.crates()
            if resolved.get(r.name) != r.version
        )

    def mismatched(self, reference: ChainReference) -> tuple[Mismatched, ...]:
        """Only the divergences that stop a run. See :class:`Absent` for why the rest do not."""
        return tuple(g for g in self.gaps(reference) if isinstance(g, Mismatched))


def resolve(workspace: Workspace) -> CvlrSources:
    """Every CVLR crate in ``workspace``'s resolved graph, in name order.

    Cargo's order is not stable. This list is written into run metadata, where a shuffle looks
    like a change.
    """
    return CvlrSources(tuple(sorted(workspace.family(CVLR_PREFIX), key=lambda c: c.name)))
