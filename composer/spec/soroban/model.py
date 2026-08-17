"""Soroban system model."""

from dataclasses import dataclass
from functools import cached_property
from typing import Literal

from pydantic import BaseModel, Field

from composer.spec.system_model import BaseApplication
from composer.spec.types import ComponentName, ContractName, RustIdentifier
from composer.spec.util import slugify_filename

StorageDurability = Literal["instance", "persistent", "temporary"]

StorageOperation = Literal["read", "write", "remove", "extend_ttl"]

AuthKind = Literal["require_auth", "require_auth_for_args"]


class StorageEntry(BaseModel):
    key: str = Field(
        description="Source key, such as a DataKey variant (`Balance(Address)`) or a Symbol."
    )
    durability: StorageDurability = Field(
        description="Storage kind used at the call site: instance, persistent, or temporary."
    )
    value_type: str = Field(description="The stored type.")
    description: str = Field(description="What this entry stores and who may change it.")


class AuthRequirement(BaseModel):
    address: str = Field(
        description="Address being authorized, as an argument name or stored address."
    )
    kind: AuthKind = Field(
        description="Auth call used: require_auth or require_auth_for_args."
    )
    description: str = Field(description="What this auth check protects.")


class StorageAccessSite(BaseModel):
    key: str = Field(description="Storage key accessed.")
    durability: StorageDurability = Field(
        description="Storage kind used for this access."
    )
    access: StorageOperation = Field(
        description="Operation: read, write, remove, or extend_ttl."
    )


class ContractCall(BaseModel):
    target_contract: str = Field(
        description="Called contract, or how its address is found."
    )
    description: str = Field(
        description="What is called and how errors are handled."
    )


class SorobanFunction(BaseModel):
    name: str = Field(description="The function's snake_case name.")
    description: str = Field(description="What the function does.")
    args: list[str] = Field(
        default_factory=list, description="Arguments, excluding `env`."
    )
    returns: str | None = Field(default=None, description="The return type, or null.")
    auth: list[AuthRequirement] = Field(
        default_factory=list,
        description="Auth checks performed by the function. Empty means no auth.",
    )
    storage: list[StorageAccessSite] = Field(
        default_factory=list, description="Storage accessed by this function."
    )
    calls: list[ContractCall] = Field(
        default_factory=list, description="Cross-contract calls made by this function."
    )
    events: list[str] = Field(default_factory=list, description="Events emitted.")
    errors: list[str] = Field(
        default_factory=list,
        description="Failure cases, including contract errors, panics, unwraps, overflow, or conversion failures.",
    )
    requirements: list[str] = Field(
        description="Required function behavior."
    )

    def to_signature(self) -> str:
        """This function rendered as a Rust-like signature, e.g.
        ``transfer(from: Address, to: Address, amount: i128) -> Result<(), Error>``.

        Note: ``env`` is already excluded from ``args`` by the analysis prompt,
        so this looks like what the caller sees."""
        returns = f" -> {self.returns}" if self.returns else ""
        return f"{self.name}({', '.join(self.args)}){returns}"


class InterComponentInteraction(BaseModel):
    contract: ContractName = Field(
        description="Name of the contract used."
    )
    # ``_soroban_validate`` rejects a name that does not resolve.
    component: ComponentName = Field(
        description="The component of that contract this interaction is with. Must match the "
        "`name` of a component declared on it."
    )
    description: str = Field(description="How this component is used.")


class AuthorityInteraction(BaseModel):
    authority: str = Field(
        description="Name of the external actor used."
    )
    description: str = Field(description="How this actor is used.")


type ComponentInteraction = InterComponentInteraction | AuthorityInteraction


class ContractComponent(BaseModel):
    name: ComponentName = Field(description="Short component name.")
    description: str = Field(
        description="What this component does."
    )
    functions: list[str] = Field(
        description="Function names in this component."
    )
    storage_keys: list[str] = Field(
        description="Storage keys maintained by this component."
    )
    interactions: list[ComponentInteraction] = Field(
        description="Links to other components or external actors."
    )
    requirements: list[str] = Field(
        description="Required component behavior."
    )


class SorobanContract(BaseModel):
    name: ContractName = Field(
        description="Short contract name."
    )
    contract_identifier: RustIdentifier = Field(
        pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$",
        description="Rust identifier for the contract type, crate, or module.",
    )
    contract_id: str | None = Field(
        default=None,
        description="Pinned deployed contract address, if any.",
    )
    description: str = Field(description="The contract's role in the system.")
    functions: list[SorobanFunction] = Field(description="The contract's entry points.")
    storage_entries: list[StorageEntry] = Field(
        default_factory=list, description="The storage entries this contract owns."
    )
    components: list[ContractComponent] = Field(
        description="Feature groups in this contract. Every function must appear in at least one."
    )

    @cached_property
    def functions_by_name(self) -> dict[str, SorobanFunction]:
        return {f.name: f for f in self.functions}

    @cached_property
    def storage_by_key(self) -> dict[str, StorageEntry]:
        return {e.key: e for e in self.storage_entries}


class SorobanAuthority(BaseModel):
    name: str = Field(description="Short external actor name.")
    description: str = Field(description="What this actor does.")
    assumptions: list[str] = Field(
        default_factory=list,
        description="Trust assumptions. For SACs, include issuer mint, clawback, and set_authorized powers.",
    )


type SorobanComponent = SorobanContract | SorobanAuthority


class SorobanApplication(BaseApplication[SorobanComponent]):
    @cached_property
    def contracts(self) -> list[SorobanContract]:
        return [c for c in self.components if isinstance(c, SorobanContract)]

    @cached_property
    def authorities(self) -> list[SorobanAuthority]:
        return [c for c in self.components if isinstance(c, SorobanAuthority)]


@dataclass
class SorobanContractInstance:
    ind: int
    app: SorobanApplication

    @property
    def contract(self) -> SorobanContract:
        return self.app.contracts[self.ind]


@dataclass
class SorobanComponentInstance:
    ind: int
    _contract: SorobanContractInstance

    @property
    def app(self) -> SorobanApplication:
        return self._contract.app

    @property
    def contract(self) -> SorobanContract:
        return self._contract.contract

    @property
    def component(self) -> ContractComponent:
        return self.contract.components[self.ind]

    @property
    def functions(self) -> list[SorobanFunction]:
        by_name = self.contract.functions_by_name
        return [by_name[n] for n in self.component.functions if n in by_name]

    @property
    def storage_entries(self) -> list[StorageEntry]:
        by_key = self.contract.storage_by_key
        return [by_key[k] for k in self.component.storage_keys if k in by_key]

    @property
    def sibling_components(self) -> list[ContractComponent]:
        return [c for i, c in enumerate(self.contract.components) if i != self.ind]

    @property
    def sibling_contracts(self) -> list[SorobanContract]:
        return [c for i, c in enumerate(self.app.contracts) if i != self._contract.ind]

    @property
    def display_name(self) -> str:
        return self.component.name

    @property
    def slug(self) -> str:
        return slugify_filename(self.component.name)

    @property
    def unit_index(self) -> int:
        return self.ind

    def cache_material(self) -> str:
        return "|".join([self.app.model_dump_json(), str(self.ind), str(self._contract.ind)])

    def context_tag(self) -> dict[str, object]:
        return {"component": self.component.model_dump()}

    def feature_json(self) -> dict[str, object]:
        return {
            **self.component.model_dump(mode="json"),
            "slug": self.slug,
            "functions": [f.model_dump(mode="json") for f in self.functions],
            "storage_entries": [e.model_dump(mode="json") for e in self.storage_entries],
        }
