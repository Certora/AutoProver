"""A run's resolved property set: the analyzed system model and the per-component properties
handed to formalization.

The whole system model is kept, not a per-component slice, because a unit is a view into it:
EVM's ``ContractComponentInstance`` reads its siblings, interactions and parent contract through
the application. Restoring the model and re-enumerating its units rebuilds the same units.
"""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from composer.pipeline.ecosystem import ChainTag
from composer.pipeline.run_mode import RunMode
from composer.spec.system_model import BaseApplication, FeatureUnit
from composer.spec.types import ComponentName, PropertyFormulation, SourceIdentifier


class PropertySetComponent(BaseModel):
    """One unit's identity among the rebuilt units, and its final properties."""
    name: ComponentName
    unit_index: int
    properties: list[PropertyFormulation]


class PropertySet(BaseModel):
    schema_version: Literal[1] = 1
    ecosystem: ChainTag
    contract_name: SourceIdentifier
    #: A prioritized set is one focus batch, formalized with its properties protected.
    run_mode: RunMode
    #: The analyzed model (``Ecosystem.system_model``) as JSON.
    system_model: dict[str, Any]
    #: The batches handed to formalization, in order — after the post-inference plugins and, in a
    #: prioritized run, after the focus was chosen.
    property_components: list[PropertySetComponent]

    @staticmethod
    def load(path: Path) -> "PropertySet":
        try:
            return PropertySet.model_validate_json(path.read_text())
        except ValidationError as e:
            raise ValueError(f"invalid property set {path}: {e}") from e

    def analyzed[App: BaseApplication](self, ty: type[App]) -> App:
        return ty.model_validate(self.system_model)

    def match_units[U: FeatureUnit](
        self, units: list[U]
    ) -> list[tuple[U, list[PropertyFormulation]]]:
        """Pair each recorded component with the rebuilt unit of the same name and index.

        Any disagreement is fatal: a component formalized against the wrong unit, or silently
        dropped, would make the run's CVL incomparable with the run that wrote the file."""
        by_name: dict[str, list[U]] = {}
        for u in units:
            by_name.setdefault(u.display_name, []).append(u)
        paired: list[tuple[U, list[PropertyFormulation]]] = []
        for c in self.property_components:
            found = by_name.get(c.name, [])
            if len(found) != 1:
                raise ValueError(
                    f"property set component {c.name!r} matches {len(found)} units "
                    f"(available: {sorted(by_name)})"
                )
            (unit,) = found
            if unit.unit_index != c.unit_index:
                raise ValueError(
                    f"property set component {c.name!r} has unit index {c.unit_index}, "
                    f"but the rebuilt unit has {unit.unit_index}"
                )
            paired.append((unit, c.properties))
        return paired
