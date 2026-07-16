"""
Property generation agent: extracts security properties from application components.

Parameterized by source availability via AnalysisInput tuple.
"""

from typing import Any, Callable, NotRequired, Sequence, Literal
from pydantic import BaseModel, Field
from dataclasses import dataclass


from langchain_core.messages import AnyMessage, HumanMessage

from graphcore.graph import MessagesState, FlowInput, MessagePayloadType, RawMessageType

from composer.input.files import Document
from composer.spec.context import WorkflowContext, CacheKey, ComponentGroup
from composer.spec.graph_builder import bind_standard, run_to_completion
from composer.spec.types import PropertyFormulation
from composer.spec.system_model import ContractComponentInstance
from composer.tools.thinking import RoughDraftState, get_rough_draft_tools
from composer.spec.service_host import Sort, ServiceHost
from composer.io.conversation import ConversationContextProvider
from composer.templates.loader import load_jinja_template
from composer.spec.prop_refinement import user_property_refinement

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
    reasoning: str = Field(description="What you considered this round, what you rejected and why, "
        "and how the properties you extracted capture parts of the bug surface "
        "prior rounds missed. Future rounds (and the user, in interactive "
        "mode) will read this -- not your message history -- to "
        "understand your reasoning. Be specific."
    )

class _AgentRoundWithHistory(_AgentRoundResult):
    agent_conversation: list[AnyMessage]

def bug_analysis_key(
    threat_model: Document | None,
    with_refinement: bool
) -> CacheKey[ComponentGroup, _BugAnalysisCache]:
    base_key = "bug_analysis"
    if with_refinement:
        base_key += "|refine"
    if threat_model is None:
        return CacheKey[ComponentGroup, _BugAnalysisCache](base_key)
    return CacheKey[ComponentGroup, _BugAnalysisCache](base_key + "-tm-" + threat_model.to_digest())

class _AgentResult(_BugAnalysisCache):
    final_history: list[AnyMessage]

def agent_round_key(
    i: int
) -> CacheKey[_AgentResult, _AgentRoundWithHistory]:
    return CacheKey[_AgentResult, _AgentRoundWithHistory](f"round-{i}")

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

def get_initial_prompt_builder(
    extra_inputs: Sequence[AnyPropertyGenerationInput],
    component: ContractComponentInstance
) -> Callable[[list[_AgentRoundResult]], MessagePayloadType]:
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

    def renderer(prev_results: list[_AgentRoundResult]) -> MessagePayloadType:
        rendered = load_jinja_template(
            "property_analysis_prompt.j2",
            prior_properties=prev_results,
            context=component,
        )
        if len(prev_results) == 0:
            # first round
            return [*first_round_prefix, rendered]
        else:
            return [*later_round_prefix, rendered]

    return renderer

async def _run_bug_round(
    env: ServiceHost,
    ctx: WorkflowContext[_AgentResult],
    round: int,
    prompt_render: Callable[[list[_AgentRoundResult]], MessagePayloadType],
    prev: list[_AgentRoundResult],
    system_prompt: str
) -> _AgentRoundWithHistory:
    round_ctx = ctx.child(agent_round_key(round))
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
    ).compile_async()

    flow_input: BugAnalysisInput = BugAnalysisInput(
        input=[], memory=None, did_read=False,
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

    to_ret = _AgentRoundWithHistory(items=result.items, agent_conversation=r["messages"], reasoning=result.reasoning)

    await round_ctx.cache_put(to_ret)
    return to_ret


async def _run_bug_analysis_inner(
    agent_component_analysis: WorkflowContext[_AgentResult],
    env: ServiceHost,
    component: ContractComponentInstance,
    extra_input: Sequence[AnyPropertyGenerationInput],
    max_rounds: int,
    backend_guidance: str,
) -> _AgentResult:
    if (cached := await agent_component_analysis.cache_get(_AgentResult)) is not None:
        return cached
    
    initial_prompt_builder = get_initial_prompt_builder(
        extra_inputs=extra_input, component=component
    )

    prev_rounds : list[_AgentRoundResult] = []
    last_round_convo : list[AnyMessage] | None = None

    system_prompt = load_jinja_template(
        "property_analysis_system_prompt.j2", sort=env.sort, backend_guidance=backend_guidance
    )

    for i in range(0, max_rounds):
        next_result = await _run_bug_round(
            env, agent_component_analysis, i, initial_prompt_builder, prev_rounds, system_prompt
        )
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

async def run_property_inference(
    ctx: WorkflowContext[ComponentGroup],
    env: ServiceHost,
    component: ContractComponentInstance,
    extra_input : Sequence[AnyPropertyGenerationInput] = tuple(),
    threat_model: Document | None = None,
    refinement: ConversationContextProvider | None = None,
    max_rounds: int = 3,
    backend_guidance: str = CERTORA_BACKEND_GUIDANCE,
) -> list[PropertyFormulation]:
    """
    Extract security properties for a component.

    ``backend_guidance`` is inlined verbatim into the property-analysis
    prompt as the "what's expressible in your downstream verification
    tool" filter. Defaults to ``CERTORA_BACKEND_GUIDANCE`` so existing
    callers (the autoprove pipeline) get the same prompt they always had;
    other backends (e.g. foundry tests) pass their own string describing
    what's a fit / not a fit for *their* verification surface.
    """

    component_analysis = ctx.child(bug_analysis_key(threat_model, refinement is not None))
    if (cached := await component_analysis.cache_get(_BugAnalysisCache)) is not None:
        return cached.items

    actual_extra_input = [
        *extra_input
    ]
    if threat_model is not None:
        actual_extra_input.append(CacheablePropertyGenerationInput(
            "certora:thread_model", "generic", "always",
            provide=lambda cache: [
                "In addition, a coworker has already written a 'threat model' for this application, which may include vulnerabilities/issues that"
                "are common in this type of application. This threat model is written for the entire application (not just the component you are analyzing) "
                "so some of the issues/vulnerabilities/attacks may not be relevant to your analysis. Do *NOT* overfit to this threat model; carefully "
                "analyze what content of the provided threat model is worth considering vs out of scope. Further, this threat model is just a starting point, "
                "you should ALSO look for threats *not* mentioned in this document.",
                threat_model.to_dict(with_cache=cache)
            ]
        ))


    agent_attempt = await _run_bug_analysis_inner(
        component_analysis.child(AGENT_RESULT_KEY),
        env,
        component,
        actual_extra_input,
        max_rounds=max_rounds,
        backend_guidance=backend_guidance,
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
