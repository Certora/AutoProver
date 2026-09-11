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
# ``CheckName``: the backend's name for one check — a CVL rule, a foundry
# test, a fuzz harness function. ``FormalResult.property_checks()`` maps each
# property title onto the checks that verify it.
# ``ComponentName``: human name of an AIComposer component (e.g. "Increment").
# ``PropertyTitle``: a property's unique snake_case title — the key in a
# component's ``property_rules`` mapping.
#
# ``SolidityIdentifier`` and ``RustIdentifier`` are **siblings** under
# ``SourceIdentifier``; every other name is a sibling of the rest. Passing one
# where a sibling is expected is a type error, even though all are ``str`` at
# runtime.
if TYPE_CHECKING:
    class SourceIdentifier(str): ...
    class SolidityIdentifier(SourceIdentifier): ...
    class RustIdentifier(SourceIdentifier): ...
    class ContractName(str): ...
    class ProgramName(str): ...
    class CheckName(str): ...
    class ComponentName(str): ...
    class PropertyTitle(str): ...
else:
    SourceIdentifier = str
    SolidityIdentifier = str
    RustIdentifier = str
    ContractName = str
    ProgramName = str
    CheckName = str
    ComponentName = str
    PropertyTitle = str

#: A ``CheckName`` as the CVL/prover side speaks it: a rule/invariant identifier as it appears in
#: the prover report and in a component's ``property_rules`` mapping. The same type, not a
#: sibling — a rule name is what the Rust seam calls a check name.
RuleName = CheckName

class ArtifactIdentifier(Protocol):
    @property
    def stem(self) -> str: ...

    @property
    def artifact_file(self) -> str: ...

class FormalResult(Protocol):
    def property_checks(self) -> list[tuple[PropertyTitle, list[CheckName]]]: ...

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

type PropertyKey = tuple[ComponentName, PropertyTitle]
"""A property's identity across a run: ``(component, title)``. Titles are unique within a
component, so the pair is unique run-wide — it is what the report's groups cross-reference and
what the prioritizer names when it points at one of many components' candidates."""


class PropertyFormulation(UntitledPropertyFormulation):
    """
    A property or invariant that must hold for the component
    """
    title: PropertyTitle = Field(description="A short, descriptive snake_case identifier for the property (e.g. 'total_supply_preserved'). Must be unique within the batch of properties.")


class VerificationArtifact(BaseModel):
    """A verification-supporting file produced by a plugin's contributed tool — a
    Lean proof discharging instrumented lemmas, an auxiliary certificate, etc.
    Registered with its *content*, not a path: the artifact store owns the
    deliverable layout and decides where it lands on disk."""
    #: File basename (the store sanitizes to a basename and namespaces by
    #: unit and plugin, so collisions across tools are impossible).
    name: str
    #: Open vocabulary tag, e.g. "lean-proof".
    kind: str
    #: One-or-two-line blurb for the report deliverable.
    description: str
    content: str

