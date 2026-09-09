"""What the target's own macros generate, read off its source.

``docs/cvlr-backend-plan.md`` §7.5.5 recorded the failure this answers. A draft named
``crate::__client_accounts_withdraw::WithdrawBumps`` — an Anchor-generated path, spelled by
assembling two different generated things. §5.5 mounts the CVLR crates and the source tools expose
the program's own code, but nothing put the *generated* surface in front of the author, and a name
that does not exist is indistinguishable from one that does until the build says otherwise.

**Read, not expanded.** `cargo expand` would be authoritative and is what the plan first proposed;
it also wants a nightly toolchain and a vendored binary, in a project whose most reliable source of
silent late failures is toolchain acquisition. It is not needed for this, because every name at
issue is a fixed ``format!`` over an identifier that is plainly in the source. From
``anchor-syn`` 0.31.1:

===========================  ==============================================  ==========================================
generated                    rule                                            codegen
===========================  ==============================================  ==========================================
bumps type                   ``{Ident}Bumps``, in the struct's own module     ``codegen/accounts/bumps.rs:10``
client accounts module       ``__client_accounts_{snake}``                    ``codegen/accounts/__client_accounts.rs:14``
CPI client accounts module   ``__cpi_client_accounts_{snake}``                ``codegen/accounts/__cpi_client_accounts.rs:15``
instruction discriminant     ``instruction::{Pascal}::DISCRIMINATOR``         ``codegen/program/dispatch.rs:14``
===========================  ==============================================  ==========================================

The bumps struct is also given a hand-written ``Default`` impl covering every field
(``bumps.rs:74``), which is what makes ``Default::default()`` in a ``Context::new`` call a guarantee
rather than the usual case.

Encoding those four rules here is the cost of not running a second toolchain. They are four lines in
one upstream file, and the Anchor fork is pinned per release, so a version that moved them would
fail the build loudly rather than mislead quietly.

**What it is not.** Not a Rust parser, and not a substitute for the compiler. It locates
declarations well enough to report names and the types beside them; whether the result compiles is
still the gate's answer, and the prompt says so where it renders this.
"""

import dataclasses
import re
from pathlib import Path
from typing import Iterator, Sequence

from composer.spec.cvlr.rust_source import DERIVE_LIST, body_span, code_positions

#: The derive that marks an Anchor accounts context.
ACCOUNTS_DERIVE = "Accounts"

#: The attribute that marks the module holding a program's handlers.
_PROGRAM_ATTR = re.compile(r"#\[program\]")

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_STRUCT = re.compile(rf"\bstruct\s+(?P<name>{_IDENT})")
_MOD = re.compile(rf"\bmod\s+(?P<name>{_IDENT})")
_PUB_FN = re.compile(rf"\bpub\s+fn\s+(?P<name>{_IDENT})")
_FIELD = re.compile(rf"^(?:pub(?:\s*\([^)]*\))?\s+)?(?P<name>{_IDENT})\s*:\s*(?P<type>.+)$", re.S)
_CONTEXT = re.compile(r"\bContext\s*<")

#: Directories whose `.rs` files are not the program's own surface: build output, and the harness
#: and mocks this run writes into the crate.
_SKIP_DIRS = frozenset({"target", "certora"})


def bumps_type(struct: str) -> str:
    """``codegen/accounts/bumps.rs:10`` — emitted in the struct's own module."""
    return f"{struct}Bumps"


def client_accounts_module(struct: str) -> str:
    """``codegen/accounts/__client_accounts.rs:14`` — the *client-side* struct, not the bumps."""
    return f"__client_accounts_{_snake_case(struct)}"


def cpi_client_accounts_module(struct: str) -> str:
    """``codegen/accounts/__cpi_client_accounts.rs:15``."""
    return f"__cpi_client_accounts_{_snake_case(struct)}"


def discriminant_path(handler: str) -> str:
    """``codegen/program/dispatch.rs:14``."""
    return f"instruction::{_pascal_case(handler)}::DISCRIMINATOR"


@dataclasses.dataclass(frozen=True)
class Param:
    """A named value with the type its declaration gives it — a handler's non-account argument."""

    name: str
    rust_type: str


@dataclasses.dataclass(frozen=True)
class AccountsField:
    """One field of an accounts struct, with the type as the source declares it."""

    name: str
    rust_type: str


@dataclasses.dataclass(frozen=True)
class AccountsStruct:
    """A ``#[derive(Accounts)]`` struct and the names Anchor generates from it."""

    name: str
    declared_in: str
    fields: tuple[AccountsField, ...]

    @property
    def bumps_type(self) -> str:
        return bumps_type(self.name)

    @property
    def client_accounts_module(self) -> str:
        return client_accounts_module(self.name)

    @property
    def cpi_client_accounts_module(self) -> str:
        return cpi_client_accounts_module(self.name)


@dataclasses.dataclass(frozen=True)
class Handler:
    """One handler of the ``#[program]`` module, and the accounts struct its ``Context`` names."""

    name: str
    accounts_struct: str
    declared_in: str
    args: tuple[Param, ...]

    @property
    def discriminant_path(self) -> str:
        return discriminant_path(self.name)


@dataclasses.dataclass(frozen=True)
class AnchorSurface:
    """Every accounts struct and handler found under one package."""

    program_module: str | None
    handlers: tuple[Handler, ...]
    structs: tuple[AccountsStruct, ...]

    def struct(self, name: str) -> AccountsStruct | None:
        return next((s for s in self.structs if s.name == name), None)

    def handler(self, name: str) -> Handler | None:
        return next((h for h in self.handlers if h.name == name), None)

    def accounts_for(self, handler: str) -> AccountsStruct | None:
        """The accounts struct a handler's ``Context`` names, resolved to its declaration.

        Both halves have to be present: a handler whose ``Context`` names a struct this package does
        not declare (a composite from a dependency, or a spelling the scanner did not follow) yields
        ``None`` rather than a half-answer, since the caller's whole reason for asking is to stop
        guessing.
        """
        found = self.handler(handler)
        return self.struct(found.accounts_struct) if found is not None else None


def _snake_case(pascal: str) -> str:
    """Anchor's own `to_snake_case`, for the two generated module names that use it."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", pascal).lower()


def _pascal_case(snake: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in snake.split("_") if part)


def _first_code_match(
    pattern: re.Pattern[str], source: str, start: int, code: frozenset[int]
) -> re.Match[str] | None:
    """The first match of ``pattern`` at or after ``start`` that begins on a code character.

    The ``code`` set is the file's, computed once by the caller: it is what keeps the word `struct`
    inside a doc comment or a string literal from being read as a declaration, and recomputing it
    per match would make a scan quadratic in the size of the file.
    """
    for match in pattern.finditer(source, start):
        if match.start() in code:
            return match
    return None


def _top_level_split(text: str) -> Iterator[str]:
    """Split at commas that are not inside brackets of any kind, skipping non-code characters.

    Angle brackets count, because a field's type is where the nesting lives: `Box<Account<'info,
    Vault>>` is one field and contains a comma.
    """
    depth, previous = 0, 0
    positions = list(code_positions(text, 0))
    for i in positions:
        match text[i]:
            case "<" | "(" | "[" | "{":
                depth += 1
            case ">" | ")" | "]" | "}":
                depth -= 1
            case "," if depth == 0:
                yield text[previous:i]
                previous = i + 1
            case _:
                pass
    yield text[previous:]


def _strip_attributes(chunk: str) -> str:
    """Drop the leading ``#[account(..)]`` attributes and doc comments from a field declaration."""
    text = chunk.strip()
    while True:
        if text.startswith("///") or text.startswith("//"):
            _, _, text = text.partition("\n")
            text = text.strip()
            continue
        if not text.startswith("#["):
            return text
        depth, closed = 0, None
        for i in code_positions(text, 1):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    closed = i
                    break
        if closed is None:
            return text
        text = text[closed + 1 :].strip()


def _fields(body: str) -> tuple[AccountsField, ...]:
    found: list[AccountsField] = []
    for chunk in _top_level_split(body):
        if (match := _FIELD.match(_strip_attributes(chunk))) is not None:
            found.append(
                AccountsField(name=match["name"], rust_type=" ".join(match["type"].split()))
            )
    return tuple(found)


def _accounts_structs(source: str, relative: str, code: frozenset[int]) -> Iterator[AccountsStruct]:
    for derive in DERIVE_LIST.finditer(source):
        if derive.start() not in code:
            continue
        if ACCOUNTS_DERIVE not in {item.strip() for item in derive["items"].split(",")}:
            continue
        declaration = _first_code_match(_STRUCT, source, derive.end(), code)
        if declaration is None:
            continue
        span = body_span(source, declaration.start())
        if span is None:
            continue
        opened, closed = span
        yield AccountsStruct(
            name=declaration["name"],
            declared_in=relative,
            fields=_fields(source[opened + 1 : closed - 1]),
        )


def context_accounts_type(signature: str) -> str | None:
    """The accounts type a handler's first parameter names, from the ``Context<..>`` around it.

    Anchor spells the parameter either way — ``Context<Withdraw>`` or the fully-lifetimed
    ``Context<'_, '_, '_, 'info, Withdraw<'info>>`` — and the accounts type is the last type
    argument in both. Taken positionally for that reason rather than by counting lifetimes.
    """
    opener = _CONTEXT.search(signature)
    if opener is None:
        return None
    depth, closed = 0, None
    for i in code_positions(signature, opener.end() - 1):
        if signature[i] == "<":
            depth += 1
        elif signature[i] == ">":
            depth -= 1
            if depth == 0:
                closed = i
                break
    if closed is None:
        return None
    arguments = list(_top_level_split(signature[opener.end() : closed]))
    if not arguments:
        return None
    match = re.match(rf"\s*(?P<name>{_IDENT})", arguments[-1])
    return match["name"] if match is not None else None


def signature_params(signature: str) -> tuple[Param, ...]:
    """A handler's parameters after the ``Context``, from its own declaration.

    The model records these too, as free text; these are the compiler's. A parameter whose pattern
    is not a plain name — a destructured tuple, a `mut` binding — is dropped rather than guessed at,
    which keeps the caller's "either every argument or none" test meaningful.
    """
    opened = signature.find("(")
    if opened < 0:
        return ()
    depth, closed = 0, None
    for i in code_positions(signature, opened):
        if signature[i] == "(":
            depth += 1
        elif signature[i] == ")":
            depth -= 1
            if depth == 0:
                closed = i
                break
    if closed is None:
        return ()
    found: list[Param] = []
    for chunk in list(_top_level_split(signature[opened + 1 : closed]))[1:]:
        if (match := _FIELD.match(_strip_attributes(chunk))) is not None:
            found.append(Param(name=match["name"], rust_type=" ".join(match["type"].split())))
    return tuple(found)


def _handlers(
    source: str, relative: str, code: frozenset[int]
) -> tuple[str | None, tuple[Handler, ...]]:
    attribute = _first_code_match(_PROGRAM_ATTR, source, 0, code)
    if attribute is None:
        return None, ()
    declaration = _first_code_match(_MOD, source, attribute.end(), code)
    if declaration is None:
        return None, ()
    span = body_span(source, declaration.start())
    if span is None:
        return declaration["name"], ()
    opened, closed = span
    body = source[opened + 1 : closed - 1]
    found: list[Handler] = []
    body_code = frozenset(code_positions(body, 0))
    for function in _PUB_FN.finditer(body):
        if function.start() not in body_code:
            continue
        parameters = body_span(body, function.start())
        signature = body[function.end() : parameters[0]] if parameters else body[function.end() :]
        if (accounts := context_accounts_type(signature)) is not None:
            found.append(
                Handler(
                    name=function["name"],
                    accounts_struct=accounts,
                    declared_in=relative,
                    args=signature_params(signature),
                )
            )
    return declaration["name"], tuple(found)


def source_files(package_root: Path) -> Iterator[Path]:
    """Every Rust file of the package that is the program's own — build output and this run's
    harness excluded, since a mock's accounts struct is not the program's surface."""
    for path in sorted(package_root.rglob("*.rs")):
        if _SKIP_DIRS.isdisjoint(path.relative_to(package_root).parts[:-1]):
            yield path


def read_surface(package_root: Path) -> AnchorSurface:
    """Scan a package for its accounts structs and its ``#[program]`` handlers.

    Tolerant by construction: a file that cannot be read is skipped rather than failing the scan,
    because this feeds a prompt. Missing a struct costs the author one source lookup; failing the
    run over an unreadable file costs the run.
    """
    program_module: str | None = None
    handlers: list[Handler] = []
    structs: list[AccountsStruct] = []
    for path in source_files(package_root):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        relative = path.relative_to(package_root).as_posix()
        code = frozenset(code_positions(source, 0))
        structs.extend(_accounts_structs(source, relative, code))
        found_module, found_handlers = _handlers(source, relative, code)
        if found_handlers or found_module is not None:
            program_module = program_module or found_module
            handlers.extend(found_handlers)
    return AnchorSurface(
        program_module=program_module, handlers=tuple(handlers), structs=tuple(structs)
    )
