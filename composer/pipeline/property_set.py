"""A run's resolved property set: the analyzed system model and the per-component properties
handed to formalization.

The whole system model is kept, not a per-component slice, because a unit is a view into it:
EVM's ``ContractComponentInstance`` reads its siblings, interactions and parent contract through
the application. Restoring the model and re-enumerating its units rebuilds the same units.

Units are matched by name and index only, so a set replayed against changed sources applies its
properties to the changed code. The set records the target's commit, and a replay warns when the
checkout has moved past it (:meth:`PropertySet.check_target`). A warning rather than a refusal: the
analysis may still describe a checkout whose new commit touched other code, and a set refused on
every unrelated commit would be rewritten reflexively, which is how a stale one gets blessed.
"""

import logging
import subprocess
from pathlib import Path
from typing import Annotated, Any, Literal

from collections.abc import Collection

from pydantic import BaseModel, Field, ValidationError, model_validator

from composer.pipeline.ecosystem import ChainTag
from composer.pipeline.run_mode import RunMode
from composer.spec.system_model import BaseApplication, FeatureUnit
from composer.spec.types import (
    ComponentName, PropertyFormulation, PropertyKey, PropertyTitle, ScopedPropertyFormulation,
    SourceIdentifier,
)

_log = logging.getLogger(__name__)

#: A property as the set holds it. A prioritized run's narrowed property keeps its ``checked_at``:
#: replayed without it, the property would be checked at every entry point it names, a wider claim
#: than the run that wrote the set made.
type RecordedProperty = Annotated[
    ScopedPropertyFormulation | PropertyFormulation, Field(union_mode="left_to_right")
]


class PropertySetComponent(BaseModel):
    """One unit's identity among the rebuilt units, and its final properties."""
    name: ComponentName
    unit_index: int
    properties: list[RecordedProperty]


class PropertyRank(BaseModel):
    """A property's place in its set's importance ranking."""
    component: ComponentName
    title: PropertyTitle
    #: 1 is the most important.
    rank: int = Field(ge=1)


class PropertySet(BaseModel):
    schema_version: Literal[1] = 1
    ecosystem: ChainTag
    contract_name: SourceIdentifier
    #: The mode of the run that wrote the set. A prioritized set holds only the chosen focus.
    run_mode: RunMode
    #: The analyzed model (``Ecosystem.system_model``), dumped with ``model_dump(mode="json")``.
    system_model: dict[str, Any]
    #: The batches handed to formalization, in order — after the post-inference plugins and, in a
    #: prioritized run, after the focus was chosen.
    property_components: list[PropertySetComponent]
    #: The importance ranking of the set's properties, from a property generator that ranks them;
    #: ``None`` when the set is unranked. Every entry names a property of the set, at most once.
    #: A ranking may leave properties out, and its ranks may have gaps.
    ranking: list[PropertyRank] | None = None
    #: The target checkout's commit when the set was written; ``None`` when the target is not a git
    #: checkout.
    target_commit: str | None = None

    @model_validator(mode="after")
    def _ranking_names_the_set_properties(self) -> "PropertySet":
        if self.ranking is None:
            return self
        keys = self.keys()
        seen: set[PropertyKey] = set()
        for r in self.ranking:
            key = (r.component, r.title)
            if key not in keys:
                raise ValueError(
                    f"the ranking names {r.title!r} of {r.component!r}, which the set does not hold"
                )
            if key in seen:
                raise ValueError(f"the ranking names {r.title!r} of {r.component!r} more than once")
            seen.add(key)
        return self

    def keys(self) -> set[PropertyKey]:
        """The ``(component, title)`` of every property in the set."""
        return {(c.name, p.title) for c in self.property_components for p in c.properties}

    def ranking_of(self, keys: Collection[PropertyKey]) -> list[PropertyRank] | None:
        """This set's ranking restricted to ``keys``, for a set that holds only those properties;
        ``None`` when this set is unranked."""
        if self.ranking is None:
            return None
        return [r for r in self.ranking if (r.component, r.title) in keys]

    @staticmethod
    def load(path: Path) -> "PropertySet":
        try:
            return PropertySet.model_validate_json(path.read_text())
        except ValidationError as e:
            raise ValueError(f"invalid property set {path}: {e}") from e

    def loadable_in(self, run_mode: RunMode) -> bool:
        """Whether a run in ``run_mode`` may load this set. A comprehensive, an evaluate and a
        property-generation set are the same thing: every inferred property of every component.
        A prioritized set holds only the chosen focus, so it loads only into a prioritized run."""
        if self.run_mode is run_mode:
            return True
        unranked = {RunMode.COMPREHENSIVE, RunMode.EVALUATE, RunMode.PROPERTY_GENERATION}
        return self.run_mode in unranked and run_mode in unranked

    def check_target(self, project_root: Path) -> None:
        """Warn when ``project_root`` is a checkout at a commit other than the one the set was
        written at."""
        if self.target_commit is None:
            return
        head = git_head(project_root)
        if head is None or head == self.target_commit:
            return
        _log.warning(
            "the property set was written at %s but %s is at %s; its analysis may no longer "
            "describe this source",
            self.target_commit[:12], project_root, head[:12],
        )

    def analyzed[App: BaseApplication](self, ty: type[App]) -> App:
        return ty.model_validate(self.system_model)

    def match_units[U: FeatureUnit](
        self, units: list[U]
    ) -> list[tuple[U, list[PropertyFormulation]]]:
        """Pair each recorded component with the rebuilt unit of the same name and index.

        Any disagreement is fatal: a component formalized against the wrong unit, or silently
        dropped, would make the run's CVL incomparable with the run that wrote the file."""
        by_name: dict[str, U] = {}
        ambiguous: set[str] = set()
        for u in units:
            if u.display_name in by_name:
                ambiguous.add(u.display_name)
            by_name[u.display_name] = u
        paired: list[tuple[U, list[PropertyFormulation]]] = []
        for c in self.property_components:
            if c.name in ambiguous:
                raise ValueError(f"property set component {c.name!r} matches several units")
            unit = by_name.get(c.name)
            if unit is None:
                raise ValueError(
                    f"property set component {c.name!r} matches no unit "
                    f"(available: {sorted(by_name)})"
                )
            if unit.unit_index != c.unit_index:
                raise ValueError(
                    f"property set component {c.name!r} has unit index {c.unit_index}, "
                    f"but the rebuilt unit has {unit.unit_index}"
                )
            paired.append((unit, [*c.properties]))
        return paired


def git_head(project_root: Path) -> str | None:
    """The checkout's commit, or ``None`` when it is not a git checkout or git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None
