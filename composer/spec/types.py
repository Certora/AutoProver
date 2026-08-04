from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Literal

# Nominal ``str`` subtypes for the distinct identity fields of an analyzed
# system. All phantom-typed (TYPE_CHECKING-only subclasses; ``str`` at runtime)
# so they remain distinct at static-check time but pydantic ``Field`` validates
# them as plain strings.
#
# ``SourceIdentifier``: the identifier an entity is defined under in source —
# EVM's contract identifier, Solana's program identifier. What the
# ecosystem-agnostic seam speaks.
# ``SolidityIdentifier`` / ``RustIdentifier``: a ``SourceIdentifier`` in a
# specific source language (regex-validated where stored on a pydantic field).
# ``ContractName`` / ``ProgramName``: the conceptual / design-doc-readable name
# of an EVM contract or a Solana program. May coincide with the source
# identifier when the design doc names the entity that way, but allowed to be
# anything human-readable.
#
# ``SolidityIdentifier`` and ``RustIdentifier`` are **siblings** under
# ``SourceIdentifier``; the conceptual names are siblings of each other and of
# ``SourceIdentifier``. Passing one where a sibling is expected is a type error,
# even though all are ``str`` at runtime.
if TYPE_CHECKING:
    class SourceIdentifier(str): ...
    class SolidityIdentifier(SourceIdentifier): ...
    class RustIdentifier(SourceIdentifier): ...
    class ContractName(str): ...
    class ProgramName(str): ...
else:
    SourceIdentifier = str
    SolidityIdentifier = str
    RustIdentifier = str
    ContractName = str
    ProgramName = str

type UnitName = str

type RuleName = UnitName
"""A CVL rule/invariant identifier as it appears in the prover report and in a component's
``property_rules`` mapping."""

type ComponentName = str
"""Human name of an AIComposer component (e.g. "Increment"), or "Structural Invariants"."""

type PropertyTitle = str
"""A property's unique snake_case title — the key in a component's ``property_rules`` mapping."""

class ArtifactIdentifier(Protocol):
    @property
    def stem(self) -> str: ...

    @property
    def artifact_file(self) -> str: ...

class FormalResult(Protocol):
    def property_units(self) -> list[tuple[PropertyTitle, list[UnitName]]]: ...

    @property
    def commentary(self) -> str: ...

    @property
    def artifact_text(self) -> str: ...


@dataclass(frozen=True)
class Curtailed[T]:
    """A formalization the run budget cut short. ``partial`` is whatever the author published
    after the budget monitor lifted its validation gates — the raw backend result at the author
    boundary, the persisted result inside a pipeline outcome — or ``None`` when the run stopped
    (or the author gave up under the wrap-up order) before publishing anything. Either way the
    component's encoding and verification state are unreliable: it is not a delivery, is never
    cached, and the report keeps it out of the property grouping, surfacing it in the budget
    appendix instead. ``detail`` optionally carries context (the hard-stop message, or the
    author's own account)."""
    partial: T | None
    detail: str | None = None

from pydantic import BaseModel, Field

type PropertyType = Literal["attack_vector", "safety_property", "invariant"]
"""The kind of a property: an attack vector, a safety property, or a state
invariant. Shared so every layer (inference, report, grouping) addresses the
same vocabulary instead of redeclaring the literal."""

class UntitledPropertyFormulation(BaseModel):
    sort: PropertyType = Field(description="The type of property you are describing.")
    description: str = Field(description="The description of the property")

    @property
    def sort_description(self) -> str:
        match self.sort:
            case "attack_vector":
                return "Attack Vector"
            case "invariant":
                return "Invariant"
            case "safety_property":
                return "Safety Property"

class PropertyFormulation(UntitledPropertyFormulation):
    """
    A property or invariant that must hold for the component
    """
    title: str = Field(description="A short, descriptive snake_case identifier for the property (e.g. 'total_supply_preserved'). Must be unique within the batch of properties.")
    

