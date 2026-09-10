"""The chains the CVLR backend verifies, one value each.

Bundles only what must travel together: the ecosystem fixes the unit type, and the prompts render
their component context from that same type, so pairing Soroban's ecosystem with Solana's prompts is
a type error. Everything else is keyed by :attr:`CvlrChain.tag` through the existing per-chain
registries (reference set, scaffold policy, ``PROJECT_TOOLCHAINS``, prover CLI).
"""

import dataclasses

from composer.pipeline.core import SharedUnit
from composer.pipeline.ecosystem import SOLANA, SOROBAN, ChainTag, Ecosystem
from composer.spec.cvlr.author import CvlrPromptBuilder, solana_prompts, soroban_prompts
from composer.spec.cvlr.guidance import SOLANA_CVLR_GUIDANCE, SOROBAN_CVLR_GUIDANCE
from composer.spec.solana.model import (
    SolanaApplication,
    SolanaComponentInstance,
    SolanaProgramInstance,
    protocol_unit,
)
from composer.spec.soroban.model import (
    SorobanApplication,
    SorobanComponentInstance,
    SorobanContractInstance,
)
from composer.spec.system_model import BaseApplication, FeatureUnit


@dataclasses.dataclass(frozen=True)
class CvlrChain[App: BaseApplication, Main, U: FeatureUnit]:
    """One chain, as the CVLR backend and its entry point consume it."""

    ecosystem: Ecosystem[App, Main, U]
    #: What the property extractor is told about the prover (:mod:`composer.spec.cvlr.guidance`).
    guidance: str
    prompts: CvlrPromptBuilder[U]
    #: Whether the author gets the program editor. Its charter is written for Solana, and an editor
    #: briefed on the wrong chain is worse than none.
    program_editing: bool
    #: The unit properties several components state are hoisted into, or ``None`` to formalize
    #: each where it was extracted (:class:`~composer.pipeline.core.SharedUnit`).
    shared_unit: SharedUnit[U, Main] | None

    @property
    def tag(self) -> ChainTag:
        """The chain's key in every per-chain registry, and the prover app it submits to."""
        return self.ecosystem.name


SOLANA_CVLR: CvlrChain[SolanaApplication, SolanaProgramInstance, SolanaComponentInstance] = (
    CvlrChain(
        ecosystem=SOLANA,
        guidance=SOLANA_CVLR_GUIDANCE,
        prompts=solana_prompts,
        program_editing=True,
        # A CVLR rule drives the handlers it names; nothing checks a property against every handler
        # by itself, as a CVL invariant is. Properties several components state go to one unit.
        shared_unit=SharedUnit(
            make=protocol_unit,
            entry_points=lambda unit: [i.name for i in unit.instructions],
        ),
    )
)

SOROBAN_CVLR: CvlrChain[SorobanApplication, SorobanContractInstance, SorobanComponentInstance] = (
    CvlrChain(
        ecosystem=SOROBAN,
        guidance=SOROBAN_CVLR_GUIDANCE,
        prompts=soroban_prompts,
        program_editing=False,
        # The Soroban model has no protocol-wide unit to hoist into yet.
        shared_unit=None,
    )
)
