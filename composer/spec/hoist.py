"""Moving properties that span several units into one shared unit, so each is formalized once.

Property inference runs per unit. Once a unit may state a property over its siblings'
instructions too, several units state the same protocol-wide property, each in its own words:
solvency and lamport conservation each came out of half a dozen components on the stake program
(``docs/cvlr-design-properties-plan.md``, step 2). Formalized as they stand, the hardest property
in the run would be paid for once per unit that stated it.

This step sees every unit's properties at once, groups the ones that state the same protocol-wide
claim, and keeps one member of each group, verbatim, for a shared unit. The rest of each group is
dropped from its unit. A unit-local property stays where it is.

Usually a group's shared property is one of its members, exactly as extracted. Extraction is not
reliable about stating a claim whole, though: one run stated lamport conservation once over every
instruction, the next only per instruction group. So when no member states the whole claim, the
model may write one property that is exactly the conjunction of its members, over the union of the
entry points they name. Validation holds it to that union, and :class:`Hoisted` records which
shared properties were written here rather than extracted. Like :mod:`composer.spec.prioritize`,
this is one structured call validated in Python.
Unlike prioritization, a plan that will not validate does not fail the run: the caller formalizes
the units unhoisted, which costs duplicate work and loses nothing.
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import Literal, Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from composer.spec.types import ComponentName, PropertyFormulation, PropertyKey, PropertyTitle
from composer.templates.loader import load_jinja_template

_log = logging.getLogger(__name__)


class Chosen(BaseModel):
    """The group's shared property is one of its members, exactly as extracted."""

    kind: Literal["chosen"] = "chosen"
    key: PropertyKey = Field(
        description="The [component, title] pair of the member that states the claim most strongly "
        "and over the most entry points. It must imply every other member."
    )


class Synthesized(BaseModel):
    """The group's shared property is written here, because no member states the whole claim."""

    kind: Literal["synthesized"] = "synthesized"
    property: PropertyFormulation = Field(
        description="Exactly the conjunction of the members, stated once over the union of the "
        "entry points they name, naming every one of them and no other. It must not strengthen any "
        "member's condition or add a claim none of them makes."
    )


class HoistGroup(BaseModel):
    """Properties from one or more units that state one protocol-wide claim."""

    claim: str = Field(
        description="The protocol-wide claim these properties state, in one sentence."
    )
    shared: Chosen | Synthesized = Field(
        discriminator="kind",
        description="The property formalized in place of the whole group. Choose a member when one "
        "states the whole claim; synthesize only when none does.",
    )
    members: list[PropertyKey] = Field(
        min_length=1,
        description="Every [component, title] pair in the group, a chosen property included.",
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
    #: Every property moved out of its unit, paired with the title of the shared property that now
    #: stands for it. A chosen property is paired with its own title.
    absorbed: list[tuple[PropertyKey, PropertyTitle]]
    #: The shared properties written by the hoist rather than extracted.
    synthesized: list[PropertyTitle]


def build_candidates(
    units: Sequence[tuple[int, ComponentName, list[str], list[PropertyFormulation]]],
) -> list[HoistCandidate]:
    return [
        HoistCandidate(unit_index, ComponentName(f"{unit_index}: {name}"), entry_points, props)
        for unit_index, name, entry_points, props in units
    ]


def _named(text: str, names: set[str]) -> set[str]:
    """The entry points in ``names`` that ``text`` names, each matched as a whole identifier."""
    return {
        name
        for name in names
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text)
    }


def _shared_title(g: HoistGroup) -> PropertyTitle:
    match g.shared:
        case Chosen(key=(_, title)):
            return title
        case Synthesized(property=prop):
            return prop.title


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

    for g in plan.groups:
        match g.shared:
            case Chosen(key=key):
                if key not in g.members:
                    problems.append(
                        f"The group {g.claim!r} chooses a property that is not one of its members."
                    )
                elif key in by_key:
                    home, prop = by_key[key]
                    if not _named(prop.description, everyone - set(home.entry_points)):
                        problems.append(
                            f"The property chosen for {g.claim!r}, {key}, names no entry point "
                            f"outside its own component ({', '.join(home.entry_points)}). Only a "
                            "property that ranges over other components' entry points is "
                            "protocol-wide. Choose a member that does, synthesize one, or leave "
                            "the group out."
                        )
            case Synthesized(property=prop):
                members = [by_key[k][1] for k in g.members if k in by_key]
                if len(g.members) < 2:
                    problems.append(
                        f"The group {g.claim!r} synthesizes a property from one member. Choose "
                        "that member instead."
                    )
                # Names a member mentions are an upper bound on its scope, not the scope: a member
                # also names instructions it excludes ("every handler except `delegate_stake`").
                # So the property may not name anything no member names, and must keep a named
                # entry point of every member.
                named = [_named(m.description, everyone) for m in members]
                stated = _named(prop.description, everyone)
                if extra := stated - set().union(*named):
                    problems.append(
                        f"The property synthesized for {g.claim!r} names entry points no member "
                        f"names: {sorted(extra)}. It may only state what its members state."
                    )
                if dropped := [
                    k[1] for k, n in zip((k for k in g.members if k in by_key), named)
                    if n and not (n & stated)
                ]:
                    problems.append(
                        f"The property synthesized for {g.claim!r} names none of the entry points "
                        f"of these members: {dropped}. It must cover every member of its group, or "
                        "they must leave the group."
                    )
                if any(stated <= set(c.entry_points) for c in candidates):
                    problems.append(
                        f"The property synthesized for {g.claim!r} ranges over one component's "
                        "entry points, so it is not protocol-wide. Leave the group out."
                    )

    if clashes := [t for t, n in Counter(_shared_title(g) for g in plan.groups).items() if n > 1]:
        problems.append(
            f"Two groups' shared properties have the same title: {clashes}. The shared unit names "
            "its properties by title, so give them different titles."
        )

    return "\n".join(problems) if problems else None


def apply_plan(candidates: Sequence[HoistCandidate], plan: HoistPlan) -> Hoisted:
    """Apply a *validated* plan. Each unit keeps its properties in their original order."""
    by_key = {(c.label, p.title): p for c in candidates for p in c.props}
    placed = {k for g in plan.groups for k in g.members}
    shared: list[PropertyFormulation] = []
    synthesized: list[PropertyTitle] = []
    for g in plan.groups:
        match g.shared:
            case Chosen(key=key):
                shared.append(by_key[key])
            case Synthesized(property=prop):
                shared.append(prop)
                synthesized.append(prop.title)
    return Hoisted(
        shared=shared,
        kept={
            c.unit_index: [p for p in c.props if (c.label, p.title) not in placed]
            for c in candidates
        },
        absorbed=[(m, _shared_title(g)) for g in plan.groups for m in g.members],
        synthesized=synthesized,
    )


def salvage(candidates: Sequence[HoistCandidate], plan: HoistPlan) -> HoistPlan:
    """The groups of ``plan`` that are valid on their own and do not overlap an earlier kept group.

    A dropped group's members stay in their units, unhoisted, which is what every member would get
    if the whole plan were discarded. One group the model will not get right should not cost the
    others."""
    kept: list[HoistGroup] = []
    for g in plan.groups:
        trial = HoistPlan(groups=[*kept, g], justification=plan.justification)
        if validate_plan(candidates, trial) is None:
            kept.append(g)
    return HoistPlan(groups=kept, justification=plan.justification)


async def plan_hoist(
    *,
    llm: BaseChatModel,
    contract_name: str,
    candidates: Sequence[HoistCandidate],
    max_attempts: int = 3,
) -> HoistPlan:
    """One structured call, validated, with corrective retries. If no attempt validates, the last
    attempt's valid groups are kept (:func:`salvage`); ``ValueError`` only if none are."""
    system = load_jinja_template("property_hoisting_system.j2")
    user = load_jinja_template(
        "property_hoisting_prompt.j2", contract_name=contract_name, candidates=candidates
    )
    bound = llm.with_structured_output(HoistPlan)

    messages: list[BaseMessage] = [SystemMessage(system), HumanMessage(user)]
    problem: str | None = None
    last = HoistPlan(groups=[], justification="")
    for attempt in range(max_attempts):
        result = await bound.ainvoke(messages)
        assert isinstance(result, HoistPlan)
        last = result
        problem = validate_plan(candidates, result)
        if problem is None:
            return result
        _log.warning("hoist plan rejected (attempt %d): %s", attempt + 1, problem)
        messages = [
            *messages,
            AIMessage(content=result.model_dump_json()),
            HumanMessage(f"That plan was rejected:\n\n{problem}\n\nProduce a corrected plan."),
        ]
    salvaged = salvage(candidates, last)
    if not salvaged.groups:
        raise ValueError(f"Hoist plan did not validate after {max_attempts} attempts: {problem}")
    _log.warning(
        "hoist plan kept %d of %d groups after %d attempts; the rest stay in their units: %s",
        len(salvaged.groups), len(last.groups), max_attempts, problem,
    )
    return salvaged
