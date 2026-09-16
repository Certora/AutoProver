"""Ranking the whole candidate property set, and cutting it down to one focus.

Property inference runs per component and never sees more than the component it was
given, so nothing in the pipeline has ever compared two components' properties against
each other. A ``prioritized`` run needs exactly that comparison: one judgement, over
every candidate at once, of which property contributes most to the correctness of the
system and which of its neighbours are needed to state or discharge it.

That judgement is a single structured call (the shape ``report/grouping.py`` uses),
not an agent: an agent here would re-import the per-component cost the mode exists to
remove. Its output is validated in Python before anything acts on it — an unvalidated
ranking would silently redirect the run's entire formalization budget.

The model scores; it does not choose. Which property the run pursues falls out of those
scores here (:func:`priority`, :func:`select`), so the ranking the artifact records and the
property the run actually spends itself on are the same thing by construction rather than
by agreement.

The models live here rather than beside the cache key so ``pipeline/keys.py`` can
import them without a cycle (see ``spec/key_family.py``: registries import producers,
never the reverse), and the selection helper takes plain data so this module never
reaches back into the driver.
"""

import logging
from collections import Counter
from dataclasses import dataclass
from typing import Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from composer.input.files import Document
from composer.llm.types import CacheLevel
from composer.spec.types import ComponentName, PropertyFormulation, PropertyKey, PropertyTitle
from composer.templates.loader import load_jinja_template

_log = logging.getLogger(__name__)

#: What a property the user actually raised is worth against one we inferred unaided. A bounded
#: boost rather than a tiebreak or a dominator: a flagged concern should win at a comparable
#: score, but should not drag a trivial property past a critical one.
CRITICAL_MATCH_BONUS = 15


class RankedProperty(BaseModel):
    """One candidate property, scored."""
    key: PropertyKey = Field(
        description="The [component, title] pair identifying the property, copied exactly "
        "from the candidate listing."
    )
    score: int = Field(
        ge=0, le=100,
        description="How badly the protocol is hurt if this property does not hold. Score the "
        "consequence of a violation, not how hard the property looks to verify.",
    )
    critical_match: bool = Field(
        description="True if this property corresponds to a concern the reader of the design "
        "document, threat model, or supplied notes explicitly raised."
    )
    rationale: str = Field(
        description="One or two sentences justifying the score. Say what breaks if the "
        "property does not hold."
    )


class PropertyGroup(BaseModel):
    """Properties that together state one claim about the protocol."""
    claim: str = Field(
        description="The single thing these properties, taken together, establish about the "
        "protocol. One or two sentences, in terms an auditor would use — not a restatement of "
        "the property titles."
    )
    members: list[PropertyKey] = Field(
        min_length=1,
        description="The [component, title] pairs this claim is made of. All of them must "
        "belong to the same component. Every candidate property appears in exactly one group.",
    )


class PropertyRanking(BaseModel):
    """Every candidate scored, and the claims they group into."""
    ranked: list[RankedProperty] = Field(
        description="Every candidate property, each appearing exactly once."
    )
    groups: list[PropertyGroup] = Field(
        description="The claims the candidates group into. Every candidate appears in exactly "
        "one group."
    )
    justification: str = Field(
        description="Two to four sentences on which claim came out on top and why it is the one "
        "worth proving."
    )


def priority(rp: RankedProperty) -> int:
    """What the run sorts on. The scoring model reports two things it can judge — how much the
    property matters, and whether the reader asked for it — and this is the one place their
    trade-off is decided, so it is reviewable and the artifact can never disagree with the pick."""
    return rp.score + (CRITICAL_MATCH_BONUS if rp.critical_match else 0)


@dataclass(frozen=True)
class Candidate:
    """One component's extracted properties, as offered to the ranker.

    ``label`` is what the model sees and names in a :class:`PropertyKey`: the component's own
    display name, prefixed with ``unit_index`` so two components sharing a name are still
    distinguishable in the prompt and in the persisted ranking. ``unit_index`` is the stable
    per-run component id, and what the driver resolves against."""
    unit_index: int
    label: ComponentName
    props: list[PropertyFormulation]


@dataclass(frozen=True)
class Selection:
    """The focus: which unit survives, which of its properties, and the claim they state."""
    unit_index: int
    titles: list[PropertyTitle]
    claim: str
    ranking: PropertyRanking


def build_candidates(
    units: Sequence[tuple[int, ComponentName, list[PropertyFormulation]]]
) -> list[Candidate]:
    """Label every unit for the prompt.

    The label carries ``unit_index``, the stable per-run component id, so a ranking always names
    exactly one component even where two share a display name — and it needs no invented
    uniquifying suffix to do it."""
    return [
        Candidate(unit_index, ComponentName(f"{unit_index}: {name}"), props)
        for unit_index, name, props in units
    ]


def validate_ranking(candidates: Sequence[Candidate], r: PropertyRanking) -> str | None:
    """``None`` if the ranking is usable, else every reason it is not — phrased for the model,
    since it is fed straight back as the correction.

    All problems are reported at once. Returning only the first means a badly malformed ranking
    costs one attempt per mistake, and the attempts run out before the mistakes do.

    There is no "did it pick the right one" check, because nothing picks: the focus is derived
    from these scores by :func:`select`."""
    by_key: dict[PropertyKey, Candidate] = {
        (c.label, p.title): c for c in candidates for p in c.props
    }
    problems: list[str] = []

    seen: Counter[PropertyKey] = Counter(rp.key for rp in r.ranked)
    if (unknown := [k for k in seen if k not in by_key]):
        problems.append(
            f"These ranked entries name properties that are not in the candidate listing: "
            f"{unknown}. Use the [component, title] pairs exactly as given."
        )
    if (dupes := [k for k, n in seen.items() if n > 1]):
        problems.append(
            f"These properties appear more than once in `ranked`: {dupes}. Rank each exactly once."
        )
    if (missing := [k for k in by_key if k not in seen]):
        problems.append(
            f"These candidate properties are missing from `ranked`: {missing}. "
            "Every candidate must be ranked, including the ones you consider unimportant."
        )

    grouped: Counter[PropertyKey] = Counter(k for g in r.groups for k in g.members)
    if (unknown_g := [k for k in grouped if k not in by_key]):
        problems.append(
            f"These group members are not in the candidate listing: {unknown_g}."
        )
    if (dupes_g := [k for k, n in grouped.items() if n > 1]):
        problems.append(
            f"These properties are in more than one group: {dupes_g}. Each property belongs to "
            "exactly one claim."
        )
    if (ungrouped := [k for k in by_key if k not in grouped]):
        problems.append(
            f"These candidate properties are in no group: {ungrouped}. Every property belongs to "
            "exactly one claim, even if that claim has only one member."
        )

    for g in r.groups:
        homes = {by_key[k].label for k in g.members if k in by_key}
        if len(homes) > 1:
            problems.append(
                f"The group {g.claim!r} spans components {sorted(homes)}. A run pursues one "
                "component's properties, so every member of a group must come from the same "
                "component."
            )

    return "\n".join(problems) if problems else None


def select(candidates: Sequence[Candidate], r: PropertyRanking) -> Selection:
    """Derive the focus from a *validated* ranking: the group containing the highest-priority
    property, which is the claim worth the run.

    A group is worth what its best member is worth, so the single most important property still
    decides, and whatever else states the same claim comes with it. ``max`` keeps the first of
    equal-priority entries, so the model's own ordering breaks ties — but the choice is ours,
    which is what makes ``property_ranking.json`` and the run's actual subject the same thing by
    construction.

    The whole group is pursued. A claim is established by all the properties that state it or by
    none of them, so there is no size at which dropping one is the right trade: a partial batch
    spends most of the budget and leaves the claim open. The saving comes from the group being
    one claim out of the whole candidate set, not from trimming the claim itself."""
    best = max(r.ranked, key=priority)
    group = next(g for g in r.groups if best.key in g.members)
    home = next(c for c in candidates if c.label == best.key[0])

    by_priority = {rp.key: priority(rp) for rp in r.ranked}
    members = sorted(group.members, key=lambda k: -by_priority[k])
    return Selection(home.unit_index, [k[1] for k in members], group.claim, r)


async def rank_properties(
    *,
    llm: BaseChatModel,
    contract_name: str,
    candidates: Sequence[Candidate],
    design_doc: Document | None,
    threat_model: Document | None,
    extra_context: Sequence[Document],
    max_attempts: int = 2,
) -> PropertyRanking:
    """One structured call, validated, with a single corrective retry.

    A ranking that will not validate raises rather than falling back to the whole
    candidate set: a silent widening would spend exactly the budget the caller asked to
    save, and failing here is cheap — nothing has been formalized yet."""
    system = load_jinja_template("property_prioritization_system.j2")
    user = load_jinja_template(
        "property_prioritization_prompt.j2",
        contract_name=contract_name,
        candidates=candidates,
    )
    docs = [d for d in (design_doc, threat_model, *extra_context) if d is not None]
    content: list[dict | str] = [
        {"type": "text", "text": user},
        *(d.to_dict(CacheLevel.SHORT) for d in docs),
    ]
    bound = llm.with_structured_output(PropertyRanking)

    messages: list[BaseMessage] = [SystemMessage(system), HumanMessage(content=content)]
    problem: str | None = None
    for attempt in range(max_attempts):
        result = await bound.ainvoke(messages)
        assert isinstance(result, PropertyRanking)
        problem = validate_ranking(candidates, result)
        if problem is None:
            return result
        _log.warning("property ranking rejected (attempt %d): %s", attempt + 1, problem)
        # Structured output hands back a parsed object and leaves no assistant turn behind, so
        # without replaying it the model is asked to fix a ranking it has no memory of making.
        messages = [
            *messages,
            AIMessage(content=result.model_dump_json()),
            HumanMessage(
                f"That ranking was rejected:\n\n{problem}\n\nProduce a corrected ranking."
            ),
        ]
    raise ValueError(f"Property ranking did not validate after {max_attempts} attempts: {problem}")
