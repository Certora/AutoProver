"""The Solana system model — the standalone analog of the EVM ``SourceApplication``.

Where the EVM model is contracts → components with storage variables and external functions,
Solana is **programs → instructions** that operate on **accounts passed in by the caller**
(there is no per-contract owned storage; state lives in accounts the instruction validates
and mutates). The model captures that shape natively — accounts + their signer/owner/PDA
constraints, cross-program invocations (CPIs), and the authorities involved — rather than
reusing the EVM field names.

Programs additionally carry :class:`ProgramComponent`\\ s — the Solana analog of EVM's
``ContractComponent``: named *capabilities*, each a semantic cluster of instructions plus the
account state they maintain. A component **references** its instructions by name; the program's
flat ``instructions`` list stays authoritative. See ``docs/ecosystem-abstraction.md`` §4.

``SolanaApplication`` is what the shared analysis phase produces (it is a ``BaseApplication``
so ``run_component_analysis`` accepts it). ``SolanaProgramInstance`` / ``SolanaComponentInstance``
are the driver's ``Main`` / ``Unit`` — thin index wrappers over the model, mirroring EVM's
``ContractInstance`` / ``ContractComponentInstance``, with the component instance satisfying the
ecosystem-agnostic ``FeatureUnit`` protocol so the shared driver's cache keys / task ids / labels
work unchanged.
"""

from dataclasses import dataclass
from functools import cached_property
from typing import Literal

from pydantic import BaseModel, Field

from composer.spec.system_model import BaseApplication
from composer.spec.types import ComponentName, ProgramName, RustIdentifier
from composer.spec.util import slugify_filename

#: How an account is expected to be supplied to an instruction. Drives the "missing signer /
#: owner check" and "account substitution" reasoning in the property prompt.
AccountRole = Literal["signer", "writable", "readonly", "pda", "program", "sysvar"]


class AccountConstraint(BaseModel):
    """One account an instruction expects in its accounts context, plus the constraints the
    program is responsible for enforcing on it."""

    name: str = Field(description="The account's name in the instruction's accounts struct/context.")
    account_type: str = Field(
        description="The account's declared type (e.g. 'Signer', 'Account<Vault>', 'Program', "
        "'SystemAccount', 'UncheckedAccount', a PDA of some seeds)."
    )
    roles: list[AccountRole] = Field(
        default_factory=list,
        description="Roles this account plays: signer / writable / readonly / pda / program / sysvar.",
    )
    constraints: list[str] = Field(
        default_factory=list,
        description="Validations the program must enforce on this account — e.g. Anchor "
        "constraints (has_one, seeds+bump, address, owner), an explicit owner/signer check, or "
        "a documented invariant. Empty means the program performs no checks (often a finding).",
    )


class CpiCall(BaseModel):
    """A cross-program invocation the instruction makes."""

    target_program: str = Field(description="The program invoked (name or program id).")
    description: str = Field(description="What the CPI does and any authority/PDA-signer it uses.")


class SolanaInstruction(BaseModel):
    """A single instruction (entry point) of a program."""

    name: str = Field(description="The instruction's snake_case name (its handler function).")
    description: str = Field(description="What the instruction does, at the behavioral level (not how).")
    accounts: list[AccountConstraint] = Field(
        default_factory=list, description="The accounts the instruction takes and their constraints."
    )
    signers: list[str] = Field(
        default_factory=list,
        description="Which accounts must sign (authorities/owners the instruction authenticates).",
    )
    cpis: list[CpiCall] = Field(
        default_factory=list, description="Cross-program invocations this instruction performs."
    )
    args: list[str] = Field(
        default_factory=list, description="The instruction's non-account arguments (name & type)."
    )
    requirements: list[str] = Field(
        description="Natural-language behavioral requirements — the instruction's specification."
    )


class InterComponentInteraction(BaseModel):
    """An interaction with another component of a program this application implements.

    The Solana peer of :class:`composer.spec.system_model.ComponentInteraction`; keyed by
    ``ProgramName`` rather than ``ContractName``"""

    program: ProgramName = Field(
        description="The conceptual name of the program interacted with (matching the `name` field "
        "of a program in this application)."
    )
    component: ComponentName | None = Field(
        description="The specific component within that program interacted with, if identifiable."
    )
    description: str = Field(description="A description of the interaction with that component.")


class AuthorityInteraction(BaseModel):
    """An interaction with an external authority/actor — an admin keypair, an off-chain signer, or
    a program the application does not itself implement (SPL Token, the System program, an oracle).

    The Solana peer of :class:`composer.spec.system_model.ExternalDependency`."""

    authority: str = Field(
        description="The name of the external authority/actor interacted with (matching the `name` "
        "field of an external authority in this application)."
    )
    description: str = Field(description="A description of the interaction with that actor.")


type ComponentInteraction = InterComponentInteraction | AuthorityInteraction


class ProgramComponent(BaseModel):
    """A single major "component" of a program — a named *capability*: a semantic cluster of the
    program's instructions together with the account state they maintain.

    The Solana analog of :class:`composer.spec.system_model.ContractComponent`, field for field
    (``external_entry_points`` → ``instructions``, ``state_variables`` → ``account_types``). Like
    its EVM peer it **references, it does not own**: ``instructions`` and ``account_types`` are
    lists of *names* resolving into the owning :class:`SolanaProgram`, which stays the single
    source of truth for the rich per-instruction data. An instruction may appear in more than one
    component when it genuinely serves two capabilities. See ``docs/ecosystem-abstraction.md``
    §4."""

    name: ComponentName = Field(description="A short, concise name of the component")
    description: str = Field(
        description="A longer description describing *what* this component does, not *how* it does it."
    )
    instructions: list[str] = Field(
        description="The names of this program's instructions that make up this component. Each "
        "must match the `name` of an instruction declared on the program."
    )
    account_types: list[str] = Field(
        description="The account/state types this component maintains. Each must match one of the "
        "program's declared `account_types`."
    )
    interactions: list[ComponentInteraction] = Field(
        description="Interactions with other components described in this system, or with external "
        "authorities/actors."
    )
    requirements: list[str] = Field(
        description="Natural-language requirements for this component — its behavioral specification."
    )


class SolanaProgram(BaseModel):
    """A concrete on-chain program in the system."""

    name: ProgramName = Field(
        description="A short conceptual name for the program, used to refer to it across the system."
    )
    program_identifier: RustIdentifier = Field(
        pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$",
        description="The program's Rust crate/module identifier as it appears in source. A valid "
        "Rust identifier (snake_case).",
    )
    program_id: str | None = Field(
        default=None, description="The on-chain program id (base58), if declared (e.g. declare_id!)."
    )
    description: str = Field(description="The program's role in the system.")
    instructions: list[SolanaInstruction] = Field(description="The program's instructions.")
    account_types: list[str] = Field(
        default_factory=list,
        description="The account/state types this program owns and derives (PDAs), name & purpose.",
    )
    components: list[ProgramComponent] = Field(
        description="The capabilities making up this program — semantic clusters of its "
        "instructions. Every instruction must belong to at least one component."
    )

    @cached_property
    def instructions_by_name(self) -> dict[str, SolanaInstruction]:
        """The program's instructions keyed by name — how a :class:`ProgramComponent`'s
        ``instructions`` name list resolves to the real objects. Shared by the analysis validator
        and the unit wrapper so neither re-derives the lookup."""
        return {i.name: i for i in self.instructions}


class SolanaAuthority(BaseModel):
    """An external actor: a signer/authority, an off-chain keypair, or another program the
    system interacts with but does not itself implement."""

    name: str = Field(description="A short unique identifier for this authority/actor.")
    description: str = Field(description="A short technical description.")
    assumptions: list[str] = Field(
        default_factory=list, description="Assumptions about this actor's behavior/trust."
    )


type SolanaComponent = SolanaProgram | SolanaAuthority


class SolanaApplication(BaseApplication[SolanaComponent]):
    """A Solana application: a set of programs (+ external authorities)."""

    @cached_property
    def programs(self) -> list[SolanaProgram]:
        return [c for c in self.components if isinstance(c, SolanaProgram)]

    @cached_property
    def authorities(self) -> list[SolanaAuthority]:
        return [c for c in self.components if isinstance(c, SolanaAuthority)]


# ---------------------------------------------------------------------------
# Index wrappers — the driver's Main (program) and Unit (instruction).
# ---------------------------------------------------------------------------


@dataclass
class SolanaProgramInstance:
    """The located target program — the ecosystem's ``Main``, and the peer of EVM's
    :class:`composer.spec.system_model.ContractInstance`.

    Deliberately **not** a ``FeatureUnit``: ``Main`` and ``Unit`` are different axes, and EVM's main
    is not a unit either. It briefly doubled as the whole-program extraction unit; that is what
    :class:`SolanaComponentInstance` replaced (docs/ecosystem-abstraction.md §4)."""

    ind: int
    app: SolanaApplication

    @property
    def program(self) -> SolanaProgram:
        return self.app.programs[self.ind]


@dataclass
class SolanaComponentInstance:
    """One :class:`ProgramComponent` of the target program — the ecosystem's ``Unit``.

    The Solana peer of :class:`composer.spec.system_model.ContractComponentInstance`, and like it an
    index pair (program, component) over the analyzed model rather than a copy of it. The component
    is an authoring and attribution scope, not an execution scope: Crucible still fuzzes the whole
    program in one action sequence, exactly as Foundry's stateful fuzzer calls every function of a
    contract while its authoring stays per component (docs/ecosystem-abstraction.md §4)."""

    ind: int
    _program: SolanaProgramInstance

    @property
    def app(self) -> SolanaApplication:
        return self._program.app

    @property
    def program(self) -> SolanaProgram:
        return self._program.program

    @property
    def component(self) -> ProgramComponent:
        return self.program.components[self.ind]

    @property
    def instructions(self) -> list[SolanaInstruction]:
        """This component's instructions, resolved to the real objects. The component holds only
        names; the program stays authoritative for the account/constraint/CPI detail, and this is
        the single place the two are joined (a name that doesn't resolve is rejected upstream by
        ``_solana_validate``, so the lookup cannot fail here)."""
        by_name = self.program.instructions_by_name
        return [by_name[n] for n in self.component.instructions if n in by_name]

    @property
    def sibling_components(self) -> list[ProgramComponent]:
        """The program's other components — context, not units, mirroring EVM's ``ommer_contracts``."""
        return [c for i, c in enumerate(self.program.components) if i != self.ind]

    @property
    def sibling_programs(self) -> list[SolanaProgram]:
        """The application's other programs — context for cross-program (CPI) reasoning. Indexed off
        the located program rather than compared by ``program_identifier``, exactly as
        :attr:`sibling_components` is."""
        return [p for i, p in enumerate(self.app.programs) if i != self._program.ind]

    # -- FeatureUnit protocol -----------------------------------------------------------
    @property
    def display_name(self) -> str:
        return self.component.name

    @property
    def slug(self) -> str:
        """Slug uniqueness within a program is guaranteed upstream (``_solana_validate`` rejects
        sibling components that slugify alike), so no disambiguation is needed here."""
        return slugify_filename(self.component.name)

    @property
    def unit_index(self) -> int:
        return self.ind

    def cache_material(self) -> str:
        return "|".join([self.app.model_dump_json(), str(self.ind), str(self._program.ind)])

    def context_tag(self) -> dict[str, object]:
        return {"component": self.component.model_dump()}

    def feature_json(self) -> dict[str, object]:
        """The component, and only the component — EVM's ``feature_json`` is
        ``self.component.model_dump(mode="json")`` and this mirrors it (§14 Q3). Two mechanical
        additions: ``instructions`` is resolved from names to the full objects (EVM's
        ``external_entry_points`` are self-contained strings and have nothing to resolve), and
        ``slug`` rides along because a backend names artifacts after it and must not re-derive it.

        The *whole-program* surface deliberately does NOT travel here. A backend that needs it has
        its own route: Crucible's authored fixture — injected into every ``AuthorInput.context`` and
        rendered in the prompt — already exposes every ``action_*`` the fuzzer can drive."""
        return {
            **self.component.model_dump(mode="json"),
            "slug": self.slug,
            "instructions": [i.model_dump(mode="json") for i in self.instructions],
        }
