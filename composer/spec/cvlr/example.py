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
from typing import Sequence

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
class ExampleArg:
    """One non-account argument, split out of the model's ``"name: type"`` text."""

    name: str
    rust_type: str


@dataclasses.dataclass(frozen=True)
class WorkedExample:
    """Everything the prompt's example needs about one handler of the program under verification."""

    program_module: str
    handler: str
    accounts_struct: str
    accounts: tuple[ExampleAccount, ...]
    args: tuple[ExampleArg, ...]


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


def split_arg(declared: str) -> ExampleArg | None:
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
    return ExampleArg(name=name, rust_type=rust_type)


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
    )


def worked_example(
    component: SolanaComponentInstance, properties: Sequence[PropertyFormulation]
) -> WorkedExample | None:
    """The example for the handler this batch is most about.

    Ranked by how many of the batch's properties name the handler, since the example's whole job is
    to be close to the first rule the author writes; ties and total misses fall back to the
    component's declared order, which is still a real handler of this component. The first
    *renderable* candidate wins — an unrenderable one is skipped rather than failing the whole
    substitution, and ``None`` here simply leaves the prompt's generic example in place.
    """
    module = component.program.program_identifier
    ranked = sorted(
        enumerate(component.instructions),
        key=lambda pair: (-sum(_mentions(p, pair[1].name) for p in properties), pair[0]),
    )
    return next(
        (example for _, ins in ranked if (example := _renderable(ins, module)) is not None), None
    )
