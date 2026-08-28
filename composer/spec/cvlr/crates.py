"""Which CVLR the project under verification actually builds against.

``docs/cvlr-backend-plan.md`` §5.5 inverts the "we have less documentation than EVM" premise: CVL
lives inside the Prover and cannot be read, while CVLR is *a cargo dependency of the analyzed
project*, so its complete source — every macro definition, every helper signature — is on disk in
the exact version the build resolves. That is the cheapest available answer to the cold-start
hallucination risk, and this module is the half of it that has to be right first: **the host
resolves the version, the agent never guesses it.**

Two things follow, and both are about being wrong rather than being absent.

*Reading the wrong CVLR is worse than reading none.* Source that disagrees with what the build
compiles is confidently wrong, and `RUST_FORBIDDEN_READ` hides ``Cargo.lock`` from the agents, so
they have no way to notice. :func:`resolve` therefore reports crate *and* on-disk root together —
there is no path here that yields a version without the tree it came from.

*The corpus has a version too.* :mod:`composer.spec.cvlr_reference` records which releases the
knowledge base was written and compile-gated against. When a target builds a different line, advice
recalled from that corpus may name symbols this project does not have —
``acc_infos_with_mem_layout`` is one of nine the unreleased 0.6 chain line removes. Reporting the
gap is not pedantry: it is the difference between an agent that checks the crate and an agent that
trusts recall.
"""

import dataclasses
from pathlib import Path

from composer.cargo.metadata import CratePackage, Workspace
from composer.spec.cvlr_reference import ChainReference

#: The crate-name prefix that spells the CVLR family. Its members are not declared anywhere — ``cvlr``
#: pulls in ``cvlr-asserts``, ``cvlr-log``, ``cvlr-nondet``, ``cvlr-mathint`` and more as ordinary
#: dependencies — so the family is recognized by name, which is also how a reader recognizes it.
CVLR_PREFIX = "cvlr"


@dataclasses.dataclass(frozen=True)
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


@dataclasses.dataclass(frozen=True)
class CvlrSources:
    """The CVLR crates this build resolves, with the source trees they resolve to."""

    crates: tuple[CratePackage, ...]

    @property
    def core(self) -> CratePackage | None:
        return next((c for c in self.crates if c.name == CVLR_PREFIX), None)

    def roots(self) -> tuple[Path, ...]:
        """The crate directories to mount read-only for the agents.

        Directories rather than files, and every family member rather than just ``cvlr``: a question
        about what ``cvlr_assert!`` expands to is answered in ``cvlr-asserts``, and an agent handed
        only the facade crate would find a re-export and stop.
        """
        return tuple(c.root for c in self.crates)

    def gaps(self, reference: ChainReference) -> tuple[VersionGap, ...]:
        """Where this build and the corpus's reference set disagree.

        Only the crates the reference set names are compared. A project depending on a CVLR crate the
        reference does not mention is not a disagreement — it is a capability the corpus is silent
        about, which the corpus already records for itself
        (:class:`~composer.spec.cvlr_reference.UnpublishedCapability`).
        """
        resolved = {c.name: c.version for c in self.crates}
        return tuple(
            VersionGap(crate=r.name, reference=r.version, resolved=resolved.get(r.name))
            for r in reference.crates()
            if resolved.get(r.name) != r.version
        )


def resolve(workspace: Workspace) -> CvlrSources:
    """Every CVLR crate in ``workspace``'s resolved graph, in name order.

    Sorted rather than left in cargo's order because this is stated in prompts and written into run
    metadata, and an unstable order there reads as a change when nothing changed.
    """
    return CvlrSources(tuple(sorted(workspace.family(CVLR_PREFIX), key=lambda c: c.name)))
