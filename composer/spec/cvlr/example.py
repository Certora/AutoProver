"""The authoring prompt's worked example, rendered against the program under verification.

The example in the author's system prompt teaches the mechanics of reaching an Anchor handler:
nondet accounts, the accounts struct, ``Context::new``, and observing post-state through the struct
the context borrowed rather than by deserializing again. Those mechanics are the same everywhere;
the *names* are not, and until now the example carried a stand-in program with the caveat that its
names were the example's. Every unit then re-derived the translation against its own program, which
``docs/cvlr-backend-plan.md`` §7.11 measured: one unit spent six prover submissions on an ampersand,
another thirteen reaching the program at all.

The analyzed model already holds every name the example needs — the program's Rust identifier, the
handler, the accounts and their declared types, the non-account arguments — so this substitutes
them. It is deterministic: no agent, no submission, and nothing here is on the run's critical path.

What it deliberately does *not* claim is that the result compiles. The accounts struct's name is not
in the model and is reconstructed from Anchor's convention, and the declared types are the
analysis's reading of the source. The prompt says so where it renders this; a worked example whose
names are right for this program and whose struct name needs confirming is still a long way better
than one about a different program entirely.
"""

import dataclasses
import re
from typing import Literal, Sequence

from composer.spec.cvlr import anchor_surface
from composer.spec.cvlr.anchor_surface import AnchorSurface, Param
from composer.spec.solana.model import AccountConstraint, SolanaComponentInstance, SolanaInstruction
from composer.spec.types import PropertyFormulation

#: A Rust identifier, for the names that end up on the left of a `let` or a struct field.
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: A type *name* — deliberately requiring the leading capital. The analysis is asked for a declared
#: type but is free to answer with prose ("a PDA of some seeds"), and a lowercase word like "pda"
#: would otherwise pass as an identifier and be rendered as a constructor that does not exist.
_TYPE_NAME = re.compile(r"[A-Z][A-Za-z0-9_]*")

#: A type *expression*, permissive enough for the generics and lifetimes a declared type carries.
_TYPE_EXPR = re.compile(r"[A-Za-z_][A-Za-z0-9_:<>, '\[\];]*")


@dataclasses.dataclass(frozen=True)
class ExampleAccount:
    """One field of the example's accounts struct.

    ``constructor`` is the Anchor wrapper to call ``try_from`` on — ``Account``, ``Signer``,
    ``Program``, ``AccountLoader``. It is optional because the declared type is free text: when the
    analysis recorded prose rather than a type, the prompt shows the prose instead of inventing a
    wrapper the author would have to un-learn.
    """

    name: str
    declared: str
    constructor: str | None


@dataclasses.dataclass(frozen=True)
class WorkedExample:
    """Everything the prompt's example needs about one handler of the program under verification.

    ``provenance`` is what the prompt's caveat turns on, and it is the difference between the two
    readings of the same program. ``"source"`` means the names were read from the declarations
    themselves, so the struct name and the field types are facts. ``"model"`` means they came from
    the analysis, where the struct name is Anchor's convention applied to the handler and the types
    are the analysis's reading — usable, and worth saying so.
    """

    program_module: str
    handler: str
    accounts_struct: str
    accounts: tuple[ExampleAccount, ...]
    args: tuple[Param, ...]
    provenance: Literal["source", "model"]

    @property
    def bumps_type(self) -> str:
        return anchor_surface.bumps_type(self.accounts_struct)

    @property
    def client_accounts_module(self) -> str:
        return anchor_surface.client_accounts_module(self.accounts_struct)

    @property
    def cpi_client_accounts_module(self) -> str:
        return anchor_surface.cpi_client_accounts_module(self.accounts_struct)

    @property
    def discriminant_path(self) -> str:
        return anchor_surface.discriminant_path(self.handler)


def _pascal_case(snake: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in snake.split("_") if part)


def _peel(text: str, wrapper: str) -> str | None:
    """``Box<Account<T>>`` -> ``Account<T>``. Anchor boxes large accounts routinely, and the outer
    type is not the one that constructs."""
    opened = f"{wrapper}<"
    if text.startswith(opened) and text.endswith(">"):
        return text[len(opened) : -1].strip()
    return None


def account_constructor(declared: str) -> str | None:
    """The wrapper type to call ``try_from`` on, read off the analysis's declared type."""
    text = declared.strip()
    if (inner := _peel(text, "Box")) is not None:
        text = inner
    head = text.split("<", 1)[0].strip()
    return head if _TYPE_NAME.fullmatch(head) else None


def split_arg(declared: str) -> Param | None:
    """Split the model's ``"amount: u64"`` into its halves.

    The field is documented as "name & type" but is free text, so anything that does not split
    cleanly into an identifier and a type expression yields ``None`` — which takes its instruction
    out of the running rather than producing a `let` binding that names nothing.
    """
    name, sep, rust_type = declared.partition(":")
    if not sep:
        return None
    name, rust_type = name.strip().strip("`"), rust_type.strip().strip("`")
    if not _IDENT.fullmatch(name) or not _TYPE_EXPR.fullmatch(rust_type):
        return None
    return Param(name=name, rust_type=rust_type)


def _account_field(account: AccountConstraint) -> ExampleAccount | None:
    name = account.name.strip().strip("`")
    if not _IDENT.fullmatch(name):
        return None
    return ExampleAccount(
        name=name,
        # Rendered inside a `/* … */` when it is not a type, and the analysis writes this field
        # freely enough that it could carry the sequence that closes one.
        declared=account.account_type.strip().replace("*/", "* /"),
        constructor=account_constructor(account.account_type),
    )


def _mentions(prop: PropertyFormulation, handler: str) -> bool:
    text = f"{prop.title} {prop.description}".casefold()
    return handler in text or handler.replace("_", " ") in text


def _renderable(instruction: SolanaInstruction, program_module: str) -> WorkedExample | None:
    """The example for one handler, or ``None`` if the model's record of it cannot be written as
    Rust — an account whose recorded name is prose, an argument that does not split.

    Every account must be nameable because the example constructs the whole struct; an individual
    account whose *type* is prose is fine and renders as the note it is.
    """
    if not instruction.accounts or not _IDENT.fullmatch(instruction.name):
        return None
    accounts = [_account_field(a) for a in instruction.accounts]
    args = [split_arg(a) for a in instruction.args]
    if any(a is None for a in accounts) or any(a is None for a in args):
        return None
    return WorkedExample(
        program_module=program_module,
        handler=instruction.name,
        accounts_struct=_pascal_case(instruction.name),
        accounts=tuple(a for a in accounts if a is not None),
        args=tuple(a for a in args if a is not None),
        provenance="model",
    )


def _from_surface(handler: str, surface: AnchorSurface, fallback_module: str):
    """The example for one handler read from the program's own declarations.

    Preferred over the model whenever it resolves, because everything the model half has to hedge is
    settled here: the accounts struct is the one the handler's ``Context`` actually names rather than
    Anchor's convention applied to the handler, and the field and argument types are the compiler's
    rather than the analysis's reading of them.
    """
    found = surface.handler(handler)
    struct = surface.accounts_for(handler)
    if found is None or struct is None or not struct.fields:
        return None
    return WorkedExample(
        program_module=surface.program_module or fallback_module,
        handler=found.name,
        accounts_struct=struct.name,
        accounts=tuple(
            ExampleAccount(
                name=field.name,
                declared=field.rust_type,
                constructor=account_constructor(field.rust_type),
            )
            for field in struct.fields
        ),
        args=found.args,
        provenance="source",
    )


def worked_example(
    component: SolanaComponentInstance,
    properties: Sequence[PropertyFormulation],
    surface: AnchorSurface | None = None,
) -> WorkedExample | None:
    """The example for the handler this batch is most about.

    Ranked by how many of the batch's properties name the handler, since the example's whole job is
    to be close to the first rule the author writes; ties and total misses fall back to the
    component's declared order, which is still a real handler of this component.

    Each candidate is tried against the program's own declarations first and the analyzed model
    second, and the first that yields anything wins — so a handler the scanner could not follow
    falls back to the model for *that handler* rather than dropping to the next one, and only a
    candidate neither reading can render is skipped. ``None`` leaves the prompt's stand-in example
    in place.
    """
    module = component.program.program_identifier
    ranked = sorted(
        enumerate(component.instructions),
        key=lambda pair: (-sum(_mentions(p, pair[1].name) for p in properties), pair[0]),
    )
    for _, instruction in ranked:
        from_source = (
            _from_surface(instruction.name, surface, module) if surface is not None else None
        )
        if (example := from_source or _renderable(instruction, module)) is not None:
            return example
    return None
