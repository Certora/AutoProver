"""The Soroban system model — the standalone analog of the EVM ``SourceApplication`` and of
``SolanaApplication``.

Where the EVM model is contracts → components with storage variables and external functions, and
Solana is programs → instructions over **accounts passed in by the caller**, Soroban is
**contracts → functions** over a host-owned **key/value store**. Two axes have no EVM or Solana
peer and are why this is its own model rather than a reuse of either:

* **Authorization is per-``Address``, explicit, and absent by default.** Soroban has no
  ``msg.sender``; the party a function acts for is an ``Address`` *argument*, trustworthy only
  because the function called ``require_auth`` on it. So the checks a function performs are
  modelled as :class:`AuthRequirement`\\ s — the Soroban peer of Solana's per-account
  constraints — and an **empty** ``auth`` list is real signal, not missing data.
* **Storage is durability-tagged.** ``instance`` / ``persistent`` / ``temporary`` are three
  separate key spaces with different expiry semantics (a lapsed ``temporary`` entry is destroyed,
  not archived), so a key without its durability is meaningless. Durability therefore travels with
  the key everywhere it appears — on the declaration (:class:`StorageEntry`) and on each access
  site (:class:`StorageAccessSite`).

Contracts carry :class:`ContractComponent`\\ s — the Soroban analog of EVM's
``ContractComponent`` and Solana's ``ProgramComponent``: named *capabilities*, each a semantic
cluster of the contract's functions plus the storage they maintain. A component **references** its
functions and storage keys by name; the contract's flat ``functions`` / ``storage_entries`` lists
stay authoritative. See ``docs/ecosystem-abstraction.md`` §5.

``SorobanApplication`` is what the shared analysis phase produces (it is a ``BaseApplication`` so
``run_component_analysis`` accepts it). ``SorobanContractInstance`` / ``SorobanComponentInstance``
are the driver's ``Main`` / ``Unit`` — thin index wrappers over the model, mirroring EVM's
``ContractInstance`` / ``ContractComponentInstance`` and Solana's program/component pair, with the
component instance satisfying the ecosystem-agnostic ``FeatureUnit`` protocol so the shared
driver's cache keys / task ids / labels work unchanged.
"""

from dataclasses import dataclass
from functools import cached_property
from typing import Literal

from pydantic import BaseModel, Field

from composer.spec.system_model import BaseApplication
from composer.spec.types import ComponentName, ContractName, RustIdentifier
from composer.spec.util import slugify_filename

#: Which of the host's three key spaces an entry lives in. Not a cosmetic tag: it decides the
#: entry's expiry semantics (a lapsed ``temporary`` entry is deleted and unrecoverable, while
#: ``persistent``/``instance`` are archived and restored), whether it shares the contract
#: instance's TTL, and whether a read of the same key even finds it.
StorageDurability = Literal["instance", "persistent", "temporary"]

#: What a function does to a storage entry. ``extend_ttl`` is called out separately from ``write``
#: because extension is permissionless at the protocol level, so it carries no authorization
#: signal — it must not be read as evidence the function may mutate the entry's value.
StorageOperation = Literal["read", "write", "remove", "extend_ttl"]

#: Which authorization host call a function makes. ``require_auth_for_args`` additionally pins the
#: arguments the signature covers, so the pair (kind, args) is what bounds a signature's scope.
AuthKind = Literal["require_auth", "require_auth_for_args"]


class StorageEntry(BaseModel):
    """One logical storage entry a contract owns.

    The Soroban peer of EVM's ``state_variables`` and Solana's ``account_types``, but a structured
    model rather than a bare name: a key is meaningless without its durability (see
    :data:`StorageDurability`)."""

    key: str = Field(
        description="The key as spelled in source — a `#[contracttype]` DataKey variant with its "
        "parameters (e.g. 'Balance(Address)'), or a Symbol."
    )
    durability: StorageDurability = Field(
        description="Which storage the entry lives in: instance, persistent, or temporary — as "
        "actually used at the call site, even when that looks like a mistake."
    )
    value_type: str = Field(description="The stored type.")
    description: str = Field(description="What the entry means and who may legitimately change it.")


class AuthRequirement(BaseModel):
    """One authorization a function performs.

    The Soroban analog of a Solana :class:`~composer.spec.solana.model.AccountConstraint`: the
    place a function's access control actually lives. An empty ``auth`` list on a
    :class:`SorobanFunction` is the Soroban shape of "no constraints were recorded" — often the
    finding itself, since nothing else authenticates a caller."""

    address: str = Field(
        description="The address authorized — the argument name, or where it is read from "
        "(e.g. \"the admin from DataKey::Admin\")."
    )
    kind: AuthKind = Field(
        description="Which host call is made: require_auth or require_auth_for_args."
    )
    description: str = Field(description="What this authorization is meant to establish.")


class StorageAccessSite(BaseModel):
    """One function's access to one storage entry."""

    key: str = Field(description="The storage key accessed, matching a declared StorageEntry key.")
    durability: StorageDurability = Field(
        description="The storage the access targets. Recorded per access, not just per entry: "
        "reading a key from a durability it was not written to silently yields nothing."
    )
    access: StorageOperation = Field(
        description="What is done to the entry: read, write, remove, or extend_ttl."
    )


class ContractCall(BaseModel):
    """A cross-contract invocation the function makes."""

    target_contract: str = Field(
        description="The contract invoked — its name, or how its address is obtained (an argument, "
        "a storage entry, a hard-coded address)."
    )
    description: str = Field(
        description="What the call does, and whether the caller handles a callee error (a `try_*` "
        "variant) or lets it abort the transaction."
    )


class SorobanFunction(BaseModel):
    """A single ``#[contractimpl]`` entry point of a contract."""

    name: str = Field(description="The function's snake_case name.")
    description: str = Field(description="What the function does, behaviorally (not how).")
    args: list[str] = Field(
        default_factory=list, description="The function's arguments (name & type), excluding `env`."
    )
    returns: str | None = Field(default=None, description="The return type, or null.")
    auth: list[AuthRequirement] = Field(
        default_factory=list,
        description="The authorizations the function performs. Empty means the function "
        "authenticates nobody — record that rather than omitting it.",
    )
    storage: list[StorageAccessSite] = Field(
        default_factory=list, description="The storage entries this function touches."
    )
    calls: list[ContractCall] = Field(
        default_factory=list, description="Cross-contract invocations this function performs."
    )
    events: list[str] = Field(default_factory=list, description="The events it publishes.")
    errors: list[str] = Field(
        default_factory=list,
        description="How it fails: #[contracterror] variants, panic_with_error! sites, and "
        "unhandled aborts (unwrap, overflow, failed host conversions).",
    )
    requirements: list[str] = Field(
        description="Natural-language behavioral requirements — the function's specification."
    )


class InterComponentInteraction(BaseModel):
    """An interaction with another component of a contract this application implements.

    The Soroban peer of :class:`composer.spec.system_model.ComponentInteraction` and of Solana's
    :class:`~composer.spec.solana.model.InterComponentInteraction`; keyed by ``ContractName``."""

    contract: ContractName = Field(
        description="The conceptual name of the contract interacted with (matching the `name` "
        "field of a contract in this application)."
    )
    component: ComponentName | None = Field(
        description="The specific component within that contract interacted with, if identifiable."
    )
    description: str = Field(description="A description of the interaction with that component.")


class AuthorityInteraction(BaseModel):
    """An interaction with an external authority/actor — an admin address, an off-chain signer, or
    a contract the application does not itself implement (a SEP-41 token, a Stellar Asset
    Contract, an oracle).

    The Soroban peer of :class:`composer.spec.system_model.ExternalDependency`."""

    authority: str = Field(
        description="The name of the external authority/actor interacted with (matching the `name` "
        "field of an external authority in this application)."
    )
    description: str = Field(description="A description of the interaction with that actor.")


type ComponentInteraction = InterComponentInteraction | AuthorityInteraction


class ContractComponent(BaseModel):
    """A single major "component" of a contract — a named *capability*: a semantic cluster of the
    contract's functions together with the storage they maintain.

    The Soroban analog of :class:`composer.spec.system_model.ContractComponent` and of Solana's
    ``ProgramComponent``, field for field (``external_entry_points`` → ``functions``,
    ``state_variables`` → ``storage_keys``). Like its peers it **references, it does not own**:
    ``functions`` and ``storage_keys`` are lists of *names* resolving into the owning
    :class:`SorobanContract`, which stays the single source of truth for the rich per-function auth
    / storage / call detail. A function may appear in more than one component when it genuinely
    serves two capabilities. See ``docs/ecosystem-abstraction.md`` §5."""

    name: ComponentName = Field(description="A short, concise name of the component")
    description: str = Field(
        description="A longer description describing *what* this component does, not *how* it does it."
    )
    functions: list[str] = Field(
        description="The names of this contract's functions that make up this component. Each must "
        "match the `name` of a function declared on the contract."
    )
    storage_keys: list[str] = Field(
        description="The storage entries this component maintains. Each must match the `key` of one "
        "of the contract's declared `storage_entries`."
    )
    interactions: list[ComponentInteraction] = Field(
        description="Interactions with other components described in this system, or with external "
        "authorities/actors."
    )
    requirements: list[str] = Field(
        description="Natural-language requirements for this component — its behavioral specification."
    )


class SorobanContract(BaseModel):
    """A concrete on-chain contract in the system."""

    name: ContractName = Field(
        description="A short conceptual name for the contract, used to refer to it across the system."
    )
    contract_identifier: RustIdentifier = Field(
        pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$",
        description="The contract's Rust identifier as it appears in source — the `#[contract]` "
        "type or its crate/module. A valid Rust identifier.",
    )
    contract_id: str | None = Field(
        default=None,
        description="The deployed contract address (StrKey, 'C…'), if the implementation pins one.",
    )
    description: str = Field(description="The contract's role in the system.")
    functions: list[SorobanFunction] = Field(description="The contract's entry points.")
    storage_entries: list[StorageEntry] = Field(
        default_factory=list, description="The storage entries this contract owns."
    )
    components: list[ContractComponent] = Field(
        description="The capabilities making up this contract — semantic clusters of its "
        "functions. Every function must belong to at least one component."
    )

    @cached_property
    def functions_by_name(self) -> dict[str, SorobanFunction]:
        """The contract's functions keyed by name — how a :class:`ContractComponent`'s ``functions``
        name list resolves to the real objects. Shared by the analysis validator and the unit
        wrapper so neither re-derives the lookup."""
        return {f.name: f for f in self.functions}

    @cached_property
    def storage_by_key(self) -> dict[str, StorageEntry]:
        """The contract's storage entries keyed by ``key`` — how a component's ``storage_keys``
        resolve. Keyed by ``key`` alone rather than (key, durability): the *name* is what a
        component references, and a key declared under two durabilities is a finding the validator
        reports rather than a shape this lookup should paper over."""
        return {e.key: e for e in self.storage_entries}


class SorobanAuthority(BaseModel):
    """An external actor: an admin address, an off-chain signer, or a contract the system interacts
    with but does not itself implement (a SEP-41 token, a Stellar Asset Contract, an oracle)."""

    name: str = Field(description="A short unique identifier for this authority/actor.")
    description: str = Field(description="A short technical description.")
    assumptions: list[str] = Field(
        default_factory=list,
        description="Assumptions about this actor's behavior/trust. For a Stellar Asset Contract, "
        "record the issuer's mint / clawback / set_authorized powers here.",
    )


type SorobanComponent = SorobanContract | SorobanAuthority


class SorobanApplication(BaseApplication[SorobanComponent]):
    """A Soroban application: a set of contracts (+ external authorities)."""

    @cached_property
    def contracts(self) -> list[SorobanContract]:
        return [c for c in self.components if isinstance(c, SorobanContract)]

    @cached_property
    def authorities(self) -> list[SorobanAuthority]:
        return [c for c in self.components if isinstance(c, SorobanAuthority)]


# ---------------------------------------------------------------------------
# Index wrappers — the driver's Main (contract) and Unit (component).
# ---------------------------------------------------------------------------


@dataclass
class SorobanContractInstance:
    """The located target contract — the ecosystem's ``Main``, and the peer of EVM's
    :class:`composer.spec.system_model.ContractInstance` and Solana's ``SolanaProgramInstance``.

    Deliberately **not** a ``FeatureUnit``: ``Main`` and ``Unit`` are different axes, and neither
    ecosystem's main is a unit."""

    ind: int
    app: SorobanApplication

    @property
    def contract(self) -> SorobanContract:
        return self.app.contracts[self.ind]


@dataclass
class SorobanComponentInstance:
    """One :class:`ContractComponent` of the target contract — the ecosystem's ``Unit``.

    The Soroban peer of :class:`composer.spec.system_model.ContractComponentInstance`, and like it
    an index pair (contract, component) over the analyzed model rather than a copy of it. The
    component is an authoring and attribution scope, not an execution scope (see
    ``docs/ecosystem-abstraction.md`` §5)."""

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
        """This component's functions, resolved to the real objects. The component holds only
        names; the contract stays authoritative for the auth/storage/call detail, and this is the
        single place the two are joined (a name that doesn't resolve is rejected upstream by
        ``_soroban_validate``, so the lookup cannot fail here)."""
        by_name = self.contract.functions_by_name
        return [by_name[n] for n in self.component.functions if n in by_name]

    @property
    def storage_entries(self) -> list[StorageEntry]:
        """This component's storage entries, resolved from ``storage_keys``. Resolved rather than
        rendered as bare names because durability is what makes a key meaningful, and only the
        contract's declaration carries it."""
        by_key = self.contract.storage_by_key
        return [by_key[k] for k in self.component.storage_keys if k in by_key]

    @property
    def sibling_components(self) -> list[ContractComponent]:
        """The contract's other components — context, not units, mirroring EVM's ``ommer_contracts``."""
        return [c for i, c in enumerate(self.contract.components) if i != self.ind]

    @property
    def sibling_contracts(self) -> list[SorobanContract]:
        """The application's other contracts — context for cross-contract reasoning. Indexed off the
        located contract rather than compared by ``contract_identifier``, exactly as
        :attr:`sibling_components` is."""
        return [c for i, c in enumerate(self.app.contracts) if i != self._contract.ind]

    # -- FeatureUnit protocol -----------------------------------------------------------
    @property
    def display_name(self) -> str:
        return self.component.name

    @property
    def slug(self) -> str:
        """Slug uniqueness within a contract is guaranteed upstream (``_soroban_validate`` rejects
        sibling components that slugify alike), so no disambiguation is needed here."""
        return slugify_filename(self.component.name)

    @property
    def unit_index(self) -> int:
        return self.ind

    def cache_material(self) -> str:
        return "|".join([self.app.model_dump_json(), str(self.ind), str(self._contract.ind)])

    def context_tag(self) -> dict[str, object]:
        return {"component": self.component.model_dump()}

    def feature_json(self) -> dict[str, object]:
        """The component, and only the component — EVM's and Solana's ``feature_json`` are the
        component dump and this mirrors them. Three mechanical additions: ``functions`` and
        ``storage_entries`` are resolved from names to the full objects (the name lists alone drop
        the auth/storage/durability detail a backend needs), and ``slug`` rides along because a
        backend names artifacts after it and must not re-derive it."""
        return {
            **self.component.model_dump(mode="json"),
            "slug": self.slug,
            "functions": [f.model_dump(mode="json") for f in self.functions],
            "storage_entries": [e.model_dump(mode="json") for e in self.storage_entries],
        }
