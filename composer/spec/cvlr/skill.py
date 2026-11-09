"""The mechanical steps of the ``certora-autoprover-solana`` skill, for ``certora-cvlr``.

The skill (``docs/cvlr-skill-plan.md``) has the user's own coding agent write the properties and the
rules. What is mechanical, and quietly wrong when done by hand, comes from here: the same preflight,
scaffold and module declarations the pipeline runs, on the user's own working tree rather than a
run's copy.

Builds are unconfined unless ``COMPOSER_SANDBOX_PROVIDER`` names a provider. The pipeline confines
by default because it compiles a project nobody at the keyboard chose to build; here the user's
agent is already building their project with their own permissions.
"""

import asyncio
import fcntl
import fnmatch
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from composer.cargo.features import CargoFeature
from composer.cargo.manifest import ManifestEditor
from composer.cargo.sbf import Built
from composer.cargo.session import CompileVerdict
from composer.kb.kb_context import (
    CVLR_BUNDLE,
    CVLR_RECIPES,
    recipe_bodies,
    recipe_index_document,
)
from composer.prover.cloud import parse_cloud_link, poll_job, results_api
from composer.prover.conf import ExcludeRules, InheritRules, RuleSelection, SelectRules
from composer.prover.core import (
    ProverCallbacks,
    ProverOptions,
    ProverReport,
    SubmittedJob,
    collect_prover_results,
)
from composer.sandbox.config import SandboxConfig
from composer.spec.cvlr.author import render_author_system, render_judge_system
from composer.spec.cvlr.conf import (
    DEFAULT_FEATURE,
    CheckVerdicts,
    CollectUnsatCore,
    ConfPurpose,
    TunableConf,
    tunable_conf,
)
from composer.spec.cvlr.guidance import SOLANA_CVLR_GUIDANCE
from composer.spec.cvlr.harness import SHARED_INVARIANTS, HarnessModule, declare_modules
from composer.spec.cvlr.preflight import (
    CvlrPreflight,
    check_package,
    gate_workspace,
    prepare_workspace,
    sbf_session,
    select_package,
)
from composer.spec.cvlr.reference import generations
from composer.spec.cvlr.manifest import ManifestCorrupt, ManifestMissing, rule_names_in_elf
from composer.spec.cvlr.prover import (
    BuildRejected,
    Prepared,
    SubmissionFailed,
    Submission,
    build_for_submission,
    start_submission,
    write_submission,
)
from composer.spec.cvlr.scaffold import ENVS_DIR, HARNESS_DIR, SPECS_DIR, declare_unit_features
from composer.spec.cvlr.tuning import SUMMARIES, extend_composite, layer_patterns
from composer.spec.cvlr.vacuity import CoreFinding, core_finding
from composer.spec.cvlr.verify import inert_summaries_note
from composer.spec.source.report_prover import fetch_unsat_cores
from composer.spec.gen_types import CVLR_INTERNAL_DIR
from composer.spec.util import slugify_rust_module
from composer.templates.loader import load_jinja_template

#: Where :func:`write_reference` puts the reference text, relative to the project root. Under the
#: internal directory the scaffold already has git ignore.
REFERENCE_DIR = CVLR_INTERNAL_DIR / "reference"

#: The chain whose reference sets a skill project is checked against.
_CHAIN = "solana"

#: The unit that proves :data:`~composer.spec.cvlr.harness.SHARED_INVARIANTS`: its base cases and
#: steps. Not in the shared module, which every component's build compiles, so that a component's
#: job does not check them again.
INVARIANT_RULES = "invariant_rules"

#: Where a component's points-to summaries go, one file per module, in the format of
#: :class:`~composer.spec.cvlr.tuning.SummaryDirective`. Composed into the file the component's
#: conf names on every prove.
SUMMARY_LAYERS_DIR = HARNESS_DIR / "summaries"


class ComponentNameRejected(ValueError):
    """A component name the harness cannot use as a module name as given."""


def component_modules(names: Sequence[str]) -> list[HarnessModule]:
    """One harness module per component, named exactly as given.

    A name that is not already a module name is refused with the one it would become, rather than
    renamed: the agent writes the file, so it has to know the name."""
    problems: list[str] = []
    for name in names:
        if (slug := slugify_rust_module(name)) != name:
            problems.append(f"{name!r} is not a Rust module name; use {slug!r}")
        elif name in (SHARED_INVARIANTS, INVARIANT_RULES):
            problems.append(f"{name!r} is declared by setup itself; name the component otherwise")
    if dupes := sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"named more than once: {', '.join(dupes)}")
    if problems:
        raise ComponentNameRejected("; ".join(problems))
    return [HarnessModule(name) for name in names]


@dataclass(frozen=True)
class SetupResult:
    preflight: CvlrPreflight
    #: The package directory, absolute.
    package_root: Path
    modules: list[HarnessModule]
    #: Module files this setup created empty. An existing file is never touched.
    created: tuple[Path, ...]
    added_features: tuple[CargoFeature, ...]
    reference_dir: Path
    #: Whether the scaffolded package was compiled with the shared module in.
    checked: bool

    @property
    def specs_dir(self) -> Path:
        return self.package_root / SPECS_DIR


_STUBS = {
    SHARED_INVARIANTS: "//! Structural invariants, assumed by every component's rules.\n",
    INVARIANT_RULES: f"//! The base cases and steps that prove `{SHARED_INVARIANTS}`.\n",
}


def _create_absent(specs_dir: Path, modules: Sequence[HarnessModule]) -> tuple[Path, ...]:
    created: list[Path] = []
    for module in [SHARED_INVARIANTS, *(m.module for m in modules)]:
        path = specs_dir / f"{module}.rs"
        if not path.exists():
            path.write_text(_STUBS.get(module, f"//! The rules for the `{module}` component.\n"))
            created.append(path)
    return tuple(created)


async def setup(
    project_root: Path,
    *,
    package: str | None,
    components: Sequence[str],
    sandbox: SandboxConfig,
    check: bool = True,
) -> SetupResult:
    """Scaffold ``project_root`` for CVLR, declare the shared module and one module per component,
    write the reference text, and compile the result unless ``check`` is off.

    ``components`` replaces the modules an earlier setup declared; a module file already written is
    kept. Raises :class:`~composer.spec.cvlr.preflight.PreflightFailed` when the project is refused,
    and :class:`ComponentNameRejected` before anything is written."""
    modules = [HarnessModule(INVARIANT_RULES), *component_modules(components)]
    with tempfile.TemporaryDirectory(prefix="cvlr-select-") as select_home:
        selected = await select_package(
            project_root,
            package,
            candidates=generations(_CHAIN),
            cargo_home=Path(select_home) if sandbox.enabled else None,
        )
    pre = await prepare_workspace(
        project_root, reference=selected.reference, sandbox=sandbox, package=selected.name
    )
    package_root = pre.workspace_root / pre.package_dir
    specs_dir = package_root / SPECS_DIR
    declare_modules(specs_dir, modules, shared=(SHARED_INVARIANTS,))
    created = _create_absent(specs_dir, modules)
    added = declare_unit_features(package_root / "Cargo.toml", [m.feature for m in modules])
    reference_dir = project_root / REFERENCE_DIR
    write_reference(
        reference_dir,
        package=pre.package,
        cvlr_versions=", ".join(f"{c.name} {c.version}" for c in pre.sources.crates),
        caveats=selected.reference.caveats,
    )
    if check:
        await gate_workspace(pre, sandbox=sandbox)
    return SetupResult(
        preflight=pre,
        package_root=package_root,
        modules=modules,
        created=created,
        added_features=added,
        reference_dir=reference_dir,
        checked=check,
    )


def reference_documents(
    *, package: str, cvlr_versions: str, caveats: Sequence[str]
) -> dict[Path, str]:
    """The reference text for a project on ``cvlr_versions``, by path under the reference
    directory."""
    (facts,) = CVLR_BUNDLE.specs
    docs = {
        Path("README.md"): load_jinja_template(
            "cvlr/skill_reference.j2", package=package, cvlr_versions=cvlr_versions
        ),
        Path("cvlr.md"): facts.loader(),
        Path("properties.md"): SOLANA_CVLR_GUIDANCE,
        Path("author.md"): render_author_system(
            {
                "module": "<module>",
                "cvlr_versions": cvlr_versions,
                "caveats": list(caveats),
                "example": None,
                "conf": tunable_conf(TunableConf()),
            }
        ),
        Path("judge.md"): render_judge_system(
            {"cvlr_versions": cvlr_versions, "caveats": list(caveats)}
        ),
        Path("invariants.md"): load_jinja_template("cvlr_invariant_guidance.j2"),
        Path("vacuity.md"): load_jinja_template("cvlr_vacuity_system_prompt.j2")
        + "\n\n"
        + load_jinja_template("cvlr_vacuity_prompt.j2"),
        Path("recipes") / "index.md": recipe_index_document(CVLR_BUNDLE.label, CVLR_RECIPES),
    }
    docs.update(
        {Path("recipes") / f"{id}.md": text for id, text in recipe_bodies(CVLR_RECIPES).items()}
    )
    return docs


def write_reference(
    directory: Path, *, package: str, cvlr_versions: str, caveats: Sequence[str]
) -> None:
    """Replace ``directory`` with :func:`reference_documents`, so nothing from an older version
    is left beside them."""
    if directory.exists():
        shutil.rmtree(directory)
    documents = reference_documents(package=package, cvlr_versions=cvlr_versions, caveats=caveats)
    for relative, text in documents.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


# ---------------------------------------------------------------------------------------------
# proving


class NotSetUp(RuntimeError):
    """The component has no module or feature, so setup has not declared it."""


@dataclass(frozen=True)
class _Located:
    """A component setup declared, and the package it is in."""

    package: str
    workspace_root: Path
    #: Relative to :attr:`workspace_root`.
    package_dir: Path
    module: HarnessModule

    @property
    def package_root(self) -> Path:
        return self.workspace_root / self.package_dir


async def _locate(
    project_root: Path, package: str | None, component: str, sandbox: SandboxConfig
) -> _Located:
    with tempfile.TemporaryDirectory(prefix="cvlr-select-") as select_home:
        selected = await select_package(
            project_root,
            package,
            candidates=generations(_CHAIN),
            cargo_home=Path(select_home) if sandbox.enabled else None,
        )
    located = _Located(
        selected.name, selected.workspace_root, selected.package_dir, HarnessModule(component)
    )
    manifest = located.package_root / "Cargo.toml"
    if located.module.feature not in ManifestEditor.read(manifest).manifest.features or not (
        located.package_root / SPECS_DIR / located.module.artifact_file
    ).is_file():
        raise NotSetUp(
            f"{component!r} is not a declared component of {selected.name}; run "
            f"`certora-cvlr setup` naming it with --component"
        )
    return located


async def check(
    project_root: Path, component: str, *, package: str | None, sandbox: SandboxConfig
) -> CompileVerdict:
    """``cargo check`` one component's rules for the SBF target, without building them.

    Raises :class:`NotSetUp` and :class:`~composer.spec.cvlr.preflight.PreflightFailed`."""
    located = await _locate(project_root, package, component, sandbox)
    return await check_package(
        located.workspace_root,
        located.package_dir,
        sandbox=sandbox,
        features=(DEFAULT_FEATURE, located.module.feature),
    )


@dataclass(frozen=True)
class ProveRequest:
    component: str
    rules: RuleSelection = field(default_factory=InheritRules)
    settings: TunableConf = TunableConf()
    msg: str | None = None
    #: :class:`~composer.spec.cvlr.conf.CollectUnsatCore` reruns a rule reported vacuous with the
    #: vacuity check off, so that :func:`vacuity` can read the core of its proof.
    purpose: ConfPurpose = CheckVerdicts()


@dataclass(frozen=True)
class JobStarted:
    link: str
    #: The rules the job checks, as the build listed them.
    rules: tuple[str, ...]
    #: The summaries layer composed into this job's conf, when the component has one.
    summaries: Path | None
    #: Said about the build before the job ran: summary directives that match no function.
    inert_summaries: str | None = None


@dataclass(frozen=True)
class NoRules:
    """The build succeeded, but there was no rule to submit."""

    reason: str


type ProveOutcome = JobStarted | BuildRejected | NoRules | SubmissionFailed


async def prove(
    project_root: Path,
    request: ProveRequest,
    *,
    package: str | None,
    sandbox: SandboxConfig,
    prover_opts: ProverOptions,
    on_waiting: Callable[[], None] = lambda: None,
) -> ProveOutcome:
    """Build one component's rules and submit them, returning once the job is accepted.

    Another ``prove`` of the same workspace waits for this one to be accepted, since from the build
    until then nothing else may build the crate (:func:`~composer.spec.cvlr.prover.prepare_submission`).
    ``on_waiting`` is called when it has to. Raises :class:`NotSetUp` and
    :class:`~composer.spec.cvlr.preflight.PreflightFailed`."""
    located = await _locate(project_root, package, request.component, sandbox)
    module = located.module
    workspace_root = located.workspace_root
    package_root = located.package_root
    layer_path = package_root / SUMMARY_LAYERS_DIR / f"{module.module}.txt"
    layer = layer_path.read_text() if layer_path.is_file() else ""
    envs = package_root / ENVS_DIR
    composite = envs / SUMMARIES.unit_composite(module.module)
    composite.write_text(extend_composite((envs / SUMMARIES.composite).read_text(), layer))

    session = await sbf_session(workspace_root, sandbox=sandbox)
    submission = Submission(
        manifest_path=located.package_dir / "Cargo.toml",
        stem=module.stem
        + ("_unsat_core" if isinstance(request.purpose, CollectUnsatCore) else ""),
        msg=request.msg or f"{located.package}: {module.module}",
        purpose=request.purpose,
        settings=request.settings,
        rules=request.rules,
        features=(DEFAULT_FEATURE, module.feature),
        summaries=(composite.relative_to(workspace_root),),
    )
    lock_path = workspace_root / CVLR_INTERNAL_DIR / "build.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock, tempfile.TemporaryDirectory(prefix="cvlr-submit-") as into:
        await _lock(lock.fileno(), on_waiting)
        build = await build_for_submission(session, submission)
        if not isinstance(build.verdict, Built):
            return BuildRejected(build)
        try:
            listed = rule_names_in_elf(build.verdict.manifest.artifact)
        except ManifestMissing:
            return NoRules("The built program has no `.cvlr_rules` section, so it lists no rules.")
        except ManifestCorrupt:
            return NoRules("The built program's `.cvlr_rules` section could not be read.")
        rules = selected_rules(request.rules, listed)
        if not rules:
            return NoRules(
                f"The build of {module.module} lists no rules the selection names. It lists: "
                f"{', '.join(sorted(listed)) or 'none'}. A rule is a `#[rule]` function or a "
                "`cvlr_rules!` invocation."
            )
        chosen = replace(submission, rules=SelectRules(rules))
        conf = await write_submission(session, chosen, into=Path(into))
        started = await start_submission(
            Prepared(session, build, conf), prover_opts=prover_opts
        )
    if isinstance(started, SubmissionFailed):
        return started
    return JobStarted(
        link=started.link,
        rules=rules,
        summaries=layer_path if layer else None,
        inert_summaries=await inert_summaries_note(
            layer_patterns(layer), build.verdict.manifest.artifact
        ),
    )


def selected_rules(selection: RuleSelection, listed: set[str]) -> tuple[str, ...]:
    """The rules the build listed that ``selection`` names, matched as globs.

    The conf names its rules: one with no ``rule`` entry ends in a FAILED job with no report."""
    match selection:
        case InheritRules():
            chosen = listed
        case SelectRules(names=globs):
            chosen = {r for r in listed if any(fnmatch.fnmatchcase(r, g) for g in globs)}
        case ExcludeRules(names=globs):
            chosen = {r for r in listed if not any(fnmatch.fnmatchcase(r, g) for g in globs)}
    return tuple(sorted(chosen))


async def _lock(fd: int, on_waiting: Callable[[], None]) -> None:
    """Take the exclusive lock on ``fd``, released when the file is closed."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        on_waiting()
        await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX)


@dataclass(frozen=True)
class Running:
    """The job had not finished when the wait ran out."""

    status: str


type WaitOutcome = Running | ProverReport | str


async def wait(link: str, *, seconds: float, prover_opts: ProverOptions) -> WaitOutcome:
    """Wait up to ``seconds`` for the job at ``link``, then return its verdicts, or that it is still
    running. An error comes back as a string, as from :func:`~composer.prover.core.run_prover`."""
    last = "UNKNOWN"

    async def on_status(status: str) -> None:
        nonlocal last
        last = status

    try:
        await poll_job(parse_cloud_link(link), timeout=seconds, on_status=on_status)
    except TimeoutError:
        return Running(last)
    return await collect_prover_results(
        SubmittedJob(link=link, is_local_link=False),
        "cvlr-wait",
        prover_opts,
        ProverCallbacks(),
        None,
    )


@dataclass(frozen=True)
class CoreRead:
    """The unsat core of a rule's proof, from a job ``prove`` ran with ``CollectUnsatCore``."""

    finding: CoreFinding
    core: str


async def vacuity(link: str, rule: str) -> CoreRead | None:
    """The first unsat core the job at ``link`` wrote for ``rule``, or ``None`` when it wrote
    none: the job did not collect cores, or the rule did not verify with the vacuity check off."""
    cores = await asyncio.to_thread(fetch_unsat_cores, results_api(), link, rule)
    if not cores:
        return None
    return CoreRead(core_finding(cores[0]), cores[0])
