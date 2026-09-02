import enum
import logging
import os
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath
from typing import Mapping, NotRequired, Any, Callable, TypedDict

from graphcore.graph import MessagesState, FlowInput


from composer.spec.context import (
    WorkflowContext, SystemDoc, SourceCode
)
from composer.spec.gen_types import TypedTemplate
from composer.spec.graph_builder import bind_standard, run_to_completion
from composer.spec.system_model import (
    BaseApplication, ExistingFromSource, ExplicitContract, ExternalActor, ExternalDependency,
    SourceExplicitContract, SourceExternalActor,
)
from composer.spec.types import SourceIdentifier
from composer.spec.service_host import ServiceHost, Sort
from composer.spec.util import fs_forbidden_read, fs_withheld_subtree, slugify_filename
from composer.tools.thinking import RoughDraftState, get_rough_draft_tools
from composer.diagnostics.budget import budget_monitor

DESCRIPTION = "Component analysis"

_logger = logging.getLogger(__name__)


class AnalysisPromptParams(TypedDict):
    """Kwargs shared by the analysis agent's system and initial prompt templates."""
    sort: Sort
    has_doc: bool


#: The EVM/Solidity analysis prompts.
ANALYSIS_SYSTEM_TEMPLATE = TypedTemplate[AnalysisPromptParams]("application_analysis_system.j2")
ANALYSIS_INITIAL_TEMPLATE = TypedTemplate[AnalysisPromptParams]("application_analysis_prompt.j2")

#: How many same-named files a relocation hint names before it stops listing them.
_MAX_RELOCATION_CANDIDATES = 10

#: The paragraph that states the frame a declared path is read in. Appended to the reference
#: block rather than to each error, so a submission with several bad paths is told once.
_PATH_FRAME = (
    "\n\nPaths are relative to the project root — exactly the paths list_files prints and "
    "get_file accepts, carrying every leading directory those tools show."
)


class _PathFault(enum.Enum):
    """Why a declared source path is unusable. Each value reads as the tail of
    "... declares path X, which ..."."""

    OUTSIDE = "does not name a file under the project root."
    DIRECTORY = "is a directory, not a Solidity file."
    MISSING = "does not exist."
    WITHHELD = "is not readable through your file tools; name a file they hand back instead."


def _path_fault(root: Path, declared: str) -> _PathFault | None:
    """What is wrong with a declared source path, or ``None`` when it names a file under *root*
    that the agent's own file tools hand back.

    Containment is lexical — relative, and never stepping up out of the root. Resolving the path
    would follow symlinks, and a dependency tree symlinked in under ``lib/`` or ``node_modules/``
    holds real Solidity that ``fs_forbidden_read`` deliberately keeps readable, so the agent is
    entitled to name a file there and has no way to restate it as a non-symlinked path.

    Readability is the one gate: a path the agent's own tools would withhold is not accepted here
    either. A relocation hint searches the same surface, with the narrower reach of a walk that
    does not descend symlinked directories — matching those tools, which reach a symlinked file
    when asked for it directly but do not enumerate one.
    """
    parsed = PurePosixPath(declared)
    if parsed.is_absolute() or ".." in parsed.parts:
        return _PathFault.OUTSIDE
    candidate = root / declared
    if candidate.is_dir():
        return _PathFault.DIRECTORY
    if not candidate.is_file():
        return _PathFault.MISSING
    if fs_forbidden_read(PurePath(declared)):
        return _PathFault.WITHHELD
    return None


def _readable_files_named(root: Path, names: set[str]) -> dict[str, list[str]]:
    """Project-root-relative paths of every readable file whose base name is in *names*, from a
    single walk of the tree.

    One walk for all the wanted names rather than one per name: a project that sits a directory
    below the root drops the same leading component from every path it declares, so a whole
    submission's worth of distinct base names goes wrong together. Wholly withheld directories are
    pruned on the way down instead of filtered out of the results, which keeps a report directory
    or a ``.certora_internal`` tree off the walk entirely. This runs while the agent waits on its
    retry, so both matter.
    """
    found: dict[str, list[str]] = {name: [] for name in names}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = [d for d in dirnames if not fs_withheld_subtree(rel_dir / d)]
        for filename in filenames:
            if filename in found and not fs_forbidden_read(rel_dir / filename):
                found[filename].append((rel_dir / filename).as_posix())
    return {name: sorted(paths) for name, paths in found.items()}


def _relocate(declared: str, same_named: Mapping[str, list[str]]) -> str:
    """The half of a missing-path complaint that says where to look instead: the readable files
    carrying the declared file name, narrowed to those whose path *ends* in the declared path.
    A path that lost a leading directory tail-matches exactly one real file, which turns the
    complaint into an answer the agent can copy."""
    name = PurePosixPath(declared).name
    candidates = same_named[name]
    tail_matches = [c for c in candidates if c.endswith("/" + declared)]
    if tail_matches:
        candidates = tail_matches
    if not candidates:
        return (
            f" No file named {name!r} is readable anywhere in the project; locate the definition "
            f"with grep_files, or drop this component if it has no file in the source tree."
        )
    if len(candidates) == 1:
        return f" The one file with that name is {candidates[0]!r}."
    shown = candidates[:_MAX_RELOCATION_CANDIDATES]
    more = "" if len(shown) == len(candidates) else f" (and {len(candidates) - len(shown)} more)"
    return (
        f" Files with that name: {', '.join(repr(c) for c in shown)}{more}. Read them and "
        f"resubmit the one that defines what you described."
    )


@dataclass(frozen=True)
class _PathComplaint:
    """One declared path the source tree does not back. Held unrendered until the pass over
    components is done, because the relocation hints for every complaint come out of one walk."""

    #: Names the component the path belongs to, e.g. "Contract Vault".
    subject: str
    declared: str
    fault: _PathFault
    #: What this kind of component may do instead of naming a file; empty where it has no way out.
    remedy: str = ""

    def render(self, same_named: Mapping[str, list[str]]) -> str:
        hint = _relocate(self.declared, same_named) if self.fault is _PathFault.MISSING else ""
        return (
            f"{self.subject} declares path {self.declared!r}, which "
            f"{self.fault.value}{hint}{self.remedy}"
        )


def _path_complaints(app: BaseApplication, project_root: Path) -> list[str]:
    """Every declared source path in *app* that names no readable file under *project_root*,
    worded for the agent's retry."""
    complaints: list[_PathComplaint] = []
    for c in app.components:
        if isinstance(c, SourceExplicitContract | ExistingFromSource):
            fault = _path_fault(project_root, c.path)
            if fault is not None:
                complaints.append(
                    _PathComplaint(f"Contract {c.solidity_identifier}", c.path, fault)
                )
        elif isinstance(c, SourceExternalActor) and c.path is not None:
            fault = _path_fault(project_root, c.path)
            if fault is not None:
                complaints.append(_PathComplaint(
                    f"External actor {c.name}", c.path, fault,
                    remedy=" If this actor has no interface file in the source tree, omit the path"
                           " instead.",
                ))
    wanted = {
        PurePosixPath(complaint.declared).name
        for complaint in complaints if complaint.fault is _PathFault.MISSING
    }
    same_named = _readable_files_named(project_root, wanted) if wanted else {}
    return [complaint.render(same_named) for complaint in complaints]


def validate_solidity_connectivity(
    app: BaseApplication, expected_main_id: SourceIdentifier | None, project_root: Path | None
) -> str | None:
    """Connectivity/shape validation for the Solidity model *family*: typed over
    ``BaseApplication`` because it checks only the contract/actor/interaction graph that
    ``Application``, ``SourceApplication``, ``HarnessedApplication``, and
    ``FromSourceApplication`` all share. Both callers name it directly; neither can use the
    other's ``Ecosystem.validate_analysis``, which is narrowed to one ``system_model``.

    The source-carrying subtypes additionally declare project-root-relative paths, and
    ``project_root`` is the tree those are required to name a file in; ``None`` means the run has
    no source tree, where no component declares a path in the first place. Path complaints join
    the same accumulated message as the graph ones, so a submission wrong in both ways is
    corrected in a single retry."""
    # The path complaints lead: they are gathered in one go so the tree behind the relocation
    # hints is walked once, and whether there are any decides if the reference block below
    # carries the path frame.
    path_errors: list[str] = [] if project_root is None else _path_complaints(app, project_root)
    errors: list[str] = list(path_errors)
    known_components: dict[str, set[str]] = {}
    known_external: set[str] = set()
    known_solidity_ids : set[str] = set()

    for c in app.components:
        if isinstance(c, ExplicitContract):
            if c.solidity_identifier in known_solidity_ids:
                errors.append(f"Duplicate solidity identifier: {c.solidity_identifier}")
            else:
                known_solidity_ids.add(c.solidity_identifier)
            if c.name in known_components:
                errors.append(f"Duplicate contract names: {c.name}")
            else:
                known_components[c.name] = set()
            slug_origin: dict[str, str] = {}
            for sub_comp in c.components:
                if sub_comp.name in known_components[c.name]:
                    errors.append(f"Duplicate component names in {c.name}: {sub_comp.name}")
                known_components[c.name].add(sub_comp.name)
                slug = slugify_filename(sub_comp.name)
                if slug in slug_origin:
                    errors.append(
                        f"Components {slug_origin[slug]!r} and {sub_comp.name!r} in {c.name} "
                        f"both reduce to the filename slug {slug!r} (punctuation and symbols are "
                        f"normalized to underscores); give them names that differ in more than that."
                    )
                else:
                    slug_origin[slug] = sub_comp.name
        else:
            assert isinstance(c, ExternalActor)
            if c.name in known_external:
                errors.append(f"Duplicate external component name: {c.name}")
            known_external.add(c.name)

    if expected_main_id is not None and expected_main_id not in known_solidity_ids:
        errors.append(f"Expected an explicit contract instance with solidity identifier: {expected_main_id}")

    for explicit in app.components:
        if not isinstance(explicit, ExplicitContract):
            continue
        for sub_comp in explicit.components:
            thing_interacts_with_str = f"Component {sub_comp.name} of {explicit.name} interacts with"
            for interaction in sub_comp.interactions:
                if isinstance(interaction, ExternalDependency):
                    if interaction.external_actor not in known_external:
                        errors.append(f"{thing_interacts_with_str} unknown external actor: {interaction.external_actor}")
                else:
                    if interaction.contract_name not in known_components:
                        errors.append(f"{thing_interacts_with_str} an unknown explicit contact: {interaction.contract_name}")
                    elif interaction.component and interaction.component not in known_components[interaction.contract_name]:
                        errors.append(f"{thing_interacts_with_str} unknown component {interaction.component} of explicit contract {interaction.contract_name}")

    if not errors:
        return None

    def _fmt(items: set[str]) -> str:
        return ", ".join(sorted(items)) if items else "(none)"

    reference_lines = [
        f"- Declared contracts: {_fmt(set(known_components))}",
        f"- Declared external actors: {_fmt(known_external)}",
    ]
    for contract_name, subs in sorted(known_components.items()):
        reference_lines.append(f"- Components of {contract_name}: {_fmt(subs)}")
    reference = "\n\nFor reference, the names you declared in your submission:\n" + "\n".join(reference_lines)
    if path_errors:
        reference += _PATH_FRAME

    if len(errors) == 1:
        return errors[0] + reference
    return "Multiple validation errors found; fix all of them before resubmitting:\n" + "\n".join(f"- {e}" for e in errors) + reference

async def run_component_analysis[T: BaseApplication](
    ty: type[T],
    child_ctxt: WorkflowContext[T],
    input: SystemDoc | SourceCode | None,
    env: ServiceHost,
    extra_input: list[str | dict],
    expected_main_id: SourceIdentifier | None = None,
    *,
    project_root: Path | None,
    system_template: TypedTemplate[AnalysisPromptParams],
    initial_template: TypedTemplate[AnalysisPromptParams],
    validate: Callable[[T, SourceIdentifier | None, Path | None], str | None],
) -> T | None:
    """Analyze application components from a system doc and optionally source code.

    ``input`` may be a bare ``SystemDoc`` (natspec mode), a ``SourceCode`` (source
    mode — whose ``content`` may be ``None`` for a source-only run), or ``None``.
    ``has_doc`` below reflects whether a design document is actually present, not
    merely whether an ``input`` object was passed, so a source-only run renders the
    no-doc prompt branch and never dereferences a missing ``content``.

    ``project_root`` is the tree the validator resolves any source paths the model declares
    against; it is required (not defaulted) so a new call site has to say what the model's paths
    mean, and is ``None`` only when the run has no source tree.

    The cache is the other way into an analyzed model, so a hit is held to the same validation a
    freshly generated model is. Its key covers the project and the contract, nothing about the
    source tree or the checks in force, so an entry outlives any rule added after it was written.
    An entry that fails validation is a stale entry: re-deriving it is what replaces it, through
    the ``cache_put`` at the end of the generation path.
    """

    def _check(app: T) -> str | None:
        return validate(app, expected_main_id, project_root)

    if (cached := await child_ctxt.cache_get(ty)) is not None:
        stale = _check(cached)
        if stale is None:
            return cached
        _logger.info("Re-deriving the cached component analysis, which fails validation: %s", stale)

    has_doc = input is not None and input.content is not None
    # greenfield has no source tools, so a design doc is mandatory there.
    assert has_doc or env.sort != "greenfield"

    memory = child_ctxt.get_memory_tool()

    class AnalysisInput(RoughDraftState, FlowInput):
        pass

    AnalysisState = type("AnalysisState", (MessagesState, RoughDraftState), {
        "__annotations__": {"result": NotRequired[ty]}
    })

    def _validation_wrapper(
        _: Any, app: T
    ) -> str | None:
        return _check(app)

    prompt_params: AnalysisPromptParams = {"sort": env.sort, "has_doc": has_doc}
    b = bind_standard(
        builder=env.builder_lite(),
        state_type=AnalysisState,
        validator=_validation_wrapper
    ).with_input(
        AnalysisInput
    ).inject(
        lambda g: system_template.bind(prompt_params).render_to(g.with_sys_prompt_template)
    ).with_tools(
        [memory, *get_rough_draft_tools(AnalysisState), *env.analysis_tools]
    ).inject(
        lambda g: initial_template.bind(prompt_params).render_to(g.with_initial_prompt_template)
    ).with_monitor(budget_monitor())


    graph = b.compile_async()
    inputs : list[str | dict] = []
    if has_doc:
        assert input is not None
        doc = input.content
        assert doc is not None
        inputs.extend([
            "The system document is as follows",
            doc.to_dict()
        ])
    inputs.extend(extra_input)

    flow_input = AnalysisInput(input=inputs, did_read=False, memory=None)

    res = await run_to_completion(
        graph,
        flow_input,
        thread_id=child_ctxt.thread_id,
        recursion_limit=child_ctxt.recursion_limit,
        description=DESCRIPTION,
    )
    assert "result" in res
    result: T = res["result"] #type: ignore trust me bro

    await child_ctxt.cache_put(result)
    return result
