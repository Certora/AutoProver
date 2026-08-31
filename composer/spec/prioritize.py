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
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from composer.input.files import Document
from composer.llm.types import CacheLevel
from composer.spec.types import ComponentName, PropertyFormulation, PropertyKey, PropertyTitle
from composer.templates.loader import load_jinja_template

_log = logging.getLogger(__name__)

#: Ceiling on the supporting cluster. The primary property plus its lemmas is meant to stay a
#: focused unit of work; past a couple of neighbours the batch is a component again and the mode
#: has bought nothing.
MAX_SUPPORTING = 2

#: What a property the user actually raised is worth against one we inferred unaided. A bounded
#: boost rather than a tiebreak or a dominator: a flagged concern should win at a comparable
#: score, but should not drag a trivial property past a critical one.
CRITICAL_MATCH_BONUS = 15


class RankedProperty(BaseModel):
    """One candidate's place in the ranking, and what it rests on."""
    key: PropertyKey = Field(
        description="The [component, title] pair identifying the property, copied exactly "
        "from the candidate listing."
    )
    score: int = Field(
        ge=0, le=100,
        description="How much proving this property would contribute to confidence in the "
        "overall correctness of the system, 0-100. Reserve the top of the range for "
        "properties whose violation would be a critical bug. Score the property's importance, "
        "not how hard it looks to verify.",
    )
    critical_match: bool = Field(
        description="True if this property corresponds to a concern the user explicitly "
        "raised in the design document, threat model, or supplied context."
    )
    depends_on: list[PropertyTitle] = Field(
        default_factory=list,
        description="Titles of properties IN THIS PROPERTY'S OWN COMPONENT that would be needed "
        "to state or discharge it: the lemmas it assumes, the invariants its argument leans on. "
        "Not properties that are merely related to it. Leave empty if it stands alone.",
    )
    rationale: str = Field(
        description="One or two sentences justifying the score. Say what breaks if the "
        "property does not hold."
    )


class PropertyRanking(BaseModel):
    """Every candidate, scored. Which one the run pursues is derived from this, not stated
    alongside it: see :func:`priority` and :func:`select`."""
    ranked: list[RankedProperty] = Field(
        description="Every candidate property, each appearing exactly once."
    )
    justification: str = Field(
        description="Two to four sentences on which properties came out on top and why they are "
        "the ones worth a verification budget."
    )


def priority(rp: RankedProperty) -> int:
    """What the run actually sorts on. The scoring model reports two things it can judge — how much
    the property matters, and whether the user asked for it — and this is the one place their
    trade-off is decided, so it is reviewable and the artifact can never disagree with the pick."""
    return rp.score + (CRITICAL_MATCH_BONUS if rp.critical_match else 0)


@dataclass(frozen=True)
class Candidate:
    """One component's extracted properties, as offered to the ranker.

    ``label`` is what the model sees and names in a :class:`PropertyKey`. It is derived
    from the component's display name but forced unique across the run: display names are
    free text the analysis agent produced under no uniqueness constraint, so resolving a
    ranking back onto components by name alone can land on the wrong one. ``unit_index``
    is the identity the driver resolves against."""
    unit_index: int
    label: ComponentName
    props: list[PropertyFormulation]


@dataclass(frozen=True)
class Selection:
    """The focus: which unit survives, and which of its properties, in order."""
    unit_index: int
    titles: list[PropertyTitle]
    ranking: PropertyRanking


def build_candidates(
    units: Sequence[tuple[int, ComponentName, list[PropertyFormulation]]]
) -> list[Candidate]:
    """Label every unit for the prompt, uniquely.

    Counting repeats is not enough: a component may itself be named ``Vault (2)``, and then a
    second ``Vault`` numbered by count collides with it. Since the label is what the ranking names
    a property by, a collision resolves a ranked entry onto the wrong component's batch. Suffix
    until the label is actually unused instead, and assert it."""
    taken: set[str] = set()
    out: list[Candidate] = []
    for unit_index, name, props in units:
        label, n = name, 1
        while label in taken:
            n += 1
            label = f"{name} ({n})"
        taken.add(label)
        out.append(Candidate(unit_index, ComponentName(label), props))
    assert len({c.label for c in out}) == len(out), "component labels must be unique"
    return out


def validate_ranking(candidates: Sequence[Candidate], r: PropertyRanking) -> str | None:
    """``None`` if the ranking is usable, else the reason it is not — phrased for the model,
    since it is fed straight back as the retry prompt.

    There is no "did it pick the right one" check here, because nothing picks: the focus is
    derived from these scores by :func:`select`."""
    by_key: dict[PropertyKey, Candidate] = {
        (c.label, p.title): c for c in candidates for p in c.props
    }

    seen: Counter[PropertyKey] = Counter(rp.key for rp in r.ranked)
    unknown = [k for k in seen if k not in by_key]
    if unknown:
        return (
            "These ranked entries name properties that are not in the candidate listing: "
            f"{unknown}. Use the [component, title] pairs exactly as given."
        )
    if (dupes := [k for k, n in seen.items() if n > 1]):
        return f"These properties appear more than once in `ranked`: {dupes}. Rank each exactly once."
    if (missing := [k for k in by_key if k not in seen]):
        return (
            f"These candidate properties are missing from `ranked`: {missing}. "
            "Every candidate must be ranked, including the ones you consider unimportant."
        )

    for rp in r.ranked:
        home = by_key[rp.key]
        siblings = {p.title for p in home.props}
        for dep in rp.depends_on:
            if dep == rp.key[1]:
                return (
                    f"{rp.key} lists itself in `depends_on`. A property does not depend on itself."
                )
            if dep not in siblings:
                return (
                    f"{rp.key} lists {dep!r} in `depends_on`, which is not a property of its own "
                    f"component ({home.label}). A run pursues one component's properties, so a "
                    "dependency must come from the same component as the property that needs it."
                )
    return None


def select(candidates: Sequence[Candidate], r: PropertyRanking) -> Selection:
    """Derive the focus from a *validated* ranking: the highest-priority property, plus what it
    said it rests on, deduplicated and truncated to :data:`MAX_SUPPORTING`.

    ``max`` keeps the first of equal-priority entries, so the model's own ordering still breaks
    ties, but the choice itself is ours — which is what makes ``property_ranking.json`` and the
    run's actual subject the same thing by construction."""
    primary = max(r.ranked, key=priority)
    home = next(c for c in candidates if c.label == primary.key[0])

    titles: list[PropertyTitle] = [primary.key[1]]
    for dep in primary.depends_on:
        if len(titles) - 1 == MAX_SUPPORTING:
            break
        if dep in titles:
            continue
        titles.append(dep)

    # Emit them in the component's own order so the author reads them as it would any
    # batch, with the primary first.
    order = {p.title: i for i, p in enumerate(home.props)}
    head, tail = titles[0], sorted(titles[1:], key=lambda t: order[t])
    return Selection(home.unit_index, [head, *tail], r)


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
        max_supporting=MAX_SUPPORTING,
    )
    docs = [d for d in (design_doc, threat_model, *extra_context) if d is not None]
    content: list[dict | str] = [
        {"type": "text", "text": user},
        *(d.to_dict(CacheLevel.SHORT) for d in docs),
    ]
    bound = llm.with_structured_output(PropertyRanking)

    messages: list = [SystemMessage(system), HumanMessage(content=content)]
    problem: str | None = None
    for attempt in range(max_attempts):
        result = await bound.ainvoke(messages)
        assert isinstance(result, PropertyRanking)
        problem = validate_ranking(candidates, result)
        if problem is None:
            return result
        _log.warning("property ranking rejected (attempt %d): %s", attempt + 1, problem)
        messages = [
            *messages,
            HumanMessage(
                f"Your ranking was rejected: {problem}\n\nProduce a corrected ranking."
            ),
        ]
    raise ValueError(f"Property ranking did not validate after {max_attempts} attempts: {problem}")
