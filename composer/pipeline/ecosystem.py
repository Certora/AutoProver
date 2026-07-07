"""The ecosystem seam — the *front half* of the pipeline made parametric over the
blockchain/source domain (see ``docs/ecosystem-abstraction.md``).

An **ecosystem** bundles everything the shared analysis + property-extraction steps need
that is domain-specific: the system-model type they produce, the analysis/property prompt
templates, connectivity validation, the main-unit locator, and the per-unit enumeration.
It factors into a **language** facet (source-level conventions, shared across chains that
use the same language) and the **chain** facet (the platform model + prompts).

Phase 1 introduces the seam and captures today's behavior as ``EVM = SOLIDITY ⊕ evm`` —
a *move*, not a rewrite: the existing `SourceApplication`, prompt templates,
`_validate_connectivity`, `main_instance`, and unit enumeration become the EVM ecosystem's
members. The driver defaults to ``EVM``, so existing applications are unchanged. Solana /
Soroban chains and the prompt-fragment split land in later phases.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from composer.spec.context import SourceCode
from composer.spec.code_explorer import CODE_EXPLORER_SYS_PROMPT
from composer.spec.system_analysis import _validate_connectivity
from composer.spec.system_model import (
    AnyApplication,
    BaseApplication,
    ContractComponentInstance,
    ContractInstance,
    SourceApplication,
)
from composer.spec.util import FS_FORBIDDEN_READ

LanguageTag = Literal["solidity", "rust"]
ChainTag = Literal["evm", "solana", "soroban"]


@dataclass(frozen=True)
class PromptPair:
    """A (system prompt, initial prompt) template-name pair for one agent."""

    system: str
    initial: str


@dataclass(frozen=True)
class Language:
    """Source-language facet — shared by every chain that uses this language (e.g. the
    ``rust`` facet is shared by Solana and Soroban). Its members are captured here for the
    seam; consumers (the entry point's ``forbidden_read``, the ``code_explorer`` prompt) are
    rewired to read from it in a later phase, when a non-Solidity language first needs them."""

    name: LanguageTag
    default_forbidden_read: str
    code_explorer_prompt: str
    # The j2 partial with this language's failure modes (overflow, panics, …). Reserved for
    # the prompt-fragment split; unused while prompts are still monolithic.
    failure_modes_partial: str | None = None


@dataclass(frozen=True)
class Ecosystem:
    """A resolved ecosystem = a chain that carries its language. The driver consumes it to
    drive the shared front half without hardcoding any one domain."""

    name: ChainTag
    language: Language
    #: The pydantic model the analysis phase produces (a ``BaseApplication`` subtype).
    system_model: type[BaseApplication]
    #: Prompts for the system-analysis agent.
    analysis_prompts: PromptPair
    #: Prompts for the per-component property-inference agent.
    property_prompts: PromptPair
    #: Connectivity/shape validation of the analyzed model (retry feedback on failure).
    validate_analysis: Callable[[BaseApplication, str | None], str | None]
    #: Locate the target unit (the "main contract"/program) in the analyzed model.
    locate_main: Callable[[AnyApplication, SourceCode], ContractInstance]
    #: Enumerate the per-unit items the extraction phase infers properties for.
    units: Callable[[ContractInstance], list[ContractComponentInstance]]
    #: Domain-specific front-matter appended to the analysis input (was hardcoded in the driver).
    analysis_extra_input: Callable[[SourceCode], list[str | dict]]


# ---------------------------------------------------------------------------
# main-unit location (moved out of pipeline.core; re-exported there for callers)
# ---------------------------------------------------------------------------


def main_instance(app: AnyApplication, source: SourceCode) -> ContractInstance:
    """Locate the application's main contract — the one whose solidity identifier matches
    ``source.contract_name`` — and return a ``ContractInstance`` pointing at it. Backends call
    this from ``prepare_system`` to seed the per-component loop; component analysis should
    already have guaranteed the contract is present (via ``expected_main_id``)."""
    for i, c in enumerate(app.contract_components):
        if c.solidity_identifier == source.contract_name:
            return ContractInstance(i, app)
    raise ValueError(f"main contract {source.contract_name!r} not found in analyzed application")


# ---------------------------------------------------------------------------
# The EVM ecosystem (= today's behavior)
# ---------------------------------------------------------------------------


def _evm_units(main: ContractInstance) -> list[ContractComponentInstance]:
    return [
        ContractComponentInstance(_contract=main, ind=i)
        for i in range(len(main.contract.components))
    ]


def _evm_analysis_extra_input(source: SourceCode) -> list[str | dict]:
    return [
        f"The main entry point of this application has been explicitly identified as "
        f"{source.contract_name} at relative path {source.relative_path}. "
        "Your output MUST contain an explicit contract instance with this solidity identifier."
    ]


SOLIDITY = Language(
    name="solidity",
    default_forbidden_read=FS_FORBIDDEN_READ,
    code_explorer_prompt=CODE_EXPLORER_SYS_PROMPT,
)

EVM = Ecosystem(
    name="evm",
    language=SOLIDITY,
    system_model=SourceApplication,
    analysis_prompts=PromptPair(
        "application_analysis_system.j2", "application_analysis_prompt.j2"
    ),
    property_prompts=PromptPair(
        "property_analysis_system_prompt.j2", "property_analysis_prompt.j2"
    ),
    validate_analysis=_validate_connectivity,
    locate_main=main_instance,
    units=_evm_units,
    analysis_extra_input=_evm_analysis_extra_input,
)


#: Registry of available ecosystems, keyed by chain tag. Solana/Soroban register here in
#: later phases.
ECOSYSTEMS: dict[ChainTag, Ecosystem] = {"evm": EVM}
