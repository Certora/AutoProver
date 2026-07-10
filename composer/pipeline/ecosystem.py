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
from typing import Any, Callable, Literal

from composer.spec.context import SourceCode
from composer.spec.code_explorer import CODE_EXPLORER_SYS_PROMPT
from composer.spec.system_analysis import _validate_connectivity
from composer.spec.system_model import (
    AnyApplication,
    BaseApplication,
    ContractComponentInstance,
    ContractInstance,
    FeatureUnit,
    SolidityIdentifier,
    SourceApplication,
)
from composer.spec.solana.model import (
    SolanaApplication,
    SolanaInstructionInstance,
    SolanaInvariantUnit,
    SolanaProgramInstance,
)
from composer.spec.types import PropertyFormulation
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
class Ecosystem[App: BaseApplication, Main, Unit: FeatureUnit]:
    """A resolved ecosystem = a chain that carries its language. The driver consumes it to
    drive the shared front half without hardcoding any one domain.

    Generic over ``App`` (the analyzed system-model type), ``Main`` (the located main-unit
    wrapper), and ``Unit`` (the per-unit item the extraction phase iterates). A backend is
    paired with an ecosystem by these types: ``run_pipeline`` ties
    ``PipelineBackend[..., App, Main, Unit]`` to ``Ecosystem[App, Main, Unit]``, so the analyzed
    model, the main-unit, and the per-unit values flow through without casts. EVM binds
    ``(SourceApplication, ContractInstance, ContractComponentInstance)``; Solana binds its own."""

    name: ChainTag
    language: Language
    #: The pydantic model the analysis phase produces.
    system_model: type[App]
    #: Prompts for the system-analysis agent.
    analysis_prompts: PromptPair
    #: Prompts for the per-component property-inference agent.
    property_prompts: PromptPair
    #: Connectivity/shape validation of the analyzed model (retry feedback on failure).
    #: Typed over ``BaseApplication`` (not ``App``): the validator receives the produced model
    #: and narrows internally (as ``_validate_connectivity`` does), and this keeps it assignable
    #: to ``run_component_analysis``'s ``validate`` parameter without a contravariance clash.
    validate_analysis: Callable[[BaseApplication, SolidityIdentifier | None], str | None]
    #: Locate the target unit (the "main contract"/program) in the analyzed model.
    locate_main: Callable[[App, SourceCode], Main]
    #: Enumerate the per-unit items the extraction phase infers properties for.
    units: Callable[[Main], list[Unit]]
    #: Domain-specific front-matter appended to the analysis input (was hardcoded in the driver).
    analysis_extra_input: Callable[[SourceCode], list[str | dict]]

    # -- Extraction strategy (docs/crucible-unit-granularity.md) -------------------------
    #: When True, the driver runs ONE whole-program property extraction (context =
    #: ``extraction_unit(main)``) and fans each resulting property out into its own unit
    #: via ``property_unit`` — one harness + verdict per property. When False (the EVM
    #: default) it extracts per ``units(main)`` (one batch per component). Solana uses
    #: global extraction so the fuzzer gets whole-program invariants, one run per invariant.
    global_extraction: bool = False
    #: The whole-program context the global extraction reads (only used when global_extraction).
    extraction_unit: Callable[[Main], FeatureUnit] | None = None
    #: Build the per-property unit from (main, property, index) (only used when global_extraction).
    property_unit: Callable[[Main, PropertyFormulation, int], FeatureUnit] | None = None


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

EVM: Ecosystem[SourceApplication, ContractInstance, ContractComponentInstance] = Ecosystem(
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


# ---------------------------------------------------------------------------
# The RUST language facet (shared by Solana and, later, Soroban)
# ---------------------------------------------------------------------------

#: Cargo/Anchor project layout: hide build output, VCS, lockfiles, and the JS side; keep the
#: crate sources and `tests/`. (Contrast the Foundry-shaped ``FS_FORBIDDEN_READ``.)
RUST_FORBIDDEN_READ = r"(^target/.*)|(^\.git.*)|(^node_modules/.*)|(.*\.lock$)"
# The pipeline generates hundreds of MB of build/scratch *inside the workdir* mid-run, which the
# source tools' file-listing would otherwise pull into LLM context and blow the model's window:
#   • ``.sandbox_cargo`` — the command sandbox's private CARGO_HOME (docs/command-sandbox.md §3);
#     a build fills it with the *entire* cargo registry (~19.5k files, ~520 MB).
#   • ``.sandbox_tmp``    — the sandbox's private linker TMPDIR.
#   • nested ``target/``  — cargo build output below the root (e.g. the generated
#     ``fuzz/<program>/target``, ~4k files, ~900 MB); the top-level ``^target/`` above misses it.
# These are never source, so they are never readable by the source tools (belt-and-suspenders with
# each run's own cleanup: a re-run or cached CI workspace can leave them behind).
RUST_FORBIDDEN_READ = (
    RUST_FORBIDDEN_READ + r"|(^\.sandbox_cargo/.*)|(^\.sandbox_tmp/.*)|(.*/target/.*)"
)

RUST_CODE_EXPLORER_PROMPT = """\
You are a code-exploration assistant analyzing Rust source for on-chain programs (e.g. Solana
/ Anchor). You have file tools (list_files, get_file, grep_files) to explore the project.
Answer the question concretely, citing the relevant items: instruction handlers, account
validation structs (e.g. Anchor `#[derive(Accounts)]`), account/state types, PDA seed
derivations, signer/owner checks, and cross-program invocations. Quote the exact Rust snippets
that establish or omit a check; do not speculate about code you have not read.
"""

RUST = Language(
    name="rust",
    default_forbidden_read=RUST_FORBIDDEN_READ,
    code_explorer_prompt=RUST_CODE_EXPLORER_PROMPT,
    failure_modes_partial="rust/_failure_modes.j2",
)


# ---------------------------------------------------------------------------
# The Solana chain (RUST ⊕ solana)
# ---------------------------------------------------------------------------


def _solana_validate(app: BaseApplication, expected_main: SolidityIdentifier | None) -> str | None:
    """Connectivity/shape validation for a ``SolanaApplication`` (retry feedback on failure).
    Mirrors the EVM ``_validate_connectivity`` structure: unique program identifiers, unique
    instruction slugs within a program, the expected main program present, CPI targets known."""
    if not isinstance(app, SolanaApplication):
        return None
    errors: list[str] = []
    known_programs: set[str] = set()
    for prog in app.programs:
        if prog.program_identifier in known_programs:
            errors.append(f"Duplicate program identifier: {prog.program_identifier}")
        known_programs.add(prog.program_identifier)
        slug_origin: dict[str, str] = {}
        from composer.spec.util import slugify_filename

        for ins in prog.instructions:
            slug = slugify_filename(ins.name)
            if slug in slug_origin:
                errors.append(
                    f"Instructions {slug_origin[slug]!r} and {ins.name!r} in {prog.name} "
                    f"reduce to the same filename slug {slug!r}; give them more-distinct names."
                )
            slug_origin[slug] = ins.name
            # CPI targets may be well-known external programs (SPL Token, System, …)
            # that are not declared in the model; we do not flag those. A future
            # policy can require known_programs | known_authorities | an allowlist.
    if expected_main is not None and expected_main not in known_programs:
        errors.append(
            f"Expected a program with identifier {expected_main!r}; declared programs: "
            f"{sorted(known_programs) or '(none)'}."
        )
    if not errors:
        return None
    if len(errors) == 1:
        return errors[0]
    return "Multiple validation errors; fix all before resubmitting:\n" + "\n".join(f"- {e}" for e in errors)


def _solana_locate_main(app: SolanaApplication, source: SourceCode) -> SolanaProgramInstance:
    for i, prog in enumerate(app.programs):
        if prog.program_identifier == source.contract_name:
            return SolanaProgramInstance(i, app)
    raise ValueError(f"main program {source.contract_name!r} not found in analyzed application")


def _solana_units(main: SolanaProgramInstance) -> list[SolanaInstructionInstance]:
    # Per-instruction units — unused under global extraction (kept for a per-instruction
    # fallback and to satisfy the ecosystem's Unit type).
    return [SolanaInstructionInstance(i, main) for i in range(len(main.program.instructions))]


def _solana_extraction_unit(main: SolanaProgramInstance) -> SolanaProgramInstance:
    # The whole program is the extraction context; SolanaProgramInstance is itself a
    # FeatureUnit, so the driver reads it directly to propose whole-program invariants.
    return main


def _solana_property_unit(
    main: SolanaProgramInstance, prop: PropertyFormulation, ind: int
) -> SolanaInvariantUnit:
    return SolanaInvariantUnit(ind, main, prop)


def _solana_analysis_extra_input(source: SourceCode) -> list[str | dict]:
    return [
        f"The main program of this application has been explicitly identified as "
        f"{source.contract_name} at relative path {source.relative_path}. "
        "Your output MUST contain a program whose program_identifier is this exact identifier."
    ]


SOLANA: Ecosystem[SolanaApplication, SolanaProgramInstance, SolanaInstructionInstance] = Ecosystem(
    name="solana",
    language=RUST,
    system_model=SolanaApplication,
    analysis_prompts=PromptPair("solana/analysis_system.j2", "solana/analysis_prompt.j2"),
    property_prompts=PromptPair("solana/property_system.j2", "solana/property_prompt.j2"),
    validate_analysis=_solana_validate,
    locate_main=_solana_locate_main,
    units=_solana_units,
    analysis_extra_input=_solana_analysis_extra_input,
    # Fuzzing wants whole-program invariants (one run per invariant), not per-instruction
    # properties — docs/crucible-unit-granularity.md.
    global_extraction=True,
    extraction_unit=_solana_extraction_unit,
    property_unit=_solana_property_unit,
)


#: Registry of available ecosystems, keyed by chain tag. Heterogeneous in ``App``/``Main``/``Unit``
#: (each chain has its own model), hence ``Ecosystem[Any, Any, Any]``.
ECOSYSTEMS: dict[ChainTag, Ecosystem[Any, Any, Any]] = {"evm": EVM, "solana": SOLANA}
