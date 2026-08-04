"""The ecosystem seam — the *front half* of the pipeline made parametric over the
blockchain/source domain (see ``docs/ecosystem-abstraction.md``).

An **ecosystem** bundles everything the shared analysis + property-extraction steps need
that is domain-specific: the system-model type they produce, the analysis/property prompt
templates, connectivity validation, the main-unit locator, and the per-unit enumeration.
It factors into a **language** facet (the conventions for reading the *analyzed* program's
source — Solidity, Rust — shared across chains that use the same language) and the **chain**
facet (the platform model + prompts). The language here is that of the *code under analysis*,
not the language the AutoProver backend is implemented in (see :class:`Language`).

``EVM = SOLIDITY ⊕ evm`` binds the EVM types, prompt templates, ``validate_solidity_connectivity``,
``main_instance``, and unit enumeration into the seam; ``SOLANA = RUST ⊕ solana`` binds the
Solana model + prompts and reuses the shared ``RUST`` language facet. See ``docs/ecosystem-abstraction.md``.
"""

from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Callable, Literal, Mapping, TypedDict

from composer.spec.context import SourceCode
from composer.spec.code_explorer import CODE_EXPLORER_SYS_PROMPT
from composer.spec.gen_types import TypedTemplate
from composer.spec.prop_inference import (
    InitialPromptRenderer,
    PropertySystemPromptParams,
    PROPERTY_SYSTEM_TEMPLATE,
    _AgentRoundResult,
    render_evm_property_prompt,
)
from composer.spec.system_analysis import (
    AnalysisPromptParams,
    ANALYSIS_INITIAL_TEMPLATE,
    ANALYSIS_SYSTEM_TEMPLATE,
    validate_solidity_connectivity,
)
from composer.spec.service_host import Sort
from composer.spec.system_model import (
    AnyApplication,
    BaseApplication,
    ContractComponentInstance,
    ContractInstance,
    FeatureUnit,
    SourceApplication,
    component_context,
)
from composer.spec.types import SourceIdentifier
from composer.templates.loader import load_jinja_template
from composer.spec.solana.model import (
    AuthorityInteraction,
    SolanaApplication,
    SolanaAuthority,
    SolanaComponentInstance,
    SolanaProgram,
    SolanaProgramInstance,
)
from composer.spec.util import fs_forbidden_read, slugify_filename

LanguageTag = Literal["solidity", "rust"]
ChainTag = Literal["evm", "solana", "soroban"]


@dataclass(frozen=True)
class PromptPair[SysParams: Mapping[str, Any], InitParams: Mapping[str, Any]]:
    """A (system prompt, initial prompt) typed-template pair for one agent — see
    :class:`~composer.spec.gen_types.TypedTemplate`. Two type parameters because a pair's system
    and initial templates don't always share a kwargs shape."""

    system: TypedTemplate[SysParams]
    initial: TypedTemplate[InitParams]


@dataclass(frozen=True)
class PropertyPrompts[Unit: FeatureUnit]:
    """The property-inference agent's prompts — a :class:`PromptPair` whose initial half is a
    renderer rather than a template.

    The system prompt is a plain typed template: its params (``sort`` + ``backend_guidance``)
    carry no unit, so one declaration serves every ecosystem. The initial prompt's params *do*
    carry one, and the fuzzer cannot construct the ``FeatureUnit`` protocol — so each ecosystem
    declares its own param dict naming its concrete unit and hands over a bound renderer. See
    :data:`~composer.spec.prop_inference.InitialPromptRenderer`."""

    system: TypedTemplate[PropertySystemPromptParams]
    render_initial: InitialPromptRenderer[Unit]


@dataclass(frozen=True)
class Language:
    """The language of the **code being analyzed** — a facet of the ecosystem, shared by every
    chain whose programs are written in it (e.g. the ``rust`` facet is shared by Solana and
    Soroban). It drives how the shared front half *reads* the target's source (fs-exclusion
    rule, code-explorer prompt, failure modes)."""

    name: LanguageTag
    #: What the agent's source tools withhold: either a predicate over the project-root-relative
    #: path (Solidity's ``fs_forbidden_read``, whose carve-outs don't fit a regex) or a plain
    #: exclusion pattern where one suffices. Both shapes are what ``GlobalExcludeArg`` accepts.
    default_forbidden_read: str | Callable[[PurePath], bool]
    code_explorer_prompt: str
    # The j2 partial with this language's vulnerability patterns (overflow, panics, …). Reserved
    # for the prompt-fragment split; unused while prompts are still monolithic.
    vulnerability_patterns_partial: str | None = None


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
    analysis_prompts: PromptPair[AnalysisPromptParams, AnalysisPromptParams]
    #: Prompts for the per-component property-inference agent.
    property_prompts: PropertyPrompts[Unit]
    #: Connectivity/shape validation of the analyzed model (retry feedback on failure).
    validate_analysis: Callable[[App, SourceIdentifier | None], str | None]
    #: Locate the target unit (the "main contract"/program) in the analyzed model.
    locate_main: Callable[[App, SourceCode], Main]
    #: Enumerate the units the extraction phase infers properties for — one batch per unit. Both
    #: ecosystems return one per **component** of the main contract/program: a named cluster of its
    #: behavior produced by system analysis (docs/ecosystem-abstraction.md §4). Note this is on the
    #: *ecosystem* axis, so every backend paired with a chain inherits the same split — pick it on
    #: backend-neutral grounds, and let a backend that wants coarser work aggregate in its
    #: ``Formalizer`` instead.
    units: Callable[[Main], list[Unit]]
    #: Runtime witness of ``Unit``, matched against a plugin's
    #: :data:`~composer.pipeline.plugin_api.PluginScope` to decide whether that plugin applies to
    #: this ecosystem's runs. A value rather than just the type parameter because the matching
    #: happens at the entry-point boundary, where nothing static survives.
    unit_type: type[Unit]
    #: Domain-specific front-matter appended to the analysis input (was hardcoded in the driver).
    analysis_extra_input: Callable[[SourceCode], list[str | dict]]
    #: Whether ``analysis_prompts``/``property_prompts`` have a ``sort == "greenfield"`` branch.
    #: Only EVM does (the natspec design-doc-to-Solidity path) — Solana's templates have no
    #: greenfield content, so ``run_pipeline`` asserts against this rather than silently rendering
    #: a template that never mentions the mode it was asked for.
    supports_greenfield: bool


#: Names for the two concrete instantiations, so a consumer that is pinned to one chain can say so
#: without respelling the triple (or erasing it to ``Any``). :class:`Ecosystems` declares the same
#: pairing; these are what its fields are typed with.
type EvmEcosystem = Ecosystem[SourceApplication, ContractInstance, ContractComponentInstance]
type SolanaEcosystem = Ecosystem[SolanaApplication, SolanaProgramInstance, SolanaComponentInstance]


# ---------------------------------------------------------------------------
# main-unit location
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
# The EVM ecosystem
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


# Adding Vyper support (a second EVM source language) would, at a very high level:
#   1. Extend ``LanguageTag`` with ``"vyper"`` and add a ``VYPER`` ``Language`` facet here (its
#      own ``forbidden_read``, code-explorer prompt, and — eventually — failure-modes partial).
#   2. Bind it to a Vyper-flavored EVM ``Ecosystem`` (its own analysis/property prompts) and
#      route to it by detecting the target's source language at the entry point.
#   3. Loosen the analysis model's Solidity assumptions: contracts are keyed by
#      ``SolidityIdentifier`` / ``solidity_identifier`` throughout (see ``system_model`` and
#      ``main_instance``), which would need to widen to the language-neutral
#      ``SourceIdentifier`` the ecosystem seam already speaks.
# The CVL backend needs the least work — the Certora Prover already accepts Vyper (it verifies
# compiled bytecode) — while the Foundry backend is Solidity-only by construction (it authors
# and runs ``.t.sol`` tests), so it would need a separate Vyper story or be left EVM/Solidity-only.
SOLIDITY = Language(
    name="solidity",
    default_forbidden_read=fs_forbidden_read,
    code_explorer_prompt=CODE_EXPLORER_SYS_PROMPT,
)

EVM: EvmEcosystem = Ecosystem(
    name="evm",
    language=SOLIDITY,
    system_model=SourceApplication,
    analysis_prompts=PromptPair(ANALYSIS_SYSTEM_TEMPLATE, ANALYSIS_INITIAL_TEMPLATE),
    property_prompts=PropertyPrompts(PROPERTY_SYSTEM_TEMPLATE, render_evm_property_prompt),
    validate_analysis=validate_solidity_connectivity,
    locate_main=main_instance,
    supports_greenfield=True,
    units=_evm_units,
    unit_type=ContractComponentInstance,
    analysis_extra_input=_evm_analysis_extra_input,
)


# ---------------------------------------------------------------------------
# The RUST language facet (shared by Solana, Soroban)
# ---------------------------------------------------------------------------

#: Cargo/Anchor project layout: hide build output, VCS, lockfiles, and the JS side; keep the
#: crate sources and `tests/`. A pattern suffices here — unlike the Foundry-shaped
#: ``fs_forbidden_read``, nothing needs carving back out of an excluded directory.
RUST_FORBIDDEN_READ = r"(^target/.*)|(^\.git.*)|(^node_modules/.*)|(.*\.lock$)"
# NOTE: the confined-build scratch dirs (``.sandbox_cargo`` / ``.sandbox_rustup`` /
# ``.sandbox_tmp`` and nested ``target/``) are also excluded, but that extension lives with the
# rust-framework layer that introduces confined Rust builds — no build runs in this front-half, so
# those dirs never exist here.

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
    vulnerability_patterns_partial="rust/_vulnerability_patterns.j2",
)


# ---------------------------------------------------------------------------
# The Solana chain (RUST ⊕ solana)
# ---------------------------------------------------------------------------


def _validate_program_components(prog: SolanaProgram) -> list[str]:
    """One program's :class:`ProgramComponent` checks — the peer of the component half of EVM's
    ``validate_solidity_connectivity`` (``docs/ecosystem-abstraction.md`` §4, "Validation").

    Name and slug uniqueness mirror EVM directly. The component↔instruction mapping check has no
    EVM peer and is the one deliberate divergence: EVM's ``external_entry_points`` are prose the
    prompt renders, while a Solana component's ``instructions`` are *references* that resolve into
    the program (the unit wrapper turns them back into ``SolanaInstruction`` objects). A dangling
    name would silently drop an instruction's account/constraint detail from the extraction
    prompt, and an *unreferenced* instruction is an entry point no property will ever cover — so
    the mapping is required to be both valid and total. It costs two set operations."""
    errors: list[str] = []
    seen: set[str] = set()
    slug_origin: dict[str, str] = {}
    for comp in prog.components:
        if comp.name in seen:
            errors.append(f"Duplicate component names in {prog.name}: {comp.name}")
        seen.add(comp.name)
        slug = slugify_filename(comp.name)
        if slug in slug_origin:
            errors.append(
                f"Components {slug_origin[slug]!r} and {comp.name!r} in {prog.name} both reduce "
                f"to the filename slug {slug!r} (punctuation and symbols are normalized to "
                f"underscores); give them names that differ in more than that."
            )
        else:
            slug_origin[slug] = comp.name
        for ins_name in comp.instructions:
            if ins_name not in prog.instructions_by_name:
                errors.append(
                    f"Component {comp.name!r} of {prog.name} lists an instruction {ins_name!r} "
                    f"that {prog.name} does not declare."
                )
    referenced = {n for comp in prog.components for n in comp.instructions}
    unassigned = [i.name for i in prog.instructions if i.name not in referenced]
    if unassigned:
        errors.append(
            f"Instruction(s) {', '.join(repr(n) for n in unassigned)} of {prog.name} belong to no "
            f"component; every instruction must appear in at least one component's `instructions` "
            f"(an instruction may appear in more than one)."
        )
    return errors


def _solana_validate(app: SolanaApplication, expected_main: SourceIdentifier | None) -> str | None:
    """Connectivity/shape validation for a ``SolanaApplication`` (retry feedback on failure).

    Typed over ``SolanaApplication``, not ``BaseApplication``: ``Ecosystem.validate_analysis`` is
    ``Callable[[App, ...]]``, so the seam already guarantees the model came from *this* ecosystem's
    ``system_model``. An earlier version narrowed at runtime and returned ``None`` for a foreign
    application; that silently passed a model this function cannot check, and the parameter type is
    what makes the case unreachable instead.
    Mirrors the EVM ``validate_solidity_connectivity`` structure: unique program identifiers and names,
    unique instruction slugs within a program, unique component names/slugs within a program, the
    component↔instruction mapping valid and total, component interactions resolving, and the
    expected main program present."""
    errors: list[str] = []
    known_identifiers: set[str] = set()
    # Program NAME -> its component names. Keyed by name (not identifier) because that is what an
    # interaction names, exactly as EVM keys by contract name.
    known_components: dict[str, set[str]] = {}
    programs = [c for c in app.components if isinstance(c, SolanaProgram)]
    known_authorities = {c.name for c in app.components if isinstance(c, SolanaAuthority)}
    for prog in programs:
        if prog.program_identifier in known_identifiers:
            errors.append(f"Duplicate program identifier: {prog.program_identifier}")
        known_identifiers.add(prog.program_identifier)
        if prog.name in known_components:
            errors.append(f"Duplicate program names: {prog.name}")
        known_components.setdefault(prog.name, set()).update(c.name for c in prog.components)
        slug_origin: dict[str, str] = {}
        for ins in prog.instructions:
            slug = slugify_filename(ins.name)
            if slug in slug_origin:
                errors.append(
                    f"Instructions {slug_origin[slug]!r} and {ins.name!r} in {prog.name} "
                    f"reduce to the same filename slug {slug!r}; give them more-distinct names."
                )
            slug_origin[slug] = ins.name
            # An instruction may call into another program (Solana's version of one
            # contract calling another). That target is often a standard program every
            # app uses (e.g. the token or system program) that the model author never
            # bothered to declare, so we don't flag undeclared call targets here. A
            # future policy could require them to match known_programs | known_authorities
            # | an allowlist.
        errors.extend(_validate_program_components(prog))

    # Interactions, in a second pass so a component may name one declared later (EVM does the same).
    # Unlike the CPI-target leniency above, these ARE required to resolve: the analysis prompt tells
    # the model to declare every external actor it interacts with, including SPL Token / System.
    for prog in programs:
        for comp in prog.components:
            where = f"Component {comp.name} of {prog.name} interacts with"
            for inter in comp.interactions:
                if isinstance(inter, AuthorityInteraction):
                    if inter.authority not in known_authorities:
                        errors.append(f"{where} unknown external authority: {inter.authority}")
                elif inter.program not in known_components:
                    errors.append(f"{where} an unknown program: {inter.program}")
                elif inter.component and inter.component not in known_components[inter.program]:
                    errors.append(
                        f"{where} unknown component {inter.component} of program {inter.program}"
                    )

    if expected_main is not None and expected_main not in known_identifiers:
        errors.append(
            f"Expected a program with identifier {expected_main!r}; declared programs: "
            f"{sorted(known_identifiers) or '(none)'}."
        )
    if not errors:
        return None

    # The declared-names reference block (EVM peer): every error above is a name that failed to
    # resolve, so the retry is far more likely to land if it can see the vocabulary it submitted.
    def _fmt(items: set[str]) -> str:
        return ", ".join(sorted(items)) if items else "(none)"

    reference_lines = [
        f"- Declared programs: {_fmt(set(known_components))}",
        f"- Declared external authorities: {_fmt(known_authorities)}",
    ]
    for prog_name, comps in sorted(known_components.items()):
        reference_lines.append(f"- Components of {prog_name}: {_fmt(comps)}")
    reference = (
        "\n\nFor reference, the names you declared in your submission:\n" + "\n".join(reference_lines)
    )

    if len(errors) == 1:
        return errors[0] + reference
    return (
        "Multiple validation errors; fix all before resubmitting:\n"
        + "\n".join(f"- {e}" for e in errors)
        + reference
    )


def _solana_locate_main(app: SolanaApplication, source: SourceCode) -> SolanaProgramInstance:
    for i, prog in enumerate(app.programs):
        if prog.program_identifier == source.contract_name:
            return SolanaProgramInstance(i, app)
    raise ValueError(f"main program {source.contract_name!r} not found in analyzed application")


def _solana_units(main: SolanaProgramInstance) -> list[SolanaComponentInstance]:
    # One unit per component of the MAIN program — the exact shape of ``_evm_units`` (which
    # enumerates the main *contract's* components; siblings are context, not units). Replaces the
    # whole-program singleton this returned before: one extraction agent for a 62-instruction
    # program is a hard cap on depth, and the unit was a Crucible cost decision sitting on a
    # backend-neutral seam. See docs/ecosystem-abstraction.md §1 (the two axes) and §4.
    return [
        SolanaComponentInstance(ind=i, _program=main)
        for i in range(len(main.program.components))
    ]


def _solana_analysis_extra_input(source: SourceCode) -> list[str | dict]:
    return [
        f"The main program of this application has been explicitly identified as "
        f"{source.contract_name} at relative path {source.relative_path}. "
        "Your output MUST contain a program whose program_identifier is this exact identifier."
    ]


@component_context
class SolanaPropertyPromptParams(TypedDict):
    """Kwargs for the Solana property-extraction agent's initial prompt template. The EVM peer is
    ``EvmPropertyPromptParams``; they differ only in the concrete ``context`` type, which is the
    whole reason each ecosystem declares its own (see :class:`PropertyPrompts`)."""
    context: SolanaComponentInstance
    sort: Sort
    prior_properties: list[_AgentRoundResult]


#: The Solana prompts. Bound to top-level names (rather than inlined into the prompt pairs below)
#: because ``composer.meta.templates`` discovers templates by an AST scan for top-level
#: ``NAME = TypedTemplate[...](...)`` assignments — an inlined declaration is invisible to it and
#: so missing from ``template_manifest.json`` (see ``tests/test_template_manifest.py``).
SOLANA_ANALYSIS_SYSTEM_TEMPLATE = TypedTemplate[AnalysisPromptParams]("solana/analysis_system.j2")
SOLANA_ANALYSIS_INITIAL_TEMPLATE = TypedTemplate[AnalysisPromptParams]("solana/analysis_prompt.j2")
SOLANA_PROPERTY_SYSTEM_TEMPLATE = TypedTemplate[PropertySystemPromptParams]("solana/property_system.j2")
SOLANA_PROPERTY_INITIAL_TEMPLATE = TypedTemplate[SolanaPropertyPromptParams]("solana/property_prompt.j2")


def _render_solana_property_prompt(
    context: SolanaComponentInstance,
    sort: Sort,
    prior_properties: list[_AgentRoundResult],
) -> str:
    return SOLANA_PROPERTY_INITIAL_TEMPLATE.bind({
        "context": context,
        "sort": sort,
        "prior_properties": prior_properties,
    }).render_to(load_jinja_template)


# Per-component units, mirroring EVM: ``Main`` is the located program, ``Unit`` is one of its
# ``ProgramComponent``s. Every Solana backend inherits this split — Crucible today, a CVLR backend
# later — which is why it is chosen on backend-neutral grounds (docs/ecosystem-abstraction.md §4).
SOLANA: SolanaEcosystem = Ecosystem(
    name="solana",
    language=RUST,
    system_model=SolanaApplication,
    analysis_prompts=PromptPair(
        SOLANA_ANALYSIS_SYSTEM_TEMPLATE, SOLANA_ANALYSIS_INITIAL_TEMPLATE
    ),
    property_prompts=PropertyPrompts(
        SOLANA_PROPERTY_SYSTEM_TEMPLATE, _render_solana_property_prompt
    ),
    validate_analysis=_solana_validate,
    locate_main=_solana_locate_main,
    supports_greenfield=False,
    units=_solana_units,
    unit_type=SolanaComponentInstance,
    analysis_extra_input=_solana_analysis_extra_input,
)


class Ecosystems(TypedDict):
    """Registry of available ecosystems, keyed by chain tag. The set is closed (a new chain
    means a new field here, not a new dict entry), so a ``TypedDict`` gives each entry its own
    concrete ``Ecosystem[App, Main, Unit]`` instead of erasing all of them to
    ``Ecosystem[Any, Any, Any]``."""

    evm: EvmEcosystem
    solana: SolanaEcosystem


ECOSYSTEMS: Ecosystems = {"evm": EVM, "solana": SOLANA}
