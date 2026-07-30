from typing import Callable, Literal, Annotated, Mapping, TypedDict, TYPE_CHECKING, Protocol
from functools import cache
from importlib.resources import files
from dataclasses import dataclass
from pydantic import BaseModel, Field, BeforeValidator
import yaml

from composer.templates.loader import load_jinja_template
from composer.spec.gen_types import TypedTemplate
from composer.llm.types import CacheLevel

if TYPE_CHECKING:
    from graphcore.graph import MessagePayloadType

class MessageCacher(Protocol):
    def cache_marker(self, payload: "MessagePayloadType", cache_level: CacheLevel) -> "MessagePayloadType":
        ...

type _ContextLoader = Callable[[], str]

type RecipeChannel = Literal["CVL", "CONF", "EDIT"]

ChannelList = Annotated[list[RecipeChannel], BeforeValidator(
    lambda v: [v] if isinstance(v, str) else v
)]

KB_TOOL_NAME = "get_cvl_recipe"

class KBRecipe(BaseModel):
    id: str
    name: str
    triggers: list[str]
    file: str
    search_terms: list[str] = Field(default_factory=list)
    note: str | None = Field(default=None)

class IndexModel(BaseModel):
    recipes: list[KBRecipe]

class RecipeIndexParams(TypedDict):
    recipes: list[KBRecipe]
    kb_retrieval_name: str

index_template = TypedTemplate[RecipeIndexParams]("cvl_kb_index.j2")

def _resource_file_loader(s: str) -> _ContextLoader:
    return lambda: (files() / "resources" / s).read_text()

@dataclass
class ContextSpec:
    title: str
    loader: _ContextLoader

@cache
def _kb_model() -> IndexModel:
    index_text = (files() / "resources" / "cvl_recipes_index.yaml").read_text()
    return IndexModel.model_validate(yaml.safe_load(index_text))

@cache
def _kb_index() -> Mapping[str, str]:
    to_ret : dict[str, str] = {}
    for r in _kb_model().recipes:
        to_ret[r.id] = r.file
    return to_ret

@cache
def kb_loader() -> Callable[[str], str | None]:
    ind = _kb_index()
    kb_resource_dir = files() / "resources"
    def loader(id: str) -> str | None:
        if id not in ind:
            return None
        f = kb_resource_dir / ind[id]
        if not f.is_file():
            return None
        return f.read_text()
    return loader

_INDEX = [
    ContextSpec(
        title="CVL Baseline Knowledge",
        loader=_resource_file_loader("cvl_baseline_facts.md")
    ),
    ContextSpec(
        title="CVL Summarization and Linking Guide",
        loader=_resource_file_loader(
            "cvl_summarization_rag_draft.md"
        )
    ),
    ContextSpec(
        title="Invariants and Quantifiers Guide",
        loader=_resource_file_loader(
            "cvl_invariants_quantifiers.md"
        )
    ),
    ContextSpec(
        title="CVL Recipes",
        loader=lambda: index_template.bind({
            "kb_retrieval_name": KB_TOOL_NAME,
            "recipes": _kb_model().recipes
        }).render_to(load_jinja_template)
    )
]

@cache
def cvl_context_raw() -> "MessagePayloadType":
    return [
f"""
<context-document>
Title: {s.title}

{s.loader()}
</context-document>
""" for s in _INDEX
    ]

def cvl_context(r: MessageCacher, level: CacheLevel) -> "MessagePayloadType":
    return r.cache_marker(cvl_context_raw(), level)
