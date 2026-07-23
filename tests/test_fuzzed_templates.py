import pathlib
from typing import Protocol
from functools import wraps
import pytest
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from hypothesis import HealthCheck, given, settings, strategies as st, Phase
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from composer.meta.types import Manifest
from composer.meta.resolver import resolve_params
from composer.spec.system_model import ContractComponentInstance, ContractInstance, AnyApplication, ExplicitContract
import hypothesis.strategies._internal.core as hcore

REPO_ROOT = pathlib.Path(__file__).parent.parent

TEMPLATES_DIR = REPO_ROOT / "composer" / "templates"

MANIFEST = Manifest.validate_json((REPO_ROOT / "template_manifest.json").read_text())

env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), undefined=StrictUndefined)

FUZZABLE = sorted(
    (key, entry) for key, entry in MANIFEST.items()
)

class _TypeResolver(Protocol):
    def __call__[T](self, thing: type[T]) -> st.SearchStrategy[T]:
        ...

def _make_cursed_patcher(wrapped: _TypeResolver) -> _TypeResolver:
    assert callable(wrapped)

    def _pattern_of(field_info: FieldInfo):
        return next((m.pattern for m in field_info.metadata
                 if getattr(m, "pattern", None)), None)

    def _field_strategy(f: FieldInfo):
        if (p := _pattern_of(f)):
            return st.from_regex(p, fullmatch=True)
        ann = f.annotation
        assert ann is not None
        return st.from_type(ann)

    def _model_strategy[T: BaseModel](cls: type[T]) -> st.SearchStrategy[T]:
        return st.builds(cls, **{n: _field_strategy(f) for n, f in cls.model_fields.items()})

    @wraps(wrapped)
    def _cursed_base_model_patch[T](thing: type[T]) -> st.SearchStrategy[T]:
        if isinstance(thing, type) and issubclass(thing, BaseModel):
            return _model_strategy(thing)
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
    assert isinstance(t, type) and t is ContractComponentInstance
    return st.from_type(ContractInstance).filter(
        lambda c: len(c.contract.components) > 0
    ).flatmap(
        lambda c: \
            st.builds(
                ContractComponentInstance,
                ind=st.integers(min_value=0, max_value=len(c.contract.components) - 1),
                _contract=st.just(c)
            )
    )

st.register_type_strategy(ContractInstance, contract_resolver)

st.register_type_strategy(ContractComponentInstance, instance_resolver)

@pytest.mark.parametrize(
    "key,entry", FUZZABLE, ids=[entry.template_name for _, entry in FUZZABLE]
)
@settings(
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    phases=(Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink),
)
@given(data=st.data())
def test_template_renders_under_fuzzed_params(key, entry, data):
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
