from typing import TYPE_CHECKING, Protocol, Literal

# Nominal ``str`` subtypes for the two distinct contract-identity fields.
# Both phantom-typed (TYPE_CHECKING-only subclass; ``str`` at runtime) so they
# remain distinct at static-check time but pydantic ``Field`` validates them
# as plain strings.
#
# ``SolidityIdentifier``: a Solidity contract identifier (regex-validated where
# stored on a pydantic field).
# ``ContractName``: the conceptual / design-doc-readable name of a contract.
# May be a Solidity identifier when the design doc names contracts that way,
# but allowed to be anything human-readable.
#
# The two are **siblings under str**, not in a subtype relation with each
# other — passing a ``SolidityIdentifier`` where ``ContractName`` is expected
# (or vice-versa) is a type error, even though both are ``str`` at runtime.
if TYPE_CHECKING:
    class SolidityIdentifier(str): ...
    class ContractName(str): ...
else:
    SolidityIdentifier = str
    ContractName = str

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

from pydantic import BaseModel, Field

type PropertyType = Literal["attack_vector", "safety_property", "invariant"]
"""The kind of a property: an attack vector, a safety property, or a state
invariant. Shared so every layer (inference, report, grouping) addresses the
same vocabulary instead of redeclaring the literal."""

class PropertyFormulation(BaseModel):
    """
    A property or invariant that must hold for the component
    """
    title: str = Field(description="A short, descriptive snake_case identifier for the property (e.g. 'total_supply_preserved'). Must be unique within the batch of properties.")
    sort: PropertyType = Field(description="The type of property you are describing.")
    description: str = Field(description="The description of the property")
