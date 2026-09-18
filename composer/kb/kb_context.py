from typing import Callable, Literal, Annotated, Mapping, Sequence, TypedDict, cast, TYPE_CHECKING
from functools import cache
from importlib.resources import files
from dataclasses import dataclass
from pydantic import BaseModel, Field, BeforeValidator
import yaml

from graphcore.graph import CacheMarker

from composer.templates.loader import load_jinja_template
from composer.spec.gen_types import TypedTemplate

if TYPE_CHECKING:
    from graphcore.graph import PromptInput

type _ContextLoader = Callable[[], str]

#: Where a CVL recipe's action lies, so an agent can tell whether the fix is in its action space.
type CvlChannel = Literal["CVL", "CONF", "EDIT"]


def _one_or_many(v: object) -> object:
    return [v] if isinstance(v, str) else v


class KBRecipe[C: str](BaseModel):
    """A recipe's index entry. Generic over its channel vocabulary because a channel names a tool
    the reading agent has, and two agent families do not have the same tools — a shared union would
    validate a recipe pointing at an action its readers cannot take."""

    id: str
    name: str
    triggers: list[str]
    file: str
    search_terms: list[str] = Field(default_factory=list)
    note: str | None = Field(default=None)
    channel: Annotated[list[C], BeforeValidator(_one_or_many)]


class IndexModel[C: str](BaseModel):
    #: What each channel means for the agent reading it. Rendered with the index: a channel tells a
    #: stuck agent whether the fix is in its action space, which it cannot do if only the index file
    #: says what the name means.
    channels: dict[C, str]
    recipes: list[KBRecipe[C]]


class RecipeIndexParams(TypedDict):
    #: Widened: the template only prints a channel, and the vocabulary was already enforced when
    #: the index was validated. Keeping it narrow here would make the params type generic, which
    #: the template fuzzer cannot resolve (``composer/meta/resolver.py``).
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
    """The on-demand half of a bundle: an index that goes in front of every agent, and bodies
    fetched by id. A family acquires one when it has recipes to serve; until then it has none, and
    that is the whole of the difference."""

    tool_name: str
    index_resource: str
    parse_index: Callable[[object], IndexModel[C]]
    index_template: TypedTemplate[RecipeIndexParams]


@dataclass(frozen=True)
class KnowledgeBundle[C: str]:
    """One agent family's practice knowledge: context documents that go in front of every agent,
    and optionally a recipe set.

    Frozen because the loaders below are memoized on the bundle, and every document is read once per
    process. That is also the bundle's defining constraint: it is a *cacheable prefix*, so nothing
    in it may vary per run. Per-run material belongs in the agent's own prompt.
    """

    #: Names the manual this family's knowledge sits beside, in the recipe index preamble.
    label: str
    specs: tuple[ContextSpec, ...]
    recipes: RecipeSet[C] | None = None


# Not memoized, unlike the public entry points below: ``functools.cache`` erases a generic return
# type, and every caller is itself cached, so this parses at most twice per recipe set.
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
    """The bundle's documents, the recipe index last. The index is appended rather than listed
    among ``specs`` because it is rendered from the bundle rather than read from a file."""
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
    """The bundle's context documents (cache marker on the trailing block; the TTL is whatever the
    builder's cache manager applies) followed by ``prompt`` — the standard initial-prompt shape for
    an agent in that family. Goes through the initial prompt rather than ``front_matter`` so the
    documents survive summarization rebuilds."""
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


#: Where a CVLR recipe's action lies. Taken from the author's tool list rather than from the shape
#: of the artifact, because a channel's job is to tell a stuck agent whether the fix is in its
#: action space: RULE is ``put_harness``, MOCK is ``summarize_for_prover``, EDIT is ``code_editor``,
#: CONF is ``adjust_prover_config`` (the loop bound and the solver portfolio, and nothing else), and
#: SKIP is ``record_skip`` — the honest terminal channel, which CVL has no peer for.
type CvlrChannel = Literal["RULE", "MOCK", "EDIT", "CONF", "SKIP"]

CVLR_RECIPES = RecipeSet[CvlrChannel](
    tool_name="get_cvlr_recipe",
    index_resource="cvlr_recipes_index.yaml",
    parse_index=lambda raw: IndexModel[CvlrChannel].model_validate(raw),
    index_template=KB_INDEX_TEMPLATE,
)

CVLR_BUNDLE = KnowledgeBundle[CvlrChannel](
    label="CVLR",
    recipes=CVLR_RECIPES,
    specs=(
        ContextSpec(
            title="CVLR and the Certora Solana Prover",
            loader=_resource_file_loader("cvlr_baseline_facts.md"),
        ),
    ),
)


def with_cvlr_context(prompt: "PromptInput") -> "PromptInput":
    return with_context(CVLR_BUNDLE, prompt)
