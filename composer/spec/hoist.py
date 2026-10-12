"""Moving properties that span several units into one shared unit, so each is formalized once.

Property inference runs per unit. Once a unit may state a property over its siblings'
instructions too, several units state the same protocol-wide property, each in its own words:
solvency and lamport conservation each came out of half a dozen components on the stake program
(``docs/cvlr-design-properties-plan.md``, step 2). Formalized as they stand, the hardest property
in the run would be paid for once per unit that stated it.

This step sees every unit's properties at once, groups the ones that state the same protocol-wide
claim, and keeps one member of each group, verbatim, for a shared unit. The rest of each group is
dropped from its unit. A unit-local property stays where it is.

The model groups and chooses; it never writes a property. A representative is one of the
extracted properties exactly as extracted, so nothing reaches formalization that extraction did not
produce. Like :mod:`composer.spec.prioritize`, this is one structured call validated in Python.
Unlike prioritization, a plan that will not validate does not fail the run: the caller formalizes
the units unhoisted, which costs duplicate work and loses nothing.
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from composer.spec.types import ComponentName, PropertyFormulation, PropertyKey
from composer.templates.loader import load_jinja_template

_log = logging.getLogger(__name__)


class HoistGroup(BaseModel):
    """Properties from one or more units that state one protocol-wide claim."""

    claim: str = Field(
        description="The protocol-wide claim these properties state, in one sentence."
    )
    representative: PropertyKey = Field(
        description="The [component, title] pair of the member that states the claim most "
        "strongly and over the most entry points. It is formalized in place of the whole group, "
        "exactly as written, so it must imply every other member."
    )
    members: list[PropertyKey] = Field(
        min_length=1,
        description="Every [component, title] pair in the group, the representative included.",
    )


class HoistPlan(BaseModel):
    """The protocol-wide claims among the candidates. Properties in no group stay in their unit."""

    groups: list[HoistGroup] = Field(
        description="One group per protocol-wide claim. Leave out every property whose scope is "
        "its own component's entry points."
    )
    justification: str = Field(
        description="Two to four sentences on what was grouped and what was left in place."
    )


@dataclass(frozen=True)
class HoistCandidate:
    """One unit's extracted properties, as offered to the model.

    ``label`` carries ``unit_index`` for the same reason
    :func:`composer.spec.prioritize.build_candidates` gives: two units may share a display name.
    ``entry_points`` are the names the unit's properties may range over, which is how a property's
    scope is told apart from its unit's."""

    unit_index: int
    label: ComponentName
    entry_points: list[str]
    props: list[PropertyFormulation]


@dataclass(frozen=True)
class Hoisted:
    """A validated plan, applied: the shared unit's properties and what each unit keeps."""

    shared: list[PropertyFormulation]
    kept: dict[int, list[PropertyFormulation]]
    #: Every property moved out of its unit, paired with the shared property that now stands for
    #: it. A representative is paired with itself.
    absorbed: list[tuple[PropertyKey, PropertyKey]]


def build_candidates(
    units: Sequence[tuple[int, ComponentName, list[str], list[PropertyFormulation]]],
) -> list[HoistCandidate]:
    return [
        HoistCandidate(unit_index, ComponentName(f"{unit_index}: {name}"), entry_points, props)
        for unit_index, name, entry_points, props in units
    ]


def _names_outside(text: str, own: set[str], everyone: set[str]) -> bool:
    """Whether ``text`` names an entry point that is not its own unit's."""
    return any(
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text)
        for name in everyone - own
    )


def validate_plan(candidates: Sequence[HoistCandidate], plan: HoistPlan) -> str | None:
    """``None`` if the plan is usable, else every reason it is not, phrased for the model."""
    by_key: dict[PropertyKey, tuple[HoistCandidate, PropertyFormulation]] = {
        (c.label, p.title): (c, p) for c in candidates for p in c.props
    }
    everyone = {e for c in candidates for e in c.entry_points}
    problems: list[str] = []

    placed: Counter[PropertyKey] = Counter(k for g in plan.groups for k in g.members)
    if unknown := [k for k in placed if k not in by_key]:
        problems.append(
            f"These group members are not in the candidate listing: {unknown}. Use the "
            "[component, title] pairs exactly as given."
        )
    if dupes := [k for k, n in placed.items() if n > 1]:
        problems.append(
            f"These properties are in more than one group: {dupes}. Each belongs to at most one."
        )

    representatives: Counter[str] = Counter()
    for g in plan.groups:
        if g.representative not in g.members:
            problems.append(
                f"The group {g.claim!r} names a representative that is not one of its members."
            )
            continue
        if g.representative not in by_key:
            continue
        home, prop = by_key[g.representative]
        representatives[prop.title] += 1
        if not _names_outside(prop.description, set(home.entry_points), everyone):
            problems.append(
                f"The representative of {g.claim!r}, {g.representative}, names no entry point "
                f"outside its own component ({', '.join(home.entry_points)}). Only a property "
                "that ranges over other components' entry points is protocol-wide. Choose a "
                "member that does, or leave the group out."
            )
    if clashes := [t for t, n in representatives.items() if n > 1]:
        problems.append(
            f"Two groups' representatives share a title: {clashes}. The shared unit names its "
            "properties by title, so choose representatives with different titles."
        )

    return "\n".join(problems) if problems else None


def apply_plan(candidates: Sequence[HoistCandidate], plan: HoistPlan) -> Hoisted:
    """Apply a *validated* plan. Each unit keeps its properties in their original order."""
    by_key = {(c.label, p.title): p for c in candidates for p in c.props}
    placed = {k for g in plan.groups for k in g.members}
    return Hoisted(
        shared=[by_key[g.representative] for g in plan.groups],
        kept={
            c.unit_index: [p for p in c.props if (c.label, p.title) not in placed]
            for c in candidates
        },
        absorbed=[(m, g.representative) for g in plan.groups for m in g.members],
    )


async def plan_hoist(
    *,
    llm: BaseChatModel,
    contract_name: str,
    candidates: Sequence[HoistCandidate],
    max_attempts: int = 2,
) -> HoistPlan:
    """One structured call, validated, with a single corrective retry. Raises ``ValueError`` if
    no attempt validates; the caller decides what that costs."""
    system = load_jinja_template("property_hoisting_system.j2")
    user = load_jinja_template(
        "property_hoisting_prompt.j2", contract_name=contract_name, candidates=candidates
    )
    bound = llm.with_structured_output(HoistPlan)

    messages: list[BaseMessage] = [SystemMessage(system), HumanMessage(user)]
    problem: str | None = None
    for attempt in range(max_attempts):
        result = await bound.ainvoke(messages)
        assert isinstance(result, HoistPlan)
        problem = validate_plan(candidates, result)
        if problem is None:
            return result
        _log.warning("hoist plan rejected (attempt %d): %s", attempt + 1, problem)
        messages = [
            *messages,
            AIMessage(content=result.model_dump_json()),
            HumanMessage(f"That plan was rejected:\n\n{problem}\n\nProduce a corrected plan."),
        ]
    raise ValueError(f"Hoist plan did not validate after {max_attempts} attempts: {problem}")
