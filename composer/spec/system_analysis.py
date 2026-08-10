from typing import NotRequired, Any, Callable, TypedDict

from graphcore.graph import MessagesState, FlowInput


from composer.spec.context import (
    WorkflowContext, SystemDoc, SourceCode
)
from composer.spec.gen_types import TypedTemplate
from composer.spec.graph_builder import bind_standard, run_to_completion
from composer.spec.system_model import BaseApplication, ExplicitContract, ExternalActor, ExternalDependency
from composer.spec.types import SourceIdentifier
from composer.spec.service_host import ServiceHost, Sort
from composer.spec.util import slugify_filename
from composer.tools.thinking import RoughDraftState, get_rough_draft_tools

DESCRIPTION = "Component analysis"


class AnalysisPromptParams(TypedDict):
    """Kwargs shared by the analysis agent's system and initial prompt templates."""
    sort: Sort
    has_doc: bool


#: The EVM/Solidity analysis prompts.
ANALYSIS_SYSTEM_TEMPLATE = TypedTemplate[AnalysisPromptParams]("application_analysis_system.j2")
ANALYSIS_INITIAL_TEMPLATE = TypedTemplate[AnalysisPromptParams]("application_analysis_prompt.j2")

def validate_solidity_connectivity(
    app: BaseApplication, expected_main_id: SourceIdentifier | None
) -> str | None:
    """Connectivity/shape validation for the Solidity model *family*: typed over
    ``BaseApplication`` because it checks only the contract/actor/interaction graph that
    ``Application``, ``SourceApplication``, ``HarnessedApplication``, and
    ``FromSourceApplication`` all share. Both callers name it directly; neither can use the
    other's ``Ecosystem.validate_analysis``, which is narrowed to one ``system_model``."""
    errors: list[str] = []
    known_components: dict[str, set[str]] = {}
    known_external: set[str] = set()
    known_solidity_ids : set[str] = set()

    for c in app.components:
        if isinstance(c, ExplicitContract):
            if c.solidity_identifier in known_solidity_ids:
                errors.append(f"Duplicate solidity identifier: {c.solidity_identifier}")
            else:
                known_solidity_ids.add(c.solidity_identifier)
            if c.name in known_components:
                errors.append(f"Duplicate contract names: {c.name}")
            else:
                known_components[c.name] = set()
            slug_origin: dict[str, str] = {}
            for sub_comp in c.components:
                if sub_comp.name in known_components[c.name]:
                    errors.append(f"Duplicate component names in {c.name}: {sub_comp.name}")
                known_components[c.name].add(sub_comp.name)
                slug = slugify_filename(sub_comp.name)
                if slug in slug_origin:
                    errors.append(
                        f"Components {slug_origin[slug]!r} and {sub_comp.name!r} in {c.name} "
                        f"both reduce to the filename slug {slug!r} (punctuation and symbols are "
                        f"normalized to underscores); give them names that differ in more than that."
                    )
                else:
                    slug_origin[slug] = sub_comp.name
        else:
            assert isinstance(c, ExternalActor)
            if c.name in known_external:
                errors.append(f"Duplicate external component name: {c.name}")
            known_external.add(c.name)

    if expected_main_id is not None and expected_main_id not in known_solidity_ids:
        errors.append(f"Expected an explicit contract instance with solidity identifier: {expected_main_id}")

    for explicit in app.components:
        if not isinstance(explicit, ExplicitContract):
            continue
        for sub_comp in explicit.components:
            thing_interacts_with_str = f"Component {sub_comp.name} of {explicit.name} interacts with"
            for interaction in sub_comp.interactions:
                if isinstance(interaction, ExternalDependency):
                    if interaction.external_actor not in known_external:
                        errors.append(f"{thing_interacts_with_str} unknown external actor: {interaction.external_actor}")
                else:
                    if interaction.contract_name not in known_components:
                        errors.append(f"{thing_interacts_with_str} an unknown explicit contact: {interaction.contract_name}")
                    elif interaction.component and interaction.component not in known_components[interaction.contract_name]:
                        errors.append(f"{thing_interacts_with_str} unknown component {interaction.component} of explicit contract {interaction.contract_name}")

    if not errors:
        return None

    def _fmt(items: set[str]) -> str:
        return ", ".join(sorted(items)) if items else "(none)"

    reference_lines = [
        f"- Declared contracts: {_fmt(set(known_components))}",
        f"- Declared external actors: {_fmt(known_external)}",
    ]
    for contract_name, subs in sorted(known_components.items()):
        reference_lines.append(f"- Components of {contract_name}: {_fmt(subs)}")
    reference = "\n\nFor reference, the names you declared in your submission:\n" + "\n".join(reference_lines)

    if len(errors) == 1:
        return errors[0] + reference
    return "Multiple validation errors found; fix all of them before resubmitting:\n" + "\n".join(f"- {e}" for e in errors) + reference

async def run_component_analysis[T: BaseApplication](
    ty: type[T],
    child_ctxt: WorkflowContext[T],
    input: SystemDoc | SourceCode | None,
    env: ServiceHost,
    extra_input: list[str | dict],
    expected_main_id: SourceIdentifier | None = None,
    *,
    system_template: TypedTemplate[AnalysisPromptParams],
    initial_template: TypedTemplate[AnalysisPromptParams],
    validate: Callable[[T, SourceIdentifier | None], str | None],
) -> T | None:
    """Analyze application components from a system doc and optionally source code.

    ``input`` may be a bare ``SystemDoc`` (natspec mode), a ``SourceCode`` (source
    mode — whose ``content`` may be ``None`` for a source-only run), or ``None``.
    ``has_doc`` below reflects whether a design document is actually present, not
    merely whether an ``input`` object was passed, so a source-only run renders the
    no-doc prompt branch and never dereferences a missing ``content``.
    """
    if (cached := await child_ctxt.cache_get(ty)) is not None:
        return cached

    has_doc = input is not None and input.content is not None
    # greenfield has no source tools, so a design doc is mandatory there.
    assert has_doc or env.sort != "greenfield"

    memory = child_ctxt.get_memory_tool()

    class AnalysisInput(RoughDraftState, FlowInput):
        pass

    AnalysisState = type("AnalysisState", (MessagesState, RoughDraftState), {
        "__annotations__": {"result": NotRequired[ty]}
    })

    def _validation_wrapper(
        _: Any, app: T
    ) -> str | None:
        return validate(app, expected_main_id)

    prompt_params: AnalysisPromptParams = {"sort": env.sort, "has_doc": has_doc}
    b = bind_standard(
        builder=env.builder_lite(),
        state_type=AnalysisState,
        validator=_validation_wrapper
    ).with_input(
        AnalysisInput
    ).inject(
        lambda g: system_template.bind(prompt_params).render_to(g.with_sys_prompt_template)
    ).with_tools(
        [memory, *get_rough_draft_tools(AnalysisState), *env.analysis_tools]
    ).inject(
        lambda g: initial_template.bind(prompt_params).render_to(g.with_initial_prompt_template)
    )

    graph = b.compile_async()
    inputs : list[str | dict] = []
    if has_doc:
        assert input is not None
        doc = input.content
        assert doc is not None
        inputs.extend([
            "The system document is as follows",
            doc.to_dict()
        ])
    inputs.extend(extra_input)

    flow_input = AnalysisInput(input=inputs, did_read=False, memory=None)

    res = await run_to_completion(
        graph,
        flow_input,
        thread_id=child_ctxt.thread_id,
        recursion_limit=child_ctxt.recursion_limit,
        description=DESCRIPTION,
    )
    assert "result" in res
    result: T = res["result"] #type: ignore trust me bro

    await child_ctxt.cache_put(result)
    return result
