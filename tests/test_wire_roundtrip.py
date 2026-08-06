"""Property-based round-trip of the Rust/Python wire protocol, in both directions.

The two sides of the seam are hand-written mirrors — pydantic models in ``composer.rustapp.wire``
and ``…descriptor``, serde types in ``rust/autoprover-sdk/src`` — and the docstrings on both ask
that they be kept in lockstep. This is what checks that they were, per field, rather than per
convention.

Each direction round-trips a payload through the *other* language and back, and asserts nothing was
lost. What a dropped or renamed field looks like:

* **Outbound** (host → wheel): the host builds a model, dumps it, Rust deserializes into the
  mirrored type and re-serializes. A field Rust doesn't declare is dropped on the way through, so
  re-parsing the answer no longer equals what was sent.
* **Inbound** (wheel → host): Rust builds a value from entropy Hypothesis drew, serializes it, the
  host parses and re-dumps, and Rust serializes the result again. The two Rust-produced documents
  are compared, so a field the *host* drops is what shows up as a difference.

The inbound generator lives in Rust (``autoprover_sdk::fuzz``, behind the ``fuzz`` feature) rather
than being derived from the pydantic schema, and that is the point: a field only the Rust side
declares gets populated. A generator built from the host's own schema can't produce what the host
has never heard of, so it can only ever find drift in one direction.

A round trip is blind to one thing on a *tolerant* seam: a field only one side declares, which the
other quietly defaults. Both halves ship together, so this seam is not tolerant — nothing on it
defaults anything, and such a field fails at the callout carrying it (see
:mod:`composer.rustapp.wire`). That is what makes the two round trips below sufficient on their own.

The two field-*set* checks that follow therefore assert nothing the round trips miss; they are kept
because they are deterministic and per-type, so a one-sided field is reported as the field that
diverged rather than as a serde error inside a shrunk example.

What none of that catches is a payload the harness never hears about: the roots have to be listed,
because a wire name and a direction are not properties of a class. So two checks close that gap by
*discovering* what the ABI defines and requiring the lists to account for it —
``test_every_mirror_is_reachable_from_a_declared_root`` and
``test_every_outbound_mirror_has_its_own_field_set_check``. Add a payload to
:mod:`composer.rustapp.wire` and forget to list it here, and those fail rather than the new payload
being silently exercised by nothing.

Everything talks to the ``wire-echo`` binary over one long-lived pipe (see its module docs). The
round trips are marked ``fuzz``; the field-set and completeness checks are deterministic and always
run.
"""

import enum
import functools
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import types
import typing
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import annotated_types
import pytest
from hypothesis import assume, given, settings, strategies as st
from pydantic import BaseModel, TypeAdapter, ValidationError

#: Imported as modules as well as by name: the completeness checks discover what the ABI defines
#: rather than trusting the import list below to be current.
import composer.rustapp.descriptor as descriptor_abi
import composer.rustapp.wire as wire_abi
from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.wire import (
    AppArgs,
    AuthorInput,
    CompileResult,
    ComponentOutcome,
    Failure,
    FinalizeComponent,
    FinalizeInput,
    Prompt,
    Property,
    SandboxGrants,
    Target,
    Unit,
    ValidateOutcome,
    WireModel,
    WorkspacePrep,
)
from composer.sandbox.config import BackendSpec

REPO_ROOT = pathlib.Path(__file__).parent.parent
RUST_DIR = REPO_ROOT / "rust"
WIRE_ECHO = RUST_DIR / "target" / "debug" / "wire-echo"


# ---------------------------------------------------------------------------
# The pipe.
# ---------------------------------------------------------------------------

class WireFault(AssertionError):
    """The Rust side refused a payload — a serde error naming the field that diverged."""


class WireEcho:
    """A live ``wire-echo`` process. One per session: an example is a single request/response, and
    spawning a process for each would dominate the runtime."""

    def __init__(self, binary: pathlib.Path) -> None:
        self._proc = subprocess.Popen(
            [str(binary)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )

    def close(self) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.close()
        self._proc.wait(timeout=10)

    def _request(self, request: dict[str, Any]) -> dict[str, Any]:
        assert self._proc.stdin is not None and self._proc.stdout is not None
        # ensure_ascii keeps the request line pure ASCII, so drawn text can't depend on the pipe's
        # encoding; the protocol is one line per message in both directions.
        self._proc.stdin.write(json.dumps(request, ensure_ascii=True) + "\n")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        if not line:
            stderr = self._proc.stderr.read() if self._proc.stderr else ""
            raise WireFault(f"wire-echo exited (code {self._proc.returncode}): {stderr}")
        return json.loads(line)

    def echo(self, ty: str, payload: object) -> object:
        """``payload`` deserialized into the Rust ``ty`` and serialized back."""
        answer = self._request({"op": "echo", "ty": ty, "payload": payload})
        match answer:
            case {"status": "ok", "payload": echoed}:
                return echoed
            case _:
                raise WireFault(f"echo {ty}: {answer} for {json.dumps(payload)}")

    def gen(self, ty: str, entropy: bytes) -> object | None:
        """A Rust-built ``ty``, serialized — or ``None`` when the draw ran out of entropy, which is
        this harness's own limit rather than anything about the protocol."""
        answer = self._request({"op": "gen", "ty": ty, "entropy": list(entropy)})
        match answer:
            case {"status": "ok", "payload": generated}:
                return generated
            case {"status": "exhausted"}:
                return None
            case _:
                raise WireFault(f"gen {ty}: {answer}")


@pytest.fixture(scope="session")
def wire_echo():
    if shutil.which("cargo") is None:
        pytest.skip("no cargo — the Rust half of the protocol can't be built")
    subprocess.run(
        ["cargo", "build", "--package", "autoprover-sdk", "--features", "fuzz",
         "--bin", "wire-echo"],
        cwd=RUST_DIR, check=True, timeout=900,
        # The SDK pulls in pyo3, whose build script probes the *first* `python3` on PATH and fails
        # on one newer than it supports. Pin it to the interpreter running these tests, which is by
        # definition a version this checkout supports.
        env={**os.environ, "PYO3_PYTHON": sys.executable},
    )
    echo = WireEcho(WIRE_ECHO)
    yield echo
    echo.close()


# ---------------------------------------------------------------------------
# Outbound strategies — what the host sends.
#
# Built by walking each model's ``model_fields`` rather than naming the fields here, because a
# strategy that names them stops covering the one somebody adds next: an undrawn field keeps its
# default, survives the trip, and the round trip passes without ever having tested it.
# ---------------------------------------------------------------------------

_MAX_ITEMS = 3
_TEXT = st.text(max_size=24)
#: Bounded to `i64`: past that `serde_json` reads a number as a float, and the comparison would be
#: measuring float formatting rather than the protocol.
_INT = st.integers(min_value=-(2**63), max_value=2**63 - 1)
#: Finite only — NaN and infinity have no JSON spelling, so neither side can carry one.
_FLOAT = st.floats(allow_nan=False, allow_infinity=False)

#: An opaque payload (`dict[str, Any]`, `serde_json::Value`): any JSON document, kept shallow since
#: both sides treat it as opaque and depth buys no coverage.
_JSON = st.recursive(
    st.none() | st.booleans() | _INT | _TEXT,
    lambda inner: (st.lists(inner, max_size=_MAX_ITEMS)
                   | st.dictionaries(_TEXT, inner, max_size=_MAX_ITEMS)),
    max_leaves=6,
)

_LEAVES: dict[Any, st.SearchStrategy[Any]] = {
    str: _TEXT, int: _INT, bool: st.booleans(), float: _FLOAT,
    type(None): st.none(), Any: _JSON,
}


def _bounded(annotation: Any, metadata: tuple[Any, ...]) -> st.SearchStrategy[Any]:
    """``annotation`` narrowed by whatever ``annotated_types`` bounds accompany it.

    Honoured rather than ignored because a bound is how this side says what the *protocol's* domain
    is, not merely what Python can hold — ``BackendSpec.timeout_s`` is ``Ge(0)`` because the mirrored
    field is a ``u64``. Drawing outside it would report a value neither side ever sends."""
    lo = next((m.ge for m in metadata if isinstance(m, annotated_types.Ge)), None)
    hi = next((m.le for m in metadata if isinstance(m, annotated_types.Le)), None)
    if annotation is int and (lo is not None or hi is not None):
        return st.integers(min_value=lo if lo is not None else -(2**63),
                           max_value=hi if hi is not None else 2**63 - 1)
    return _strategy_for(annotation)


def _strategy_for(annotation: Any) -> st.SearchStrategy[Any]:
    """A strategy for one model field's annotation.

    An annotation this doesn't recognize raises rather than falling back to ``st.from_type``: the
    fallback would draw *something* for a field whose wire domain nobody had thought about, which
    is how a round trip ends up asserting less than it appears to."""
    if isinstance(annotation, typing.TypeAliasType):  # PEP 695 `type X = ...`
        return _strategy_for(annotation.__value__)
    if annotation in _LEAVES:
        return _LEAVES[annotation]
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return st.sampled_from(annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _model_strategy(annotation)
    if typing.is_typeddict(annotation):
        return _typed_dict_strategy(annotation)
    origin, args = typing.get_origin(annotation), typing.get_args(annotation)
    if origin is Annotated:
        return _bounded(args[0], args[1:])
    if origin is Literal:
        return st.sampled_from(args)
    if origin in (types.UnionType, typing.Union):
        return st.one_of(*(_strategy_for(a) for a in args))
    if origin is list:
        return st.lists(_strategy_for(args[0]), max_size=_MAX_ITEMS)
    if origin is dict:
        return st.dictionaries(_strategy_for(args[0]), _strategy_for(args[1]), max_size=_MAX_ITEMS)
    if origin is tuple:
        return st.tuples(*(_strategy_for(a) for a in args))
    raise NotImplementedError(f"no wire strategy for {annotation!r}; teach _strategy_for about it")


def _model_strategy[T: BaseModel](model: type[T]) -> st.SearchStrategy[T]:
    """Every field of ``model``, drawn — including ones added after this test was written.

    Constraints come off ``FieldInfo.metadata``, since pydantic strips the ``Annotated`` wrapper
    from ``.annotation`` and files the bounds separately."""
    return st.builds(model, **{
        name: _bounded(f.annotation, tuple(f.metadata))
        for name, f in model.model_fields.items()
    })


def _typed_dict_strategy(td: Any) -> st.SearchStrategy[dict[str, Any]]:
    """Every key of a ``TypedDict`` mirror (``BackendSpec``), drawn. ``include_extras`` keeps the
    ``Annotated`` bounds that :func:`_bounded` reads — unlike a model, nothing has stripped them."""
    hints = typing.get_type_hints(td, include_extras=True)
    return st.fixed_dictionaries({name: _strategy_for(ann) for name, ann in hints.items()})


def _mirrors_of(payload: Any) -> tuple[Any, ...]:
    """The mirrors whose fields land at ``payload``'s **top level**.

    Several when the root is a tagged union, because the tag and the selected variant's fields arrive
    together at one level. Unwraps only the containers that add no level of their own — an
    ``Annotated`` discriminated union, and a list (a ``Vec<T>`` root presents one ``T``)."""
    if typing.is_typeddict(payload) or (
        isinstance(payload, type) and issubclass(payload, BaseModel)
    ):
        return (payload,)
    origin, args = typing.get_origin(payload), typing.get_args(payload)
    if origin is Annotated:
        return _mirrors_of(args[0])
    if origin in (types.UnionType, typing.Union):
        return tuple(m for a in args for m in _mirrors_of(a))
    if origin is list:
        return _mirrors_of(args[0])
    raise NotImplementedError(f"no mirrors for payload root {payload!r}")


#: Cached because a root's adapter and strategy are asked for once per Hypothesis example, and
#: rebuilding a validator (or a recursive strategy) thousands of times dominates the runtime.
@functools.cache
def _adapter_for(payload: Any) -> TypeAdapter[Any]:
    return TypeAdapter(payload)


@functools.cache
def _payload_strategy(payload: Any) -> st.SearchStrategy[Any]:
    return _strategy_for(payload)


@dataclass(frozen=True)
class Root:
    """One payload root the seam carries whole: its wire name, and the host annotation describing it.

    Only those two are declared. The adapter, the strategy and the field set are all derived from
    ``payload``, so a root cannot describe itself two ways — and adding one means naming it twice
    (here and in the Rust ``WireType``), not five times.

    Reading and writing both go through :attr:`adapter` rather than a model's own
    ``model_dump_json``, so a mirror that is a ``TypedDict`` (:class:`BackendSpec`) needs no separate
    path."""

    ty: str
    payload: Any

    @property
    def adapter(self) -> TypeAdapter[Any]:
        return _adapter_for(self.payload)

    @property
    def strategy(self) -> st.SearchStrategy[Any]:
        return _payload_strategy(self.payload)

    @property
    def declared(self) -> set[str]:
        """The field names at this root's top level."""
        return {name for m in _mirrors_of(self.payload) for name in _field_names(m)}


#: Host → wheel. `author_input` is the one root the host models as a union of variants rather than
#: one struct: the Rust mirror is a single struct with the payload flattened in, and the tag is all
#: that says which fields should be there. `sandbox` is the one mirrored by a `TypedDict`.
OUTBOUND = [
    Root("app_args", AppArgs),
    Root("author_input", AuthorInput),
    Root("failure", Failure),
    Root("target", Target),
    Root("finalize_input", FinalizeInput),
    Root("sandbox", BackendSpec),
]

#: Both an inbound root of its own (what the ``units`` callout returns) and nested inside the
#: outbound :class:`Target`, so it appears in both lists below — one root, two roles.
_UNITS = Root("units", list[Unit])

#: Wheel → host.
INBOUND = [
    Root("app_descriptor", AppDescriptor),
    Root("compile_result", CompileResult),
    Root("validate_outcome", ValidateOutcome),
    Root("prompt", Prompt),
    _UNITS,
    Root("workspace_prep", WorkspacePrep),
    Root("sandbox_grants", SandboxGrants),
]


# ---------------------------------------------------------------------------
# The round trips.
# ---------------------------------------------------------------------------

#: Examples per payload root, pinned rather than left to the active Hypothesis profile. What this
#: test asserts is a contract, so it should assert the same amount on every run — and the profile in
#: effect during a full suite run is whichever one another module loaded at import time, which is no
#: way to decide how hard a protocol gets checked. An example is one pipe round trip, so this is
#: a few seconds for the whole file.
_EXAMPLES = 200

# `deadline=None` throughout: an example spans a subprocess round trip, so per-example timing is
# noisy enough to trip the default deadline for reasons unrelated to the protocol.

@pytest.mark.fuzz
@pytest.mark.parametrize("case", OUTBOUND, ids=[c.ty for c in OUTBOUND])
@settings(deadline=None, max_examples=_EXAMPLES)
@given(data=st.data())
def test_outbound_payload_survives_the_wheel(
    case: Root, wire_echo: WireEcho, data: st.DataObject
) -> None:
    """A payload the host sends reaches the wheel whole: every field the host set is one the Rust
    type declares, so re-reading what Rust made of it gives the value back unchanged."""
    sent = data.draw(case.strategy)
    echoed = wire_echo.echo(case.ty, json.loads(case.adapter.dump_json(sent)))
    assert case.adapter.validate_python(echoed) == case.adapter.validate_python(sent)


@pytest.mark.fuzz
@pytest.mark.parametrize("case", INBOUND, ids=[c.ty for c in INBOUND])
@settings(deadline=None, max_examples=_EXAMPLES)
@given(entropy=st.binary(min_size=1, max_size=512))
def test_inbound_payload_survives_the_host(case: Root, wire_echo: WireEcho, entropy: bytes) -> None:
    """A payload a wheel returns reaches the host whole. Both documents compared here are Rust's
    own serialization, which is what lets the comparison ignore how the two languages spell an
    absent optional (see ``test_an_empty_optional_is_spelled_null_on_both_sides``) and see only content."""
    produced = wire_echo.gen(case.ty, entropy)
    assume(produced is not None)
    reparsed = case.adapter.validate_python(produced)
    redumped = json.loads(case.adapter.dump_json(reparsed))
    assert wire_echo.echo(case.ty, redumped) == produced


#: Draws for the coverage check below. Deterministic (not a Hypothesis property) so the union of
#: fields it observes is the same on every run: a coverage assertion that sometimes passes would be
#: worse than none.
_COVERAGE_DRAWS = 250


def _coverage_entropy(draw: int) -> bytes:
    """Varied but fixed bytes for draw number ``draw``."""
    return hashlib.sha256(str(draw).encode()).digest() * 8


def _field_names(mirror: Any) -> set[str]:
    """The field names of one host-side mirror, model or ``TypedDict``."""
    if typing.is_typeddict(mirror):
        return set(typing.get_type_hints(mirror))
    return set(mirror.model_fields)


def _keys_anywhere(document: object) -> set[str]:
    """Every object key in ``document``, at any depth."""
    match document:
        case dict():
            return set(document).union(*(_keys_anywhere(v) for v in document.values()))
        case list():
            return set().union(*(_keys_anywhere(v) for v in document))
        case _:
            return set()


def _declared_anywhere(adapter: TypeAdapter[Any]) -> set[str]:
    """Every field name in the host's model tree for a payload root, read off its JSON schema so
    this doesn't need a second annotation walker to keep in step with ``_strategy_for``."""
    def walk(node: object) -> set[str]:
        match node:
            case {"properties": dict() as props}:
                return set(props).union(*(walk(v) for v in node.values()))
            case dict():
                return set().union(*(walk(v) for v in node.values()))
            case list():
                return set().union(*(walk(v) for v in node))
            case _:
                return set()

    return walk(adapter.json_schema())


@pytest.mark.parametrize("case", INBOUND, ids=[c.ty for c in INBOUND])
def test_generator_reaches_every_field_the_host_declares(case: Root, wire_echo: WireEcho) -> None:
    """Every field the host declares on an inbound payload is one a wheel actually sends.

    This is the round trip's blind spot, and the reason it needs covering separately: a field only
    the *host* declares, with a default, is exactly what forward compatibility is supposed to
    tolerate — an older wheel omitting it leaves it at its default, and the round trip cannot tell
    that from a field no wheel will ever send, i.e. a typo or a leftover. What Rust's generator
    never emits, nothing emits."""
    observed: set[str] = set()
    for draw in range(_COVERAGE_DRAWS):
        produced = wire_echo.gen(case.ty, _coverage_entropy(draw))
        if produced is not None:
            observed |= _keys_anywhere(produced)
    missing = _declared_anywhere(case.adapter) - observed
    assert not missing, f"{case.ty}: no wheel can send {sorted(missing)} — absent from the Rust type"


#: Structs that only ever travel *inside* an outbound payload. Addressable in their own right so the
#: field-set check can generate one alone: an opaque payload's keys sit at the same depths as a nested
#: struct's, and are not field names at all, so comparing each struct's own top level is what keeps
#: that assertion exact.
NESTED = [
    _UNITS,
    Root("property", Property),
    Root("finalize_component", FinalizeComponent),
    Root("component_outcome", ComponentOutcome),
]

#: Everything reachable on an outbound payload — the roots, plus the structs nested in them. Derived
#: rather than listed again, so the two can't disagree about which roots exist.
MIRRORS = [*OUTBOUND, *NESTED]


#: The modules that between them define every mirror on the seam. Discovery reads these rather than a
#: hand-kept list, so the completeness check below has an independent source of truth.
_ABI_MODULES = (wire_abi, descriptor_abi)


def _defined_mirrors() -> set[Any]:
    """Every mirror the ABI modules define, plus the sandbox layer's one.

    Filtered by ``__module__`` so a name one module imports from another is counted once, where it is
    defined; underscore-prefixed models (``_AuthorInputBase``) are internal shape-sharing, never a
    payload of their own."""
    return {BackendSpec} | {
        obj
        for module in _ABI_MODULES
        for name, obj in vars(module).items()
        if not name.startswith("_")
        and isinstance(obj, type)
        and issubclass(obj, WireModel)
        and obj is not WireModel
        and obj.__module__ == module.__name__
    }


def _mirrors_within(annotation: Any, found: set[Any]) -> None:
    """Collect into ``found`` every mirror ``annotation`` can reach, at any depth."""
    if isinstance(annotation, typing.TypeAliasType):
        _mirrors_within(annotation.__value__, found)
        return
    if typing.is_typeddict(annotation):
        if annotation in found:
            return
        found.add(annotation)
        for field in typing.get_type_hints(annotation, include_extras=True).values():
            _mirrors_within(field, found)
        return
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in found:
            return
        found.add(annotation)
        for field in annotation.model_fields.values():
            _mirrors_within(field.annotation, found)
        return
    for arg in typing.get_args(annotation):
        _mirrors_within(arg, found)


def test_every_outbound_mirror_has_its_own_field_set_check() -> None:
    """Every struct reachable on an outbound payload is compared in its *own* right.

    :data:`MIRRORS` has to name each nested struct, because the field-set check compares only a
    struct's own top level — reach `Target` and you see ``{name, units}``, never a `Unit`'s fields.
    The wire names are the Rust type names, which nothing here can derive without munging a string,
    so the list stays declared and this is the half that keeps it complete."""
    reached: set[Any] = set()
    for root in OUTBOUND:
        _mirrors_within(root.payload, reached)
    checked = {mirror for root in MIRRORS for mirror in _mirrors_of(root.payload)}
    assert not reached - checked, (
        f"reachable on an outbound payload but never compared on its own: "
        f"{sorted(m.__name__ for m in reached - checked)} — add it to NESTED"
    )


def test_every_mirror_is_reachable_from_a_declared_root() -> None:
    """Every mirror the ABI defines is carried by one of the roots above.

    The roots themselves stay declared: a wire name and a direction are not properties of a class, so
    nothing can infer them. What this closes is the gap that leaves — add a payload to
    ``composer.rustapp.wire`` and forget to list it, and it is silently exercised by nothing. Here it
    is discovery against declaration, so the omission fails instead."""
    reached: set[Any] = set()
    for root in [*OUTBOUND, *INBOUND, *NESTED]:
        _mirrors_within(root.payload, reached)
    missed = _defined_mirrors() - reached
    assert not missed, (
        f"no declared root reaches {sorted(m.__name__ for m in missed)} — add it to OUTBOUND or "
        "INBOUND if it is a payload root of its own, or to the payload that carries it if not"
    )


def _top_level_keys(document: object) -> set[str]:
    """The keys of ``document`` itself — not of anything nested inside it. A list stands for its
    elements, which is how a ``Vec<T>`` root presents one ``T``."""
    match document:
        case dict():
            return set(document)
        case list():
            return set().union(*(_top_level_keys(item) for item in document))
        case _:
            return set()


@pytest.mark.parametrize("case", MIRRORS, ids=[m.ty for m in MIRRORS])
def test_wheel_reads_no_field_the_host_never_sends(case: Root, wire_echo: WireEcho) -> None:
    """Every field a wheel can read off an outbound payload is one the host actually sends.

    The mirror of ``test_generator_reaches_every_field_the_host_declares``, and the round trip's
    other blind spot: a field only the *Rust* side declares is filled by ``#[serde(default)]`` — or,
    for an ``Option<T>``, by serde regardless — so the payload deserializes, the field is dropped
    again on the way back out, and the round trip sees nothing wrong. What the wheel would actually
    read is an empty string, a ``None`` or an empty vec, forever, for something no host ever sets."""
    emitted: set[str] = set()
    for draw in range(_COVERAGE_DRAWS):
        produced = wire_echo.gen(case.ty, _coverage_entropy(draw))
        if produced is not None:
            emitted |= _top_level_keys(produced)
    unknown = emitted - case.declared
    assert not unknown, f"{case.ty}: a wheel can read {sorted(unknown)}, which no host sends"


def test_an_empty_optional_is_spelled_null_on_both_sides(wire_echo: WireEcho) -> None:
    """An empty optional has exactly one spelling, and it is ``null``.

    Nothing on this seam omits a key: Rust carries no ``skip_serializing_if`` and the inbound models
    default nothing, so absence is an error on whichever side reads it rather than a second way to
    say "nothing". That is what lets the round trips above compare documents directly — they are
    comparing content, not two conventions for the same value."""
    row = {"property": "p", "unit": "u", "target": None}
    assert wire_echo.echo("units", [row]) == [row]
    assert Unit.model_validate(row) == Unit(property="p", unit="u", target=None)

    absent = {"property": "p", "unit": "u"}
    with pytest.raises(WireFault, match="missing field"):
        wire_echo.echo("units", [absent])
    with pytest.raises(ValidationError):
        Unit.model_validate(absent)


def test_descriptor_rejects_unknown_tags(wire_echo: WireEcho) -> None:
    """``ecosystem`` and ``backend_tag`` are plain ``String`` in Rust but closed sets here, so the
    inbound round trip draws from those sets (``autoprover_sdk::fuzz``) instead of reporting every
    unknown tag as drift. That narrowing is only sound if the host does reject the rest."""
    produced = wire_echo.gen("app_descriptor", bytes(range(64)))
    assert isinstance(produced, dict)
    AppDescriptor.model_validate(produced)
    for field in ("ecosystem", "backend_tag"):
        with pytest.raises(ValidationError):
            AppDescriptor.model_validate({**produced, field: "nonesuch"})
