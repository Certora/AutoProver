import pathlib
import sys
from typing import Sequence, Iterator
import typing
import types
from functools import reduce
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
    BaseApplication, ExplicitContract, ExternalActor, ContractComponent
)
from composer.spec.service_host import Sort # defined here? huh?

import os

REPO_ROOT = pathlib.Path(__file__).parent.parent

TEMPLATES_DIR = REPO_ROOT / "composer" / "templates"

MANIFEST = Manifest.validate_json((REPO_ROOT / "template_manifest.json").read_text())

env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), undefined=StrictUndefined)

FUZZABLE = sorted(
    (key, entry) for key, entry in MANIFEST.items()
)

def _build_template_context[T](t: type[T]) -> st.SearchStrategy[T]:
    """Strategy for a context-marked template-param TypedDict: draw ``sort``,
    then build a ``context`` coherent with it.

    ``sort`` selects the application family (``sort_to_application``); the
    ``context`` is a ``ContractComponentInstance`` pointing into a freshly-drawn
    app of that family, its ``ind`` chosen in-bounds by
    ``app_to_contract``/``contract_to_component``. This keeps the sort-gated,
    subtype-specific fields the templates read off the context (e.g. the
    ``.tag``/``.path`` ``application_context_new.j2`` renders only under
    ``sort == "update"``) present exactly when the template will touch them, and
    keeps the embedded indices valid. When the field is ``Optional`` the strategy
    also yields ``None`` to exercise the ``{% if context %}``-absent branch. Any
    remaining fields are drawn normally.
    """
    annots = typing.get_type_hints(t)
    ctx_ann = annots["context"]
    optional = type(None) in typing.get_args(ctx_ann)

    def _context(sort: Sort) -> st.SearchStrategy[ContractComponentInstance | None]:
        component = contract_to_component(app_to_contract(sort_to_application(sort)))
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

def _unwrap_component_types(
    t: typing.TypeAliasType | type
) -> tuple[Sequence[type[ExplicitContract]], Sequence[type[ExternalActor]]]:
    if isinstance(t, typing.TypeAliasType):
        return _unwrap_component_types(
            t.__value__
        )
    assert typing.get_origin(t) in (typing.Union, types.UnionType)
    variants = typing.get_args(t)
    to_ret_contracts : list[type[ExplicitContract]] = []
    to_ret_external : list[type[ExternalActor]] = []
    for t in variants:
        assert isinstance(t, type) and issubclass(t, BaseModel)
        if issubclass(t, ExternalActor):
            to_ret_external.append(t)
        else:
            assert issubclass(t, ExplicitContract)
            to_ret_contracts.append(t)
    return (to_ret_contracts, to_ret_external)

def _pattern_of(field_info: FieldInfo):
    return next((m.pattern for m in field_info.metadata
             if getattr(m, "pattern", None)), None)

def _field_strategy[T: BaseModel](n: str, f: FieldInfo, cls: type[T]):
    """Strategy for a single model field ``n`` of ``cls``, overriding
    Hypothesis's annotation-only inference wherever that inference would
    produce a value Pydantic rejects or the downstream code cannot use.

    The ``st.from_type`` calls here recurse through the registry populated in
    ``_register_strategies`` (nested models are registered too), so pattern-aware,
    list-shaped building propagates all the way down without any interception of
    Hypothesis internals.
    """
    # A `pattern=` constraint lives in FieldInfo.metadata, invisible to the
    # annotation Hypothesis inspects; generate straight from the regex so the
    # value passes validation (e.g. `solidity_identifier`).
    if (p := _pattern_of(f)):
        return st.from_regex(p, fullmatch=True)
    ann = f.annotation
    assert ann is not None
    # `components` is `list[<contract | external-actor union>]`. Force at least
    # one contract (downstream indexing and `contract_components` need a
    # non-empty contract set) plus a bounded external-actor mix, then permute.
    if issubclass(cls, BaseApplication) and n == "components":
        assert typing.get_origin(ann) is list
        component_type = next(iter(typing.get_args(ann)))
        (contracts, external) = _unwrap_component_types(component_type)
        contract_types = reduce(
            operator.or_,
            (st.from_type(t) for t in contracts)
        )
        external_types = reduce(
            operator.or_,
            (st.from_type(t) for t in external)
        )
        external_comps = st.lists(external_types, max_size=2)
        return st.lists(contract_types, min_size=1, max_size=3).flatmap(lambda conts: \
            external_comps.flatmap(lambda exts:
                st.permutations([
                    *conts, *exts
                ])
            )
        )
    # At least one sub-component so a ContractComponentInstance built off this
    # contract always has a component to point at.
    if issubclass(cls, ExplicitContract) and n == "components":
        return st.lists(st.from_type(ContractComponent), min_size=1)
    return st.from_type(ann)

def _model_strategy[T: BaseModel](cls: type[T]) -> st.SearchStrategy[T]:
    """Build ``cls`` with an explicit strategy per model field (see
    ``_field_strategy``), replacing Hypothesis's annotation-only inference."""
    return st.builds(cls, **{n: _field_strategy(n, f, cls) for n, f in cls.model_fields.items()})

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

def sort_to_application(
    sort: Sort
) -> st.SearchStrategy[AnyApplication]:
    t = Application if sort == "greenfield" else (
        FromSourceApplication if sort == "update" else
        (FromSourceApplication | HarnessedApplication | FromSourceApplication)
    )
    return st.from_type(t)

def _all_subclasses(cls: type) -> Iterator[type]:
    for sub in cls.__subclasses__():
        yield sub
        yield from _all_subclasses(sub)

def _is_first_party(cls: type) -> bool:
    """True iff ``cls`` is defined in this repo's own source (not an installed
    dependency). The path check excludes ``site-packages``/``.venv`` because the
    virtualenv lives *inside* the repo root, so a plain ``relative_to(REPO_ROOT)``
    would otherwise sweep in every third-party model (langchain, pydantic, ...)."""
    mod = sys.modules.get(cls.__module__)
    f = getattr(mod, "__file__", None)
    if not f:
        return False
    p = pathlib.Path(f).resolve()
    try:
        p.relative_to(REPO_ROOT)
    except ValueError:
        return False
    return "site-packages" not in p.parts and ".venv" not in p.parts

def _register_strategies() -> None:
    """Register, via the public ``st.register_type_strategy`` API, a strategy for
    every type a template param can bottom out in — replacing the old monkeypatch
    of Hypothesis's private ``_from_type``.

    This works at every nesting depth because Hypothesis's resolver consults the
    same global registry for top-level *and* nested field types; a strategy
    registered for ``FromSourceContract`` is honoured whether it is asked for
    directly or as an element of some other model's ``components`` list.

    Completeness relies on the target types being *imported* when we scan:

    * Pydantic models — every first-party ``BaseModel`` subclass currently loaded
      gets a pattern-aware, list-shaped builder (see ``_model_strategy``).
      Resolving every template's params below force-imports the modules that
      define the params and, transitively, the models they reference, so the scan
      sees the full set. Registering an unused model is inert (strategies are lazy
      and only consulted if that type is actually drawn).
    * Context params — TypedDicts stamped with the context marker get the coupled
      ``sort``/``context`` builder (see ``_build_template_context``).

    If a future template references a first-party model that is *not* transitively
    imported by param resolution, that model will not be registered and Hypothesis
    will fall back to annotation-only building — surfacing as a loud
    ``ValidationError`` naming the model, not silent bad data. The fix is to make
    sure it gets imported (usually automatic, since params reference their models
    by real — non-``from __future__`` — annotations).
    """
    context_params: set[type] = set()
    for _key, entry in FUZZABLE:
        for t in resolve_params(entry):
            if getattr(t, _context_marker_attr, None) is not None:
                context_params.add(t)

    st.register_type_strategy(ContractInstance, contract_resolver)
    st.register_type_strategy(ContractComponentInstance, instance_resolver)

    for model in set(_all_subclasses(BaseModel)):
        if model in (ContractInstance, ContractComponentInstance) or not _is_first_party(model):
            continue
        st.register_type_strategy(model, lambda _t, m=model: _model_strategy(m))

    for t in context_params:
        st.register_type_strategy(t, lambda _x, tt=t: _build_template_context(tt))

_register_strategies()

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
    # Strategies for Pydantic models and context params are registered once at
    # import time via the public `st.register_type_strategy` (see
    # `_register_strategies`); nothing to patch here.
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
