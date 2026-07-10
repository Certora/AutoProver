"""The Solana system model — the standalone analog of the EVM ``SourceApplication``.

Where the EVM model is contracts → components with storage variables and external functions,
Solana is **programs → instructions** that operate on **accounts passed in by the caller**
(there is no per-contract owned storage; state lives in accounts the instruction validates
and mutates). The model captures that shape natively — accounts + their signer/owner/PDA
constraints, cross-program invocations (CPIs), and the authorities involved — rather than
reusing the EVM field names.

``SolanaApplication`` is what the shared analysis phase produces (it is a ``BaseApplication``
so ``run_component_analysis`` accepts it). ``SolanaProgramInstance`` / ``SolanaInstructionInstance``
are the driver's ``Main`` / ``Unit``: thin index wrappers over the model, with the instruction
instance satisfying the ecosystem-agnostic ``FeatureUnit`` protocol so the shared driver's
cache keys / task ids / labels work unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import Literal

from pydantic import BaseModel, Field

from composer.spec.system_model import BaseApplication
from composer.spec.types import PropertyFormulation
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


class SolanaProgram(BaseModel):
    """A concrete on-chain program in the system."""

    name: str = Field(
        description="A short conceptual name for the program, used to refer to it across the system."
    )
    program_identifier: str = Field(
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
    """The located target program — the ecosystem's ``Main``.

    Also serves as the **whole-program extraction unit** (satisfies
    ``composer.spec.system_model.FeatureUnit``): under the global extraction strategy
    (docs/crucible-unit-granularity.md) it is the context the property phase reads to
    propose whole-program invariants, before those invariants fan out into
    :class:`SolanaInvariantUnit`\\ s."""

    ind: int
    app: SolanaApplication

    @property
    def program(self) -> SolanaProgram:
        return self.app.programs[self.ind]

    # -- FeatureUnit protocol (whole-program extraction context) ------------------------
    @property
    def display_name(self) -> str:
        return self.program.name

    @property
    def slug(self) -> str:
        return slugify_filename(self.program.name)

    @property
    def unit_index(self) -> int:
        return self.ind

    def cache_material(self) -> str:
        return "|".join([self.app.model_dump_json(), str(self.ind), "program"])

    def context_tag(self) -> dict:
        return {"program": self.program.model_dump()}

    def feature_json(self) -> dict:
        return {
            "program": self.program.name,
            "instructions": [i.model_dump(mode="json") for i in self.program.instructions],
        }


@dataclass
class SolanaInvariantUnit:
    """One whole-program invariant — a ``Unit`` for the global extraction strategy.

    Produced by fanning the invariants out of a single whole-program extraction, so each
    invariant gets its own harness fn + fuzz run + report row (satisfies
    ``composer.spec.system_model.FeatureUnit``). The invariant travels in the formalize
    batch's ``props``; ``feature_json`` carries the whole-program API so the test author
    can drive any ``action_*`` in the sequence it asserts over."""

    ind: int
    _program: SolanaProgramInstance
    invariant: PropertyFormulation

    @property
    def app(self) -> SolanaApplication:
        return self._program.app

    @property
    def program(self) -> SolanaProgram:
        return self._program.program

    # -- FeatureUnit protocol -----------------------------------------------------------
    @property
    def display_name(self) -> str:
        return self.invariant.title

    @property
    def slug(self) -> str:
        return slugify_filename(self.invariant.title)

    @property
    def unit_index(self) -> int:
        return self.ind

    def cache_material(self) -> str:
        return "|".join(
            [self.app.model_dump_json(), str(self._program.ind), str(self.ind), self.invariant.title]
        )

    def context_tag(self) -> dict:
        return {"invariant": self.invariant.model_dump(mode="json"), "program": self.program.name}

    def feature_json(self) -> dict:
        return {
            "program": self.program.name,
            "instructions": [i.model_dump(mode="json") for i in self.program.instructions],
        }


@dataclass
class SolanaInstructionInstance:
    """One instruction of the target program — the ecosystem's ``Unit`` (satisfies
    ``composer.spec.system_model.FeatureUnit``)."""

    ind: int
    _program: SolanaProgramInstance

    @property
    def app(self) -> SolanaApplication:
        return self._program.app

    @property
    def program(self) -> SolanaProgram:
        return self._program.program

    @property
    def instruction(self) -> SolanaInstruction:
        return self.program.instructions[self.ind]

    # -- FeatureUnit protocol -----------------------------------------------------------
    @property
    def display_name(self) -> str:
        return self.instruction.name

    @property
    def slug(self) -> str:
        return slugify_filename(self.instruction.name)

    @property
    def unit_index(self) -> int:
        return self.ind

    def cache_material(self) -> str:
        return "|".join([self.app.model_dump_json(), str(self.ind), str(self._program.ind)])

    def context_tag(self) -> dict:
        return {"instruction": self.instruction.model_dump()}

    def feature_json(self) -> dict:
        # The unit's semantic content for a backend marshalling it across a boundary:
        # the instruction, tagged with its program (a Solana backend needs both).
        return {
            "program": self.program.name,
            "instruction": self.instruction.model_dump(mode="json"),
        }
