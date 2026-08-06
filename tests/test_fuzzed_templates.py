import pathlib
from typing import Any, Callable, Protocol, Sequence
import typing
import types
from functools import wraps, reduce
import operator

import pytest
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from hypothesis import HealthCheck, given, settings, strategies as st, Phase
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from composer.meta.types import Manifest
from composer.meta.resolver import resolve_params
from composer.spec.system_model import (
    ContractComponentInstance, ContractInstance, AnyApplication, Application, FromSourceApplication,
    HarnessedApplication, _context_marker_attr,
    BaseApplication
)
from composer.spec.solana.model import (
    SolanaApplication, SolanaComponentInstance, SolanaProgramInstance
)
from composer.spec.service_host import Sort # defined here? huh?
import hypothesis.strategies._internal.core as hcore

import os

REPO_ROOT = pathlib.Path(__file__).parent.parent

TEMPLATES_DIR = REPO_ROOT / "composer" / "templates"

MANIFEST = Manifest.validate_json((REPO_ROOT / "template_manifest.json").read_text())

env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), undefined=StrictUndefined)

#: Cap on any drawn list field (see ``_field_strategy``). Templates iterate these; a handful of
#: elements covers the same branches as an unbounded draw, at a fraction of the entropy.
_MAX_LIST = 3

FUZZABLE = sorted(
    (key, entry) for key, entry in MANIFEST.items()
)

class _TypeResolver(Protocol):
    def __call__[T](self, thing: type[T]) -> st.SearchStrategy[T]:
        ...


def _unit_type_of(ctx_ann: typing.Any) -> type:
    """The concrete unit type a marked param dict declares for its ``context``, with any
    ``| None`` stripped. Each ecosystem names its own (``ContractComponentInstance``,
    ``SolanaComponentInstance``) precisely so this is constructible — the ``FeatureUnit``
    protocol the pipeline speaks is not."""
    variants = [a for a in typing.get_args(ctx_ann) if a is not type(None)]
    if not variants:
        assert isinstance(ctx_ann, type)
        return ctx_ann
    assert len(variants) == 1 and isinstance(variants[0], type), ctx_ann
    return variants[0]


def _build_template_context[T](t: type[T]) -> st.SearchStrategy[T]:
    """Strategy for a context-marked template-param TypedDict: draw ``sort``,
    then build a ``context`` coherent with it.

    ``sort`` selects the application family (``sort_to_application``); the ``context`` is a unit
    of the *declared* type pointing into a freshly-drawn app of that family, its ``ind`` chosen
    in-bounds (``app_to_contract``/``contract_to_component`` for EVM,
    ``solana_component_resolver`` for Solana — see ``_COHERENT_UNITS``). This keeps the sort-gated,
    subtype-specific fields the templates read off the context (e.g. the ``.tag``/``.path``
    ``application_context_new.j2`` renders only under ``sort == "update"``) present exactly when
    the template will touch them, and keeps the embedded indices valid. When the field is
    ``Optional`` the strategy also yields ``None`` to exercise the ``{% if context %}``-absent
    branch. Any remaining fields are drawn normally.
    """
    annots = typing.get_type_hints(t)
    ctx_ann = annots["context"]
    optional = type(None) in typing.get_args(ctx_ann)
    unit_type = _unit_type_of(ctx_ann)
    assert unit_type in _COHERENT_UNITS, (
        f"{t.__name__} declares context: {unit_type.__name__}, which has no coherent-unit "
        f"strategy. Add one to _COHERENT_UNITS — drawing it independently of `sort` (or with "
        f"out-of-bounds indices) makes the template crash rather than exercising it."
    )

    def _context(sort: Sort) -> st.SearchStrategy[object | None]:
        component = _COHERENT_UNITS[unit_type](sort)
        # Exercise the `{% if context %}`-absent branch when the field permits None.
        return st.none() | component if optional else component

    other_fields = {
        k: st.from_type(t) for (k, t) in annots.items() if k != "sort" and k != "context"
    }

    return st.from_type(annots["sort"]).flatmap(lambda sort: \
        st.builds(t,
            sort=st.just(sort),
            context=_context(sort),
            **other_fields
        )
    )

def _bears_units(t: type[BaseModel]) -> bool:
    """Whether template units are indexed *out of* this component type — i.e. it carries its own
    ``components`` list (EVM's ``ExplicitContract``, Solana's ``SolanaProgram``), as opposed to an
    external actor / authority component, which has none. This is the property the shaping below
    exists to guarantee: an app must hold at least one unit-bearing component, and each of those
    at least one component, or indexing a unit out of it has nothing to land on."""
    return "components" in t.model_fields

def _unwrap_component_types(
    t: typing.TypeAliasType | type
) -> tuple[Sequence[type[BaseModel]], Sequence[type[BaseModel]]]:
    """Split an app's ``components`` element union into (unit-bearing, other) — see
    :func:`_bears_units`."""
    if isinstance(t, typing.TypeAliasType):
        return _unwrap_component_types(
            t.__value__
        )
    assert typing.get_origin(t) in (typing.Union, types.UnionType)
    variants = typing.get_args(t)
    to_ret_bearing : list[type[BaseModel]] = []
    to_ret_other : list[type[BaseModel]] = []
    for t in variants:
        assert isinstance(t, type) and issubclass(t, BaseModel)
        if _bears_units(t):
            to_ret_bearing.append(t)
        else:
            to_ret_other.append(t)
    assert to_ret_bearing, f"no unit-bearing component type among {variants}"
    return (to_ret_bearing, to_ret_other)

def _make_cursed_patcher(wrapped: _TypeResolver) -> _TypeResolver:
    """Wrap Hypothesis's internal type resolver so Pydantic models and
    context-marked template-param TypedDicts are built by *our* strategies at
    every nesting depth.

    Why it exists
    -------------
    The fuzz test hands each template a random-but-valid instance of its
    parameter TypedDict(s). Those params bottom out in two kinds of value
    Hypothesis cannot generate correctly on its own:

    * **Pydantic models** (``Application``, ``ExplicitContract``, ...). Their
      validation constraints live in ``FieldInfo`` metadata, not in the type
      annotation Hypothesis inspects — e.g. ``solidity_identifier`` carries a
      ``pattern=`` regex. Default ``builds``-from-annotations produces strings
      that fail that validator, so every draw raises ``ValidationError`` instead
      of yielding a model. Component lists likewise need shaping (see
      ``_field_strategy``) or the app has no contract to index into.
    * **Context params** (TypedDicts stamped with the context marker). Their
      ``sort`` and ``context`` fields are coupled: templates read sort-gated,
      subtype-specific fields off the context (``application_context_new.j2``
      renders an ommer contract's ``.tag``/``.path`` only when
      ``sort == "update"``, and those fields exist only on ``FromSourceContract``
      variants). Drawing the two independently pairs ``sort == "update"`` with an
      app lacking ``.tag`` — a ``StrictUndefined`` crash that is a fuzzer
      artifact, not a template bug. The context also embeds array indices that
      must stay in bounds for the app it points at.

    Why not the obvious approaches
    ------------------------------
    ``st.register_type_strategy`` (used here for the two dataclasses) registers
    one exact type at a time. The Pydantic handling is instead a single blanket
    rule keyed on "is a ``BaseModel``" covering an open-ended hierarchy;
    enumerating every model to register it would be tedious and perpetually
    incomplete. Patching the *public* ``st.from_type`` would not help either:
    within one generation Hypothesis recurses into nested field types through
    its private module-level resolver, so a public-API hook never sees the
    nested models or params.

    How it works — and what's cursed
    --------------------------------
    The caller monkeypatches ``hypothesis.strategies._internal.core._from_type``
    — an undocumented private global — with the wrapper returned here for the
    duration of one test, restoring it in a ``finally``. Every type resolution,
    top-level and nested, funnels through that one function, so wrapping it once
    intercepts the whole tree. ``@wraps`` preserves the original resolver's
    identity for anything that introspects it.

    The wrapper is a dispatcher:

    * ``BaseModel`` subclass -> ``_model_strategy`` (pattern-aware, list-shaped
      building via explicit per-field strategies).
    * a type carrying the context marker attribute -> ``_build_template_context``
      (draw ``sort``, then a coherent in-bounds ``context``).
    * anything else -> the original resolver.

    The recursion inside ``_field_strategy`` / ``_build_template_context`` calls
    ``st.from_type`` again, which re-enters this same wrapper *because* the global
    is patched — that is how pattern-aware, index-safe building propagates all
    the way down.
    """
    assert callable(wrapped)

    def _pattern_of(field_info: FieldInfo):
        return next((m.pattern for m in field_info.metadata
                 if getattr(m, "pattern", None)), None)

    def _len_bounds_of(field_info: FieldInfo) -> tuple[int | None, int | None]:
        """The field's ``(min_length, max_length)`` — ``MinLen``/``MaxLen`` in the same metadata that
        hides ``pattern=``; ``None`` where unconstrained."""
        def bound(attr: str) -> int | None:
            return next((v for m in field_info.metadata
                         if (v := getattr(m, attr, None)) is not None), None)
        return bound("min_length"), bound("max_length")

    def _field_strategy[T: BaseModel](n: str, f: FieldInfo, cls: type[T]):
        """Strategy for a single model field ``n`` of ``cls``, overriding
        Hypothesis's annotation-only inference wherever that inference would
        produce a value Pydantic rejects or the downstream code cannot use."""
        (lo, hi) = _len_bounds_of(f)
        # A `pattern=` constraint lives in FieldInfo.metadata, invisible to the
        # annotation Hypothesis inspects; generate straight from the regex so the
        # value passes validation (e.g. `solidity_identifier`).
        if (p := _pattern_of(f)):
            assert (lo, hi) == (None, None), (
                f"{cls.__name__}.{n} constrains pattern *and* length; `from_regex` honors only the "
                f"pattern. Combine them here."
            )
            return st.from_regex(p, fullmatch=True)
        ann = f.annotation
        assert ann is not None
        # Same invisible metadata: Hypothesis draws the `''`/`[]` these forbid, so every draw of the
        # owning model dies in validation and the template never renders (`PropertyGroup.slug`,
        # min_length=1). Assert on anything else constrained, so the next one names itself.
        if (lo, hi) != (None, None):
            if ann is str:
                return st.text(min_size=lo or 0, max_size=hi)
            assert typing.get_origin(ann) is list, (
                f"unhandled length constraint on {cls.__name__}.{n}: {ann}"
            )
            (elem,) = typing.get_args(ann)
            # Capped at `_MAX_LIST` (see below), never below the declared minimum.
            return st.lists(st.from_type(elem), min_size=lo or 0,
                            max_size=max(lo or 0, min(hi or _MAX_LIST, _MAX_LIST)))
        # `components` is `list[<unit-bearing | other union>]` (EVM: contract | external actor;
        # Solana: program | authority). Force at least one unit-bearing component (downstream
        # indexing and `contract_components`/`programs` need a non-empty set) plus a bounded mix
        # of the others, then permute. Each variant recurses through the patched resolver, so
        # nested models are themselves built pattern-aware.
        if issubclass(cls, BaseApplication) and n == "components":
            assert typing.get_origin(ann) is list
            component_type = next(iter(typing.get_args(ann)))
            (bearing, other) = _unwrap_component_types(component_type)
            bearing_types = reduce(
                operator.or_,
                (st.from_type(t) for t in bearing)
            )
            bearing_comps = st.lists(bearing_types, min_size=1, max_size=3)
            if not other:
                return bearing_comps
            other_types = reduce(
                operator.or_,
                (st.from_type(t) for t in other)
            )
            other_comps = st.lists(other_types, max_size=2)
            return bearing_comps.flatmap(lambda bears: \
                other_comps.flatmap(lambda others:
                    st.permutations([
                        *bears, *others
                    ])
                )
            )
        # At least one sub-component so a unit built off this component always has something to
        # point at. Element type comes from the annotation rather than being hardcoded, so this
        # covers `ExplicitContract.components` and `SolanaProgram.components` alike.
        if n == "components" and _bears_units(cls):
            assert typing.get_origin(ann) is list
            return st.lists(st.from_type(next(iter(typing.get_args(ann)))), min_size=1)
        # Cap every other list field. The Solana program model nests list-of-model three deep
        # (program -> instructions -> accounts -> roles); drawn unbounded at each level, a single
        # app blows past Hypothesis's entropy budget under the `extended` profile. Templates only
        # ever iterate these, so a few elements exercise the same paths as a hundred. The element
        # type recurses through the patched resolver, so nesting stays pattern-aware.
        if typing.get_origin(ann) is list:
            (elem,) = typing.get_args(ann)
            return st.lists(st.from_type(elem), max_size=_MAX_LIST)
        return st.from_type(ann)

    def _model_strategy[T: BaseModel](cls: type[T]) -> st.SearchStrategy[T]:
        """Build ``cls`` with an explicit strategy per model field (see
        ``_field_strategy``), replacing Hypothesis's annotation-only inference."""
        return st.builds(cls, **{n: _field_strategy(n, f, cls) for n, f in cls.model_fields.items()})

    @wraps(wrapped)
    def _cursed_base_model_patch[T](thing: type[T]) -> st.SearchStrategy[T]:
        if isinstance(thing, type) and issubclass(thing, BaseModel):
            return _model_strategy(thing)
        elif isinstance(thing, type) and getattr(thing, _context_marker_attr, None) is not None:
            return _build_template_context(thing)
        else:
            return wrapped(thing)
    return _cursed_base_model_patch

def contract_resolver(t: type) -> st.SearchStrategy[ContractInstance]:
    builder : st.SearchStrategy[AnyApplication] = st.from_type(AnyApplication) # type: ignore

    return builder.filter(
        lambda x: len(x.contract_components) > 0
    ).flatmap(
        lambda sampled_app: \
            st.builds(
                ContractInstance,
                ind=st.integers(min_value=0, max_value=len(sampled_app.contract_components) - 1),
                app=st.just(sampled_app)
            )
    )

def instance_resolver(t: type) -> st.SearchStrategy[ContractComponentInstance]:
    builder: st.SearchStrategy[ContractInstance] = st.from_type(ContractInstance)
    return builder.filter(
        lambda c: len(c.contract.components) > 0
    ).flatmap(
        lambda c: \
            st.builds(
                ContractComponentInstance,
                ind=st.integers(min_value=0, max_value=len(c.contract.components) - 1),
                _contract=st.just(c)
            )
    )

def app_to_contract[A: AnyApplication](s: st.SearchStrategy[A]) -> st.SearchStrategy[ContractInstance]:
    return s.filter(
        lambda x: len(x.contract_components) > 0
    ).flatmap(lambda x: \
        st.builds(
            ContractInstance,
            ind=st.integers(min_value=0, max_value=len(x.contract_components) - 1),
            app=st.just(x)
        )
    )

def contract_to_component(s: st.SearchStrategy[ContractInstance]) -> st.SearchStrategy[ContractComponentInstance]:
    return s.filter(
        lambda x: len(x.contract.components) > 0
    ).flatmap(lambda inst: \
        st.builds(
            ContractComponentInstance,
            ind=st.integers(min_value=0, max_value=len(inst.contract.components) - 1),
            _contract=st.just(inst)
        )
    )

def solana_program_resolver(t: type) -> st.SearchStrategy[SolanaProgramInstance]:
    return st.from_type(SolanaApplication).filter(
        lambda a: len(a.programs) > 0
    ).flatmap(lambda app: \
        st.builds(
            SolanaProgramInstance,
            ind=st.integers(min_value=0, max_value=len(app.programs) - 1),
            app=st.just(app)
        )
    )

def solana_component_resolver(t: type) -> st.SearchStrategy[SolanaComponentInstance]:
    return st.from_type(SolanaProgramInstance).filter(
        lambda p: len(p.program.components) > 0
    ).flatmap(lambda prog: \
        st.builds(
            SolanaComponentInstance,
            ind=st.integers(min_value=0, max_value=len(prog.program.components) - 1),
            _program=st.just(prog)
        )
    )

def sort_to_application(
    sort: Sort
) -> st.SearchStrategy[AnyApplication]:
    t = Application if sort == "greenfield" else (
        FromSourceApplication if sort == "update" else
        (FromSourceApplication | HarnessedApplication | FromSourceApplication)
    )
    return st.from_type(t)

st.register_type_strategy(ContractInstance, contract_resolver)

st.register_type_strategy(ContractComponentInstance, instance_resolver)

st.register_type_strategy(SolanaProgramInstance, solana_program_resolver)

st.register_type_strategy(SolanaComponentInstance, solana_component_resolver)

#: How to draw a marked param dict's ``context``, per concrete unit type the ecosystems declare
#: (see ``_build_template_context``). EVM's is sort-coherent: its prompts branch on ``sort`` and
#: read subtype-specific fields off the app, so the unit must come from an app of the matching
#: family. Solana's templates have no ``sort`` branch (no greenfield/update split), so its unit is
#: drawn independently — the registered resolver already keeps the indices in bounds.
_COHERENT_UNITS: dict[type, Callable[[Sort], st.SearchStrategy[Any]]] = {
    ContractComponentInstance: lambda sort: contract_to_component(app_to_contract(sort_to_application(sort))),
    SolanaComponentInstance: lambda _sort: st.from_type(SolanaComponentInstance),
}

settings.register_profile("quick", settings(
    max_examples=10,
    suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.data_too_large]
))

settings.register_profile("extended", settings(
    max_examples=500,
))

settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "quick"))

@pytest.mark.parametrize(
    "key,entry", FUZZABLE, ids=[entry.template_name for _, entry in FUZZABLE]
)
@pytest.mark.fuzz
@settings(
    deadline=None,
    phases=(Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink),
)
@given(data=st.data())
def test_template_renders_under_fuzzed_params(key, entry, data):
    # Monkeypatch Hypothesis's private internal type resolver for the duration of
    # this test (restored in `finally`). Every type resolution — top-level and
    # every nested field — funnels through this one global, which is what lets the
    # patcher intercept Pydantic models and context params at any depth. See
    # `_make_cursed_patcher`.
    old = hcore._from_type
    hcore._from_type = _make_cursed_patcher(old)
    try:
        param_types = resolve_params(entry)
        template_params = st.tuples(
            *(
                st.from_type(t) for t in param_types
            )
        ).map(lambda ab: {
            k: v for tup in ab for (k,v) in tup.items()
        })
        params = data.draw(template_params, label=f"params for {key}")
        template = env.get_template(entry.template_name)
        template.render(**params)
    finally:
        hcore._from_type = old
