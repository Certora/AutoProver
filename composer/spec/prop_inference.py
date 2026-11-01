"""
Property generation agent: extracts security properties from application components.

Parameterized by source availability via AnalysisInput tuple.

Inference runs in rounds. Each round is a fresh agent run that sees every property the earlier
rounds kept, with their reasoning, and is asked for what they missed; from the second round on
it is also given one angle to start from (``shared/prior_properties.j2``). A light-model call
then drops the round's restatements of earlier properties (:func:`_drop_restatements`). The loop
stops at the first round that keeps nothing, at ``max_rounds``, or under budget pressure.
"""

import logging
from typing import Any, Callable, NotRequired, Sequence, Literal, TypedDict
from pydantic import BaseModel, Field
from dataclasses import dataclass


from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

from graphcore.graph import MessagesState, FlowInput, MessagePayloadType, RawMessageType, PromptInput

from composer.input.files import Document
from composer.llm.structured import with_structured_output
from composer.llm.types import CacheLevel
from composer.spec.gen_types import TypedTemplate
from composer.spec.util import combine_digests, string_hash
from composer.spec.context import WorkflowContext, CacheKey, ComponentGroup
from composer.spec.key_family import KeyFamily
from composer.spec.gen_types import TypedTemplate
from composer.spec.graph_builder import bind_standard, run_to_completion
from composer.spec.types import PropertyFormulation
from composer.spec.system_model import ContractComponentInstance, FeatureUnit, component_context
from composer.tools.thinking import RoughDraftState, get_rough_draft_tools
from composer.spec.service_host import Sort, ServiceHost
from composer.io.conversation import ConversationContextProvider
from composer.diagnostics.budget import budget_monitor, budget_pressure
from composer.templates.loader import load_jinja_template
from composer.spec.prop_refinement import user_property_refinement

_log = logging.getLogger(__name__)

#: How many inference rounds a component gets unless the caller says otherwise
#: (``--max-bug-rounds``). Each productive round costs about as much as the first one.
DEFAULT_MAX_BUG_ROUNDS = 5

class _BugAnalysisCache(BaseModel):
    items: list[PropertyFormulation] = Field(description="The security properties you have extracted about the component. Do NOT include any properties " \
    "mentioned in <prior_properties> (if any were provided to you). If you have not extracted any novel properties, return an empty list")

@dataclass
class PropertyGenerationInputBase:
    uid: str
    sort: Literal["specific", "generic"]
    when: Literal["initial", "always"]


@dataclass
class CacheablePropertyGenerationInput(PropertyGenerationInputBase):
    provide: Callable[[bool], MessagePayloadType]

@dataclass
class PropertyGenerationInput(PropertyGenerationInputBase):
    input: MessagePayloadType

    @property
    def provide(self) -> Callable[[bool], MessagePayloadType]:
        return lambda _x: self.input

type AnyPropertyGenerationInput = PropertyGenerationInput | CacheablePropertyGenerationInput

class _AgentRoundResult(_BugAnalysisCache):
    """
    The results of your analysis from this round.
    """
    reasoning: str = Field(description="A short summary of the properties added this round and the "
        "coverage they provide, for later rounds and a human reviewer. Later rounds read this to "
        "avoid re-proposing the same properties, so be specific about what these properties cover."
    )

class PropertySystemPromptParams(TypedDict):
    """Kwargs for the property-extraction agent's system prompt template.

    ``backend_guidance`` lives here, not on the initial prompt: it is fixed for the whole
    run, so putting it in the (rendered-once) system prompt keeps it inside the cached
    prefix instead of re-sending it with every round's initial prompt."""
    sort: Sort
    backend_guidance: str


#: Renders the property-extraction agent's per-round initial prompt for one ecosystem's unit
#: type. A *renderer* rather than a ``TypedTemplate``, because the initial prompt's params carry
#: the unit: each ecosystem declares its own param dict naming its concrete unit (so the template
#: fuzzer can construct one — see :func:`~composer.spec.system_model.component_context`), and only
#: code that knows that concrete type can bind the template. This agent stays generic over
#: ``FeatureUnit`` and just asks for the string.
type InitialPromptRenderer[U: FeatureUnit] = Callable[[U, Sort, list[_AgentRoundResult]], str]


@component_context
class EvmPropertyPromptParams(TypedDict):
    """Kwargs for the EVM property-extraction agent's initial prompt template."""
    context: ContractComponentInstance
    sort: Sort
    prior_properties: list[_AgentRoundResult]


#: The EVM/Solidity property prompts.
PROPERTY_SYSTEM_TEMPLATE = TypedTemplate[PropertySystemPromptParams]("property_analysis_system_prompt.j2")
PROPERTY_INITIAL_TEMPLATE = TypedTemplate[EvmPropertyPromptParams]("property_analysis_prompt.j2")


def render_evm_property_prompt(
    context: ContractComponentInstance,
    sort: Sort,
    prior_properties: list[_AgentRoundResult],
) -> str:
    return PROPERTY_INITIAL_TEMPLATE.bind({
        "context": context,
        "sort": sort,
        "prior_properties": prior_properties,
    }).render_to(load_jinja_template)


class _AgentRoundWithHistory(_AgentRoundResult):
    """One round as cached. ``items`` holds only the properties that survived
    :func:`_drop_restatements`; ``restated`` names the ones it dropped, so a replay
    needs no further LLM call."""
    agent_conversation: list[AnyMessage]
    restated: list[str] = Field(default_factory=list)


#: Prepended to every round's prompt when the project already carries specifications written
#: by its authors (an evaluate-mode run). The inference agent can read them with its source
#: tools, so it is told how far to trust them.
EXISTING_SPECS_NOTICE = (
    "This project already has formal specifications (CVL specs and Prover confs, usually under "
    "certora/) written by its authors, and your source tools can read them. Treat them as "
    "unchecked work by someone else. They may be incomplete, and some rules may be wrong: a rule "
    "may check something other than what its name says, or assume away the behaviour that "
    "matters. Do not take your properties from them, and do not limit your list to what they "
    "check. Your goal is a comprehensive set of the properties a correct implementation of this "
    "component must satisfy. The list will be used to find what those specifications miss.\n\n"
)


def _bug_analysis_key(
    threat_model_digest: str | None,
    with_refinement: bool,
    extra_context_digest: str | None = None,
    max_rounds: int | None = None,
    existing_specs: bool = False,
) -> str:
    base_key = "bug_analysis"
    if with_refinement:
        base_key += "|refine"
    if threat_model_digest is not None:
        base_key += "-tm-" + threat_model_digest
    if extra_context_digest is not None:
        base_key += "-xc-" + extra_context_digest
    if existing_specs:
        base_key += "-es"
    if max_rounds is not None:
        base_key += f"-r{max_rounds}"
    return base_key

#: Parameterized on the *digests* of the documents that fed the prompt (not the
#: documents) so a recorded digest — e.g. from a run's cache tags — can rebuild the key.
#: Each input contributes a suffix only when present, so a run with neither document
#: keeps the bare ``bug_analysis`` key. The driver always passes ``max_rounds``: the cached
#: list is the union of up to that many rounds, so a run with a different limit must not
#: replay it. ``None`` addresses records written before the limit was part of the key.
BUG_ANALYSIS_KEY = KeyFamily(ComponentGroup, _BugAnalysisCache, _bug_analysis_key)

class _AgentResult(_BugAnalysisCache):
    final_history: list[AnyMessage]

def _agent_round_key(i: int, prior: Sequence[_AgentRoundResult] = ()) -> str:
    if not prior:
        return f"round-{i}"
    fed = [
        string_hash("|".join([*(p.model_dump_json() for p in r.items), r.reasoning]))
        for r in prior
    ]
    return f"round-{i}-{string_hash('|'.join(fed))}"

#: One round, keyed by its index and by the earlier rounds' kept properties and reasoning,
#: which is what its prompt is built from. Round 0 has no earlier rounds and keeps ``round-0``.
AGENT_ROUND_KEY = KeyFamily(_AgentResult, _AgentRoundWithHistory, _agent_round_key)

AGENT_RESULT_KEY = CacheKey[_BugAnalysisCache, _AgentResult]("agent_bug_analysis")

DESCRIPTION = "Property extraction"

CERTORA_BACKEND_GUIDANCE: str = """\
You *must* limit your invariants/vectors/properties to those that can be
plausibly formally stated or reasoned about in a symbolic reasoning tool
(namely, the Certora Prover). The following is a list of types of
properties which are difficult or impossible to prove using the Certora
Prover:

1. Attack vectors or invariants that reference off-chain events (like
   key compromising, phishing, etc.)
2. Reasoning about hash function behavior or hash collisions (e.g.,
   "invalid signatures should be rejected")
3. Event emission (not impossible, simply difficult and tedious)

In addition, due to the advent of checked arithmetic, properties that
assert no overflow are considered uninteresting. Further, properties
which assert properties implied by the type system are generally not
considered interesting (e.g., a uint256 being non-negative, a uint128
field not exceeding 2^128 - 1, etc.)
"""

def _unique_titles_validator(
    prev: list[_AgentRoundResult],
) -> Callable[[Any, _AgentRoundResult], str | None]:
    """Validator for the property-extraction agent: every property title must be unique,
    both within this round's output and against titles already extracted in prior rounds
    (the agent cannot change prior rounds, so any clash must be resolved by renaming the
    property it produced this round)."""
    prior_titles = {p.title for r in prev for p in r.items}

    def validate(_state: Any, result: _AgentRoundResult) -> str | None:
        seen: set[str] = set()
        dupes: set[str] = set()
        clashes: set[str] = set()
        for p in result.items:
            if p.title in seen:
                dupes.add(p.title)
            seen.add(p.title)
            if p.title in prior_titles:
                clashes.add(p.title)
        problems: list[str] = []
        if dupes:
            problems.append(f"used more than once in this round ({', '.join(sorted(dupes))})")
        if clashes:
            problems.append(f"already used by an earlier round ({', '.join(sorted(clashes))})")
        if problems:
            return (
                "Property titles must be unique. The following are "
                + "; ".join(problems)
                + ". Rename the offending propert(ies) you produced this round and resubmit."
            )
        return None

    return validate

def _partition[S](s: Sequence[S], pred: Callable[[S], bool]) -> tuple[list[S], list[S]]:
    a = []
    b = []
    for t in s:
        if pred(t):
            a.append(t)
        else:
            b.append(t)
    return a, b

def get_initial_prompt_builder[U: FeatureUnit](
    sort: Sort,
    extra_inputs: Sequence[AnyPropertyGenerationInput],
    component: U,
    render_initial: InitialPromptRenderer[U],
) -> Callable[[list[_AgentRoundResult]], PromptInput]:
    # Order priority (to facilitate caching)
    # Generic-always -> generic-first -> component-always -> component-first -> initial-prompt
    # within each group we sort cacheable things last, and then within THOSE groups we sort by UID
    generic_input, component_input = _partition(extra_inputs, lambda d: d.sort == "generic")

    generic_always, generic_first = _partition(generic_input, lambda d: d.when == "always")

    component_always, component_first = _partition(component_input, lambda d: d.when == "always")

    def cache_stable_sort(s: list[AnyPropertyGenerationInput]):
        cacheable, uncacheable = _partition(s, lambda d: isinstance(d, CacheablePropertyGenerationInput))
        cacheable_sorted = sorted(cacheable, key=lambda d: d.uid)
        uncacheable_sorted = sorted(uncacheable, key=lambda d: d.uid)
        return [*uncacheable_sorted, *cacheable_sorted]

    def extend(s: list[RawMessageType], to_extend: list[AnyPropertyGenerationInput], cache_last: bool):
        for (i, t) in enumerate(to_extend, 1):
            r = t.provide(i == len(to_extend) and cache_last)
            if isinstance(r, list):
                s.extend(r)
            else:
                s.append(r)

    first_round_prefix : list[RawMessageType] = []

    extend(first_round_prefix, cache_stable_sort(generic_always), cache_last=True)

    later_round_prefix = first_round_prefix.copy()

    stable_component_always = cache_stable_sort(component_always)

    extend(first_round_prefix, cache_stable_sort(generic_first), cache_last=True) # want to match (S, GA, GF) prefix across components
    extend(first_round_prefix, stable_component_always, cache_last=len(generic_first) == 0) # if there is no GF, then the first round is (S, GA, CA), so warm that prefix for later rounds
    extend(first_round_prefix, cache_stable_sort(component_first), cache_last=False)

    extend(later_round_prefix, stable_component_always, cache_last=True)

    def renderer(prev_results: list[_AgentRoundResult]) -> PromptInput:
        rendered = render_initial(component, sort, prev_results)
        if len(prev_results) == 0:
            # first round
            return [*first_round_prefix, rendered]
        else:
            return [*later_round_prefix, rendered]

    return renderer

class _Restatement(BaseModel):
    title: str = Field(description="The title of the new property, copied exactly from <new_properties>.")
    restates: str = Field(
        description="The title of the earlier property it restates, copied exactly from <earlier_properties>."
    )


class _RoundRestatements(BaseModel):
    restatements: list[_Restatement] = Field(
        description="One entry for every new property that restates an earlier one. Empty if none does."
    )


class PropertyDedupPromptParams(TypedDict):
    earlier: list[PropertyFormulation]
    fresh: list[PropertyFormulation]


PROPERTY_DEDUP_TEMPLATE = TypedTemplate[PropertyDedupPromptParams]("property_dedup_prompt.j2")


async def _drop_restatements(
    env: ServiceHost,
    prev: Sequence[_AgentRoundResult],
    fresh: list[PropertyFormulation],
    thread_id: str,
) -> tuple[list[PropertyFormulation], list[str]]:
    """Split ``fresh`` into the properties that are new and the titles of those that restate a
    property of ``prev``. This is what makes a round count as productive: the title validator
    only stops exact repeats, and a round that rewords earlier properties would otherwise keep
    the loop going to ``max_rounds``.

    One structured call on the light model. An entry naming a title that is not in ``fresh``,
    or an earlier property that does not exist, removes nothing: a wrong removal loses a property
    for good, a missed one costs at most one more round. ``thread_id`` tags the call in the run's
    usage record, which is otherwise unable to say which code path made it."""
    earlier = [p for r in prev for p in r.items]
    if not earlier or not fresh:
        return fresh, []
    prompt = PROPERTY_DEDUP_TEMPLATE.bind({
        "earlier": earlier, "fresh": fresh,
    }).render_to(load_jinja_template)
    bound = with_structured_output(env.llm_lite(), _RoundRestatements)
    result = await bound.ainvoke(
        [SystemMessage(load_jinja_template("property_dedup_system.j2")), HumanMessage(prompt)],
        config={"metadata": {"thread_id": thread_id}},
    )
    assert isinstance(result, _RoundRestatements)

    fresh_titles = {p.title for p in fresh}
    earlier_titles = {p.title for p in earlier}
    dropped: set[str] = set()
    for r in result.restatements:
        if r.title in fresh_titles and r.restates in earlier_titles:
            dropped.add(r.title)
        else:
            _log.warning(
                "restatement check named unknown properties (%r restates %r); ignored",
                r.title, r.restates,
            )
    return [p for p in fresh if p.title not in dropped], [p.title for p in fresh if p.title in dropped]


async def _run_bug_round(
    env: ServiceHost,
    ctx: WorkflowContext[_AgentResult],
    round: int,
    prompt_render: Callable[[list[_AgentRoundResult]], PromptInput],
    prev: list[_AgentRoundResult],
    system_prompt: str
) -> _AgentRoundWithHistory:
    round_ctx = ctx.child(AGENT_ROUND_KEY(round, prev))
    if (cached := await round_ctx.cache_get(_AgentRoundWithHistory)) is not None:
        return cached


    builder = env.builder_heavy()

    class BugAnalysisInput(FlowInput, RoughDraftState):
        pass

    class ST(MessagesState, RoughDraftState):
        result: NotRequired[_AgentRoundResult]

    d = bind_standard(
        builder, ST, "The security properties you have extracted about the component",
        validator=_unique_titles_validator(prev),
    ).with_input(
        BugAnalysisInput
    ).with_initial_prompt(
        prompt_render(prev)
    ).with_tools(
        get_rough_draft_tools(ST)
    ).with_tools(
        env.analysis_tools
    ).with_sys_prompt(
        system_prompt
    ).with_monitor(
        budget_monitor()
    ).compile_async()

    flow_input: BugAnalysisInput = BugAnalysisInput(
        input=[], memory=None, drafted=False,
    )

    r = await run_to_completion(
        d,
        flow_input,
        thread_id=round_ctx.thread_id,
        recursion_limit=ctx.recursion_limit,
        description=f"{DESCRIPTION} (Round {round + 1})",
    )
    assert "result" in r

    result: _AgentRoundResult = r["result"]

    kept, restated = await _drop_restatements(
        env, prev, result.items, f"{round_ctx.thread_id}-dedup"
    )
    to_ret = _AgentRoundWithHistory(
        items=kept, agent_conversation=r["messages"], reasoning=result.reasoning,
        restated=restated,
    )

    await round_ctx.cache_put(to_ret)
    return to_ret


async def _run_bug_analysis_inner[U: FeatureUnit](
    agent_component_analysis: WorkflowContext[_AgentResult],
    env: ServiceHost,
    component: U,
    extra_input: Sequence[AnyPropertyGenerationInput],
    max_rounds: int,
    backend_guidance: str,
    system_template: TypedTemplate[PropertySystemPromptParams],
    render_initial: InitialPromptRenderer[U],
) -> _AgentResult:
    if (cached := await agent_component_analysis.cache_get(_AgentResult)) is not None:
        return cached

    initial_prompt_builder = get_initial_prompt_builder(
        extra_inputs=extra_input, component=component, sort=env.sort, render_initial=render_initial
    )

    prev_rounds : list[_AgentRoundResult] = []
    last_round_convo : list[AnyMessage] | None = None

    system_prompt = system_template.bind({
        "sort": env.sort,
        "backend_guidance": backend_guidance
    }).render_to(load_jinja_template)

    for i in range(0, max_rounds):
        # Under budget pressure a fresh round would be told to pack it in on
        # its first monitor tick — don't bother launching it. Round 0 always
        # runs (the loop's invariants require at least one round's history).
        if i > 0 and budget_pressure():
            _log.info(
                "%s: property inference stopped before round %d/%d under budget pressure",
                component.display_name, i + 1, max_rounds,
            )
            break
        next_result = await _run_bug_round(
            env, agent_component_analysis, i, initial_prompt_builder, prev_rounds, system_prompt
        )
        _log.info(
            "%s: property inference round %d/%d found %d new propert(ies)%s",
            component.display_name, i + 1, max_rounds, len(next_result.items),
            f"; dropped {len(next_result.restated)} restating earlier ones"
            if next_result.restated else "",
        )
        # A round that keeps nothing ends the loop, whether it proposed nothing or
        # only restated earlier rounds.
        if len(next_result.items) == 0:
            assert last_round_convo is not None
            break

        prev_rounds.append(next_result)
        last_round_convo = next_result.agent_conversation

    assert last_round_convo is not None
    to_ret = _AgentResult(
        items=[
            prop for sublist in prev_rounds for prop in sublist.items
        ],
        final_history=last_round_convo
    )
    await agent_component_analysis.cache_put(to_ret)
    return to_ret

async def run_property_inference[U: FeatureUnit](
    ctx: WorkflowContext[ComponentGroup],
    env: ServiceHost,
    component: U,
    extra_input : Sequence[AnyPropertyGenerationInput] = tuple(),
    threat_model: Document | None = None,
    extra_context: Sequence[Document] = (),
    refinement: ConversationContextProvider | None = None,
    max_rounds: int = DEFAULT_MAX_BUG_ROUNDS,
    *,
    backend_guidance: str,
    system_template: TypedTemplate[PropertySystemPromptParams],
    render_initial: InitialPromptRenderer[U],
    existing_specs: bool = False,
) -> list[PropertyFormulation]:
    """
    Extract security properties for a component, in up to ``max_rounds`` rounds (see the
    module docstring for when the loop stops).

    ``existing_specs`` says the project already carries specifications written by its authors
    (an evaluate-mode run); every round is then told they may be incomplete or wrong
    (:data:`EXISTING_SPECS_NOTICE`).

    ``backend_guidance`` is inlined verbatim into the property-analysis *system* prompt as the
    "what's expressible in your downstream verification tool" filter — the Certora Prover, foundry
    tests, each describing what is and isn't a fit for *their* verification surface. It lives in
    the system prompt (rendered once) rather than the per-round initial prompt so it stays inside
    the cached prefix.

    ``extra_context`` is any number of documents the user supplied about the application.
    Unlike ``threat_model`` they make no claim to be a list of *threats* — they are
    presented as authoritative background, not a checklist. Rendered in the order given,
    each labelled with its filename.
    """

    component_analysis = ctx.child(BUG_ANALYSIS_KEY(
        threat_model.to_digest() if threat_model is not None else None,
        refinement is not None,
        combine_digests([d.to_digest() for d in extra_context]),
        max_rounds=max_rounds,
        existing_specs=existing_specs,
    ))
    if (cached := await component_analysis.cache_get(_BugAnalysisCache)) is not None:
        return cached.items

    actual_extra_input = [
        *extra_input
    ]
    if existing_specs:
        actual_extra_input.append(PropertyGenerationInput(
            "certora:existing-specs", "generic", "always", EXISTING_SPECS_NOTICE,
        ))
    def to_cache_level(s: bool) -> CacheLevel:
        if s:
            return CacheLevel.SHORT
        else:
            return CacheLevel.NONE
    if threat_model is not None:
        actual_extra_input.append(CacheablePropertyGenerationInput(
            "certora:thread_model", "generic", "always",
            provide=lambda cache: [
                "In addition, a coworker has already written a 'threat model' for this application, which may include vulnerabilities/issues that "
                "are common in this type of application. This threat model is written for the entire application (not just the component you are analyzing) "
                "so some of the issues/vulnerabilities/attacks may not be relevant to your analysis. Do *NOT* overfit to this threat model; carefully "
                "analyze what content of the provided threat model is worth considering vs out of scope. Further, this threat model is just a starting point: "
                "it may be incomplete, and some of it may be wrong. You should ALSO look for threats *not* mentioned in this document; the goal is a "
                "comprehensive set of properties for the component.\n\n",
                threat_model.to_dict(cache_level=to_cache_level(cache))
            ]
        ))
    if extra_context:
        def extra_context_blocks(cache: bool) -> list[RawMessageType]:
            # Each document introduced by its filename, then its body. Only the final
            # body carries the cache marker: a breakpoint is a prefix boundary, so
            # marking an interior one spends one of the four without extending the
            # cached prefix.
            blocks: list[RawMessageType] = []
            for i, doc in enumerate(extra_context):
                is_last = i == len(extra_context) - 1
                blocks.append(f"--- {doc.basename} ---\n\n")
                blocks.append(doc.to_dict(cache_level=to_cache_level(cache and is_last)))
            return blocks

        actual_extra_input.append(CacheablePropertyGenerationInput(
            "certora:extra_context", "generic", "always",
            provide=lambda cache: [
                f"In addition, the user requesting this analysis has provided the following extra context about "
                f"the application ({len(extra_context)} document(s), each introduced by its filename below): "
                "notes, assumptions, deployment details, or anything else they considered relevant. It was "
                "written for the entire application (not just the component you are analyzing), so parts of it "
                "may not bear on your analysis. Treat it as authoritative information about how the system is "
                "intended to work and be deployed, and let it inform which behaviors count as violations. It "
                "is *NOT* a list of the issues to look for and it is *NOT* exhaustive: keep looking for "
                "problems it does not mention.\n\n",
                *extra_context_blocks(cache),
            ]
        ))


    agent_attempt = await _run_bug_analysis_inner(
        component_analysis.child(AGENT_RESULT_KEY),
        env,
        component,
        actual_extra_input,
        max_rounds=max_rounds,
        backend_guidance=backend_guidance,
        system_template=system_template,
        render_initial=render_initial,
    )
    if refinement is None:
        to_ret = agent_attempt.items
        await component_analysis.cache_put(_BugAnalysisCache(items=to_ret))
        return to_ret

    refined_props = await user_property_refinement(
        env, agent_attempt, refinement
    )
    await component_analysis.cache_put(_BugAnalysisCache(items = refined_props))
    return refined_props
