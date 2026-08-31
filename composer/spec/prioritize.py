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

#: Ceiling on the supporting cluster. The primary property plus its lemmas is meant to
#: stay a focused unit of work; past a handful of neighbours the batch is a component
#: again and the mode has bought nothing.
MAX_SUPPORTING = 4


class RankedProperty(BaseModel):
    """One candidate's place in the ranking."""
    key: PropertyKey = Field(
        description="The [component, title] pair identifying the property, copied exactly "
        "from the candidate listing."
    )
    score: int = Field(
        ge=0, le=100,
        description="How much proving this property would contribute to confidence in the "
        "overall correctness of the system, 0-100. Reserve the top of the range for "
        "properties whose violation would be a critical bug.",
    )
    critical_match: bool = Field(
        description="True if this property corresponds to a concern the user explicitly "
        "raised in the design document, threat model, or supplied context."
    )
    rationale: str = Field(
        description="One or two sentences justifying the score. Say what breaks if the "
        "property does not hold."
    )


class PropertyRanking(BaseModel):
    """The full ranking plus the focus drawn from it."""
    ranked: list[RankedProperty] = Field(
        description="Every candidate property, highest score first. Each candidate must "
        "appear exactly once."
    )
    primary: PropertyKey = Field(
        description="The single property the run will pursue: the best combination of "
        "contribution to overall correctness and match to the user's stated concerns."
    )
    supporting: list[PropertyKey] = Field(
        description="Properties from the SAME component as the primary that are needed to "
        "state or discharge it — lemmas it rests on, invariants it assumes. Not merely "
        f"related properties. Leave empty if the primary stands alone. At most {MAX_SUPPORTING}."
    )
    justification: str = Field(
        description="Two to four sentences on why this property is the one worth the run's "
        "whole verification budget, and what the supporting properties contribute to it."
    )


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
    """Label every unit uniquely for the prompt. A name shared by two components gets a
    disambiguating suffix rather than being silently merged."""
    seen: Counter[str] = Counter()
    out: list[Candidate] = []
    for unit_index, name, props in units:
        seen[name] += 1
        label = name if seen[name] == 1 else f"{name} ({seen[name]})"
        out.append(Candidate(unit_index, ComponentName(label), props))
    return out


def validate_ranking(candidates: Sequence[Candidate], r: PropertyRanking) -> str | None:
    """``None`` if the ranking is usable, else the reason it is not — phrased for the model,
    since it is fed straight back as the retry prompt."""
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

    primary = r.primary
    if primary not in by_key:
        return f"`primary` {primary} is not one of the candidate properties."

    home = by_key[primary]
    for key in r.supporting:
        if key not in by_key:
            return f"`supporting` entry {key} is not one of the candidate properties."
        if by_key[key].unit_index != home.unit_index:
            return (
                f"`supporting` entry {key} belongs to a different component than the primary "
                f"({home.label}). A run pursues one component's properties, so every supporting "
                "property must come from the primary's own component."
            )
    return None


def select(candidates: Sequence[Candidate], r: PropertyRanking) -> Selection:
    """Turn a *validated* ranking into the focus. Deduplicates the supporting cluster,
    drops the primary from it, and truncates to :data:`MAX_SUPPORTING`."""
    primary = r.primary
    home = next(
        c for c in candidates if any((c.label, p.title) == primary for p in c.props)
    )

    titles: list[PropertyTitle] = [primary[1]]
    for key in r.supporting:
        if len(titles) - 1 == MAX_SUPPORTING:
            break
        if key[1] in titles:
            continue
        titles.append(key[1])

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
