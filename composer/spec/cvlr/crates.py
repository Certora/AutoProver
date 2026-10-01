"""Which CVLR crates this build resolves, and the source trees they resolve to.

The version comes from the resolved graph. ``RUST_FORBIDDEN_READ`` hides ``Cargo.lock`` from
agents, and source for a different version than the build compiles is worse than no source.
:meth:`CvlrSources.of` reports each crate together with the directory it came from.

:mod:`composer.spec.cvlr.reference` records the one CVLR line this build supports.
:meth:`CvlrSources.gaps` reports where a project's graph and that line disagree, which
:mod:`composer.spec.cvlr.scaffold` refuses over before anything is written. It runs again after the
scaffold as a backstop: a :class:`Mismatched` at that point means a release reached the graph some
way the scaffold's gate does not see, such as a ``[patch]`` table.
"""

from dataclasses import dataclass
from typing import Self

from composer.cargo.metadata import CratePackage, Workspace
from composer.spec.cvlr.reference import ChainReference

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

    @classmethod
    def of(cls, workspace: Workspace) -> Self:
        """Every CVLR crate in ``workspace``'s resolved graph, in name order.

        Cargo's order is not stable. This list is written into run metadata, where a shuffle looks
        like a change. One version can resolve twice, from two sources or two paths, so the
        manifest path, which is unique per package, breaks the tie.
        """
        return cls(
            tuple(
                sorted(
                    workspace.family(CVLR_PREFIX),
                    key=lambda c: (c.name, c.version, c.manifest_path),
                )
            )
        )

    def gaps(self, reference: ChainReference) -> tuple[Divergence, ...]:
        """Where this build and the reference set disagree.

        Only crates the reference set names are compared. A dependency the reference does not
        mention is not a disagreement. A capability with no published crate is recorded on the
        reference itself, as :class:`~composer.spec.cvlr.reference.UnpublishedCapability`.
        A crate the graph resolves more than once is one :class:`Mismatched` per copy off the
        reference.
        """
        gaps: list[Divergence] = []
        for r in reference.crates():
            versions = [c.version for c in self.crates if c.name == r.name]
            if not versions:
                gaps.append(Absent(crate=r.name, reference=r.version))
            gaps += [
                Mismatched(crate=r.name, reference=r.version, resolved=v)
                for v in versions
                if v != r.version
            ]
        return tuple(gaps)

    def mismatched(self, reference: ChainReference) -> tuple[Mismatched, ...]:
        """Only the divergences that stop a run. See :class:`Absent` for why the rest do not."""
        return tuple(g for g in self.gaps(reference) if isinstance(g, Mismatched))
