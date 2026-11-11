from typing import Callable, Literal, Annotated, Mapping, Self, Sequence, TypedDict, cast, TYPE_CHECKING
from functools import cache
from importlib.resources import files
from dataclasses import dataclass
from pydantic import BaseModel, ConfigDict, Field, BeforeValidator, model_validator
import yaml

from graphcore.graph import CacheMarker

from composer.templates.loader import load_jinja_template
from composer.spec.gen_types import TypedTemplate

if TYPE_CHECKING:
    from graphcore.graph import PromptInput

type _ContextLoader = Callable[[], str]

#: Where the CVL fix is made: the spec, the prover configuration, or the source.
type CvlChannel = Literal["CVL", "CONF", "EDIT"]


def _one_or_many(v: object) -> object:
    return [v] if isinstance(v, str) else v


class KBRecipe[C: str](BaseModel):
    """One recipe in an index.

    ``C`` is the channel names for one agent family. A channel is a tool that family has, so each
    family keeps its own set.
    """

    id: str
    name: str
    triggers: list[str]
    file: str
    search_terms: list[str] = Field(default_factory=list)
    note: str | None = Field(default=None)
    channel: Annotated[list[C], BeforeValidator(_one_or_many)]


class IndexModel[C: str](BaseModel):
    #: Unknown keys are an error. The agent only sees what ``kb_index.j2`` renders, so prose added
    #: to the YAML would be dropped on load.
    model_config = ConfigDict(extra="forbid")

    #: Rendered with the index. This is where the agent reads what each channel means.
    channels: dict[C, str]
    recipes: list[KBRecipe[C]]

    @model_validator(mode="after")
    def _every_channel_has_a_meaning(self) -> Self:
        unexplained = {c for r in self.recipes for c in r.channel} - self.channels.keys()
        if unexplained:
            raise ValueError(f"recipes use channels the legend does not explain: {sorted(unexplained)}")
        return self


class RecipeIndexParams(TypedDict):
    #: ``KBRecipe[str]`` because the template only prints the channel name. The vocabulary is
    #: checked when the index loads, and ``composer.meta.resolver`` cannot resolve a generic
    #: params type.
    recipes: Sequence[KBRecipe[str]]
    channels: Mapping[str, str]
    kb_retrieval_name: str
    label: str


def _resource_file_loader(s: str) -> _ContextLoader:
    return lambda: (files() / "resources" / s).read_text()


@dataclass(frozen=True)
class ContextSpec:
    title: str
    loader: _ContextLoader


@dataclass(frozen=True)
class RecipeSet[C: str]:
    """The recipe index shown to every agent in the family, and the bodies fetched by id."""

    tool_name: str
    index_resource: str
    parse_index: Callable[[object], IndexModel[C]]
    index_template: TypedTemplate[RecipeIndexParams]


@dataclass(frozen=True)
class KnowledgeBundle[C: str]:
    """Context documents for one agent family, and a recipe set when the family has recipes.

    Frozen so the loaders can be memoized on the bundle. Each document is read once per process.
    Anything that changes between runs belongs in the agent's own prompt.
    """

    #: The manual name used in the recipe index preamble.
    label: str
    specs: tuple[ContextSpec, ...]
    recipes: RecipeSet[C] | None = None


# Not cached. ``functools.cache`` erases the generic return type. Every caller is cached, so
# this runs at most twice per recipe set.
def _kb_model[C: str](recipes: RecipeSet[C]) -> IndexModel[C]:
    index_text = (files() / "resources" / recipes.index_resource).read_text()
    return recipes.parse_index(yaml.safe_load(index_text))


def _kb_index[C: str](recipes: RecipeSet[C]) -> Mapping[str, str]:
    return {r.id: r.file for r in _kb_model(recipes).recipes}


@cache
def kb_loader[C: str](recipes: RecipeSet[C]) -> Callable[[str], str | None]:
    ind = _kb_index(recipes)
    kb_resource_dir = files() / "resources"

    def loader(id: str) -> str | None:
        if id not in ind:
            return None
        f = kb_resource_dir / ind[id]
        if not f.is_file():
            return None
        return f.read_text()

    return loader


def _recipe_index_document[C: str](label: str, recipes: RecipeSet[C]) -> str:
    model = _kb_model(recipes)
    params: RecipeIndexParams = {
        "kb_retrieval_name": recipes.tool_name,
        "recipes": cast(Sequence[KBRecipe[str]], model.recipes),
        "channels": cast(Mapping[str, str], model.channels),
        "label": label,
    }
    return recipes.index_template.bind(params).render_to(load_jinja_template)


@cache
def context_documents[C: str](bundle: KnowledgeBundle[C]) -> tuple[str, ...]:
    """The bundle's documents, with the recipe index last.

    Specs are loaded from files. The index is rendered from the bundle, so it is appended here.
    """
    titled = [(s.title, s.loader()) for s in bundle.specs]
    if bundle.recipes is not None:
        titled.append(
            (f"{bundle.label} Recipes", _recipe_index_document(bundle.label, bundle.recipes))
        )
    return tuple(
        f"""
<context-document>
Title: {title}

{body}
</context-document>
"""
        for title, body in titled
    )


def with_context[C: str](bundle: KnowledgeBundle[C], prompt: "PromptInput") -> "PromptInput":
    """The bundle's documents, a cache marker, then ``prompt``.

    The marker ends the cached prefix. The cache TTL comes from the builder's cache manager.
    The documents sit in the initial prompt, so a summarization rebuild keeps them.
    ``front_matter`` does not survive those rebuilds.
    """
    rest = prompt if isinstance(prompt, list) else [prompt]
    return [*context_documents(bundle), CacheMarker, *rest]


KB_INDEX_TEMPLATE = TypedTemplate[RecipeIndexParams]("kb_index.j2")

CVL_RECIPES = RecipeSet[CvlChannel](
    tool_name="get_cvl_recipe",
    index_resource="cvl_recipes_index.yaml",
    parse_index=lambda raw: IndexModel[CvlChannel].model_validate(raw),
    index_template=KB_INDEX_TEMPLATE,
)

CVL_BUNDLE = KnowledgeBundle[CvlChannel](
    label="CVL",
    recipes=CVL_RECIPES,
    specs=(
        ContextSpec(
            title="CVL Baseline Knowledge",
            loader=_resource_file_loader("cvl_baseline_facts.md"),
        ),
        ContextSpec(
            title="CVL Summarization and Linking Guide",
            loader=_resource_file_loader("cvl_summarization_rag_draft.md"),
        ),
        ContextSpec(
            title="Invariants and Quantifiers Guide",
            loader=_resource_file_loader("cvl_invariants_quantifiers.md"),
        ),
    ),
)


def with_cvl_context(prompt: "PromptInput") -> "PromptInput":
    return with_context(CVL_BUNDLE, prompt)


#: Each name is one of the author's tools. The channel says which tool can make the fix.
#: ``RULE`` is ``put_harness``. ``MOCK`` is ``summarize_for_prover``. ``EDIT`` is ``code_editor``.
#: ``CONF`` is ``adjust_prover_config``, and only for the loop bound and ``optimistic_loop``.
#: ``SKIP`` is ``record_skip``.
type CvlrChannel = Literal["RULE", "MOCK", "EDIT", "CONF", "SKIP"]

CVLR_RECIPES = RecipeSet[CvlrChannel](
    tool_name="get_cvlr_recipe",
    index_resource="cvlr_recipes_index.yaml",
    parse_index=lambda raw: IndexModel[CvlrChannel].model_validate(raw),
    index_template=KB_INDEX_TEMPLATE,
)

_CVLR_SPECS = (
    ContextSpec(
        title="CVLR and the Certora Solana Prover",
        loader=_resource_file_loader("cvlr_baseline_facts.md"),
    ),
)

#: For the author and the judge, which both have ``get_cvlr_recipe``. The legend names the author's
#: tools, and the judge uses it to see where a fix was made.
CVLR_BUNDLE = KnowledgeBundle[CvlrChannel](label="CVLR", recipes=CVLR_RECIPES, specs=_CVLR_SPECS)

#: The same facts, without the recipe index, for an agent that cannot call ``get_cvlr_recipe``.
#: The index tells its reader to fetch a recipe and names the author's tools.
CVLR_FACTS_BUNDLE = KnowledgeBundle[CvlrChannel](label="CVLR", specs=_CVLR_SPECS)


def with_cvlr_context(prompt: "PromptInput") -> "PromptInput":
    return with_context(CVLR_BUNDLE, prompt)


def with_cvlr_facts(prompt: "PromptInput") -> "PromptInput":
    return with_context(CVLR_FACTS_BUNDLE, prompt)
