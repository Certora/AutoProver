from typing import (
    Any, Callable, Collection, Literal, Annotated, Mapping, Sequence, TypedDict, cast, TYPE_CHECKING
)
from functools import cache
from importlib.resources import files
from dataclasses import dataclass
from pydantic import BaseModel, ConfigDict, Field, BeforeValidator
from pydantic.dataclasses import dataclass as pydantic_dataclass
import yaml

from graphcore.graph import CacheMarker

from composer.prover.ptypes import AnalysisErrorKind, GeneratedKind
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


class IndexModel[C: str, R: KBRecipe[Any]](BaseModel):
    #: What each channel means for the agent reading it. Rendered with the index: a channel tells a
    #: stuck agent whether the fix is in its action space, which it cannot do if only the index file
    #: says what the name means.
    channels: dict[C, str]
    recipes: list[R]


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
class RecipeSet[C: str, R: KBRecipe[Any]]:
    """The on-demand half of a bundle: an index that goes in front of every agent, and bodies
    fetched by id. A family acquires one when it has recipes to serve; until then it has none, and
    that is the whole of the difference."""

    tool_name: str
    index_resource: str
    parse_index: Callable[[object], IndexModel[C, R]]
    index_template: TypedTemplate[RecipeIndexParams]


@dataclass(frozen=True)
class KnowledgeBundle[C: str, R: KBRecipe[Any]]:
    """One agent family's practice knowledge: context documents that go in front of every agent,
    and optionally a recipe set.

    Frozen because the loaders below are memoized on the bundle, and every document is read once per
    process. That is also the bundle's defining constraint: it is a *cacheable prefix*, so nothing
    in it may vary per run. Per-run material belongs in the agent's own prompt.
    """

    #: Names the manual this family's knowledge sits beside, in the recipe index preamble.
    label: str
    specs: tuple[ContextSpec, ...]
    recipes: RecipeSet[C, R] | None = None


# Not memoized, unlike the public entry points below: ``functools.cache`` erases a generic return
# type, and every caller is itself cached, so this parses at most twice per recipe set.
def _kb_model[C: str, R: KBRecipe[Any]](recipes: RecipeSet[C, R]) -> IndexModel[C, R]:
    index_text = (files() / "resources" / recipes.index_resource).read_text()
    return recipes.parse_index(yaml.safe_load(index_text))


def _kb_index[C: str, R: KBRecipe[Any]](recipes: RecipeSet[C, R]) -> Mapping[str, str]:
    return {r.id: r.file for r in _kb_model(recipes).recipes}


@cache
def kb_loader[C: str, R: KBRecipe[Any]](recipes: RecipeSet[C, R]) -> Callable[[str], str | None]:
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


def recipe_index_document[C: str, R: KBRecipe[Any]](label: str, recipes: RecipeSet[C, R]) -> str:
    model = _kb_model(recipes)
    params: RecipeIndexParams = {
        "kb_retrieval_name": recipes.tool_name,
        "recipes": cast(Sequence[KBRecipe[str]], model.recipes),
        "channels": cast(Mapping[str, str], model.channels),
        "label": label,
    }
    return recipes.index_template.bind(params).render_to(load_jinja_template)


def recipe_bodies[C: str, R: KBRecipe[Any]](recipes: RecipeSet[C, R]) -> dict[str, str]:
    """Every recipe's body, keyed by its retrieval id, for a reader outside AutoProver."""
    kb_resource_dir = files() / "resources"
    return {r.id: (kb_resource_dir / r.file).read_text() for r in _kb_model(recipes).recipes}


@cache
def context_documents[C: str, R: KBRecipe[Any]](bundle: KnowledgeBundle[C, R]) -> tuple[str, ...]:
    """The bundle's documents, the recipe index last. The index is appended rather than listed
    among ``specs`` because it is rendered from the bundle rather than read from a file."""
    titled = [(s.title, s.loader()) for s in bundle.specs]
    if bundle.recipes is not None:
        titled.append(
            (f"{bundle.label} Recipes", recipe_index_document(bundle.label, bundle.recipes))
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


def with_context[C: str, R: KBRecipe[Any]](
    bundle: KnowledgeBundle[C, R], prompt: "PromptInput"
) -> "PromptInput":
    """The bundle's context documents (cache marker on the trailing block; the TTL is whatever the
    builder's cache manager applies) followed by ``prompt`` — the standard initial-prompt shape for
    an agent in that family. Goes through the initial prompt rather than ``front_matter`` so the
    documents survive summarization rebuilds."""
    rest = prompt if isinstance(prompt, list) else [prompt]
    return [*context_documents(bundle), CacheMarker, *rest]


KB_INDEX_TEMPLATE = TypedTemplate[RecipeIndexParams]("kb_index.j2")

CVL_RECIPES = RecipeSet[CvlChannel, KBRecipe[CvlChannel]](
    tool_name="get_cvl_recipe",
    index_resource="cvl_recipes_index.yaml",
    parse_index=lambda raw: IndexModel[CvlChannel, KBRecipe[CvlChannel]].model_validate(raw),
    index_template=KB_INDEX_TEMPLATE,
)

CVL_BUNDLE = KnowledgeBundle[CvlChannel, KBRecipe[CvlChannel]](
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
#: CONF is ``adjust_prover_config`` (the loop bound and ``optimistic_loop``, and nothing else), and
#: SKIP is ``record_skip`` — the honest terminal channel, which CVL has no peer for.
type CvlrChannel = Literal["RULE", "MOCK", "EDIT", "CONF", "SKIP"]


_SYMPTOM = ConfigDict(extra="forbid")


@pydantic_dataclass(frozen=True, config=_SYMPTOM)
class GeneratedSymptom:
    """A rule stopped on an assertion the Prover generated, of this kind."""

    generated: GeneratedKind


@pydantic_dataclass(frozen=True, config=_SYMPTOM)
class ErrorSymptom:
    """A rule ended in an error with this code, written as the number (``error: 3308``)."""

    error: AnalysisErrorKind


@pydantic_dataclass(frozen=True, config=_SYMPTOM)
class StatusSymptom:
    """A rule came back with this status.

    ``TIMEOUT`` is a rule that ran out of its own time, in a job that did not halt. A halted job
    cuts its unfinished rules off as ``TIMEOUT`` too, and those are :class:`JobSymptom`'s.
    """

    status: Literal["SANITY_FAILED", "TIMEOUT"]


@pydantic_dataclass(frozen=True, config=_SYMPTOM)
class TimeoutSymptom:
    """A rule timed out before reaching the solver, or with one function holding most of it."""

    timeout: Literal["static analysis", "dominant function"]


@pydantic_dataclass(frozen=True, config=_SYMPTOM)
class JobSymptom:
    """The job as a whole ended this way."""

    job: Literal["halted"]


#: What ``verify_rules`` can recognise in a result, and so what a recipe can be pushed on. A value
#: is equal to the index entry that names it, which is how a result finds its recipes.
type Symptom = GeneratedSymptom | ErrorSymptom | StatusSymptom | TimeoutSymptom | JobSymptom


class CvlrRecipe(KBRecipe[CvlrChannel]):
    #: The symptoms that make ``verify_rules`` attach this recipe's body to its result, so the
    #: author gets it without having to recognise the situation.
    pushed_on: list[Symptom] = Field(default_factory=list)


CVLR_RECIPES = RecipeSet[CvlrChannel, CvlrRecipe](
    tool_name="get_cvlr_recipe",
    index_resource="cvlr_recipes_index.yaml",
    parse_index=lambda raw: IndexModel[CvlrChannel, CvlrRecipe].model_validate(raw),
    index_template=KB_INDEX_TEMPLATE,
)

_CVLR_SPECS = (
    ContextSpec(
        title="CVLR and the Certora Solana Prover",
        loader=_resource_file_loader("cvlr_baseline_facts.md"),
    ),
)

#: For the agents that hold ``get_cvlr_recipe``: the author and the judge.
CVLR_BUNDLE = KnowledgeBundle[CvlrChannel, CvlrRecipe](label="CVLR", recipes=CVLR_RECIPES, specs=_CVLR_SPECS)

#: The same facts without the recipe index, for a reader that cannot retrieve a recipe. The index
#: says "retrieve the pointed file before acting", and its channels name the author's tools, so for
#: any other reader it is an instruction it cannot follow about tools it does not have.
CVLR_FACTS_BUNDLE = KnowledgeBundle[CvlrChannel, CvlrRecipe](label="CVLR", specs=_CVLR_SPECS)


def pushed_recipes(symptoms: Collection[Symptom]) -> list[str]:
    """The bodies of the CVLR recipes pushed on any of ``symptoms``, each once, in index order."""
    kb_resource_dir = files() / "resources"
    return [
        (kb_resource_dir / r.file).read_text()
        for r in _kb_model(CVLR_RECIPES).recipes
        if any(s in symptoms for s in r.pushed_on)
    ]


def with_cvlr_context(prompt: "PromptInput") -> "PromptInput":
    return with_context(CVLR_BUNDLE, prompt)


def with_cvlr_facts(prompt: "PromptInput") -> "PromptInput":
    return with_context(CVLR_FACTS_BUNDLE, prompt)
