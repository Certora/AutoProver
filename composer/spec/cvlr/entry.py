"""CLI entry-point wiring for the CVLR rule author (Solana, via the Certora Solana Prover).

Mirrors ``composer/foundry/entry.py``'s shape — parse args → stage services through
:func:`composer.pipeline.cli.cli_pipeline` → yield a closure a frontend drives with a handler
factory — and differs from it in the four places a Rust/Solana run differs from an EVM one:

* **The source surface is Cargo's, not Foundry's.** ``SOLANA.language.default_forbidden_read``
  withholds ``target/``, which on a project that has been built once is larger than everything else
  in the tree put together.
* **The package is resolved before the run starts** (:func:`select_package`), because the artifact
  store has to be pointed at the crate the harness lands in, and that is not known from the project
  root alone. Resolving it here also turns "which of these five programs?" into a usage error
  instead of a preflight failure two agents in.
* **Confinement defaults to on.** ``docs/cvlr-backend-plan.md`` §3 item 3: a cargo build runs the
  project's own ``build.rs`` and proc-macros, so production is confined and only an explicit
  ``COMPOSER_SANDBOX_PROVIDER=none`` opts out — never an automatic degrade. An unconfined run says
  so on stderr, because a result produced without confinement must not be mistaken for a
  production one.
* **The prover is cloud-only** (§3 item 1), so there is no ``--local`` to offer.

The corpus is wired here and was not in the expensive gate: ``build_rag_tools`` degrades to no RAG
when the database is not up, which is the documented behaviour for a search aid — so naming it
costs a developer without one nothing, and a developer with one gets §9's other defense against
inventing CVLR helpers.
"""

import argparse
import logging
import os
import pathlib
import re
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable, Protocol, cast

from graphcore.tools.vfs import DirBackend, GlobalExcludeArg

from composer.cargo.sbf import PLATFORM_TOOLS_ROOT
from composer.core.user import get_uid
from composer.diagnostics.timing import RunSummary
from composer.input.parsing import add_extra_context_args, add_protocol_args
from composer.input.types import DEFAULT_RECURSION_LIMIT, ExtendedModelOptions
from composer.io.multi_job import HandlerFactory
from composer.io.thread_logging import RunDataLogger
from composer.pipeline.cli import AtExit, cli_pipeline, user_ns
from composer.pipeline.ecosystem import SOLANA
from composer.pipeline.ptypes import DEFAULT_MAX_CPU_TASKS, CorePipelineResult
from composer.prover.core import make_prover_options
from composer.rag.db import KNOWLEDGE_BASES
from composer.sandbox.config import SandboxConfig
from composer.spec.context import SourceFields
from composer.spec.cvlr.harness import CvlrArtifactStore, GeneratedHarness
from composer.spec.cvlr.pipeline import BUILD_DIR, WORK_DIR, CvlrBackend, CvlrPhase
from composer.spec.cvlr.preflight import SelectedPackage, select_package
from composer.spec.service_host import PureServiceHost
from composer.spec.source.cex_capture import CexAnalysisStore
from composer.spec.source.source_env import build_layered_source_tools, build_source_tools
from composer.tools.rag_env import build_rag_tools

_log = logging.getLogger(__name__)

#: The corpus this backend searches — the tag, not a connection string, since
#: ``composer.rag.db.KNOWLEDGE_BASES`` is where a tag's connection lives and the importer targets
#: the same name. ``none`` disables the search tools.
DEFAULT_CORPUS = "cvlr_kb"

#: The result a frontend renders.
type CvlrPipelineResult = CorePipelineResult[GeneratedHarness]

type CvlrRunner = Callable[
    [HandlerFactory[CvlrPhase, None]], Awaitable[CvlrPipelineResult]
]


# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------


class CvlrArgs(ExtendedModelOptions, Protocol):
    project_root: str
    main_contract: str
    system_doc: str | None
    package: str | None
    rag_corpus: str
    max_concurrent: int
    max_cpu_tasks: int
    cache_ns: str | None
    memory_ns: str | None
    interactive: bool
    max_bug_rounds: int
    max_properties: int | None
    recursion_limit: int
    budget: str | None
    time_budget: float | None
    extra_context: list[str] | None
    threat_model: str | None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CVLR rule author for the Certora Solana Prover",
    )
    add_protocol_args(parser, ExtendedModelOptions)
    parser.add_argument(
        "--recursion-limit", type=int, default=DEFAULT_RECURSION_LIMIT,
        help=f"Max graph iterations (default: {DEFAULT_RECURSION_LIMIT})",
    )
    parser.add_argument("project_root", help="Cargo workspace root (contains the root Cargo.toml).")
    parser.add_argument(
        "main_contract",
        metavar="main_program",
        help="Main program as path:identifier, e.g. programs/vault/src/lib.rs:vault. The "
             "identifier is the program the analysis must name; the path decides which crate the "
             "harness is written into unless --package overrides it.",
    )
    parser.add_argument(
        "system_doc", nargs="?", default=None,
        help="Path to the design document (text or PDF). Optional — auto-discovered from the "
             "project when omitted.",
    )
    parser.add_argument(
        "--package", default=None,
        help="Cargo package to verify. Defaults to the crate that owns the main program's source "
             "file; name it when that is not the crate to build.",
    )
    parser.add_argument(
        "--rag-corpus", default=DEFAULT_CORPUS,
        choices=(*sorted(KNOWLEDGE_BASES), "none"),
        help=f"CVLR knowledge corpus to search, or none (default: {DEFAULT_CORPUS}). A corpus that "
             "is not installed degrades to no search tools rather than failing the run.",
    )
    parser.add_argument("--max-concurrent", type=int, default=4, help="Max concurrent agents (default: 4)")
    parser.add_argument(
        "--max-cpu-tasks", type=int, default=DEFAULT_MAX_CPU_TASKS,
        help=f"Max concurrent CPU-bound tasks — cargo builds and the like (default: "
             f"{DEFAULT_MAX_CPU_TASKS}). Cargo builds are additionally serialized behind one "
             f"permit, since every unit shares one target directory.",
    )
    parser.add_argument("--cache-ns", default=None, help="Cache namespace (enables cross-run caching)")
    parser.add_argument("--memory-ns", default=None, help="Memory namespace (default: thread id)")
    parser.add_argument("--interactive", action="store_true", help="Interactively refine extracted properties")
    parser.add_argument(
        "--threat-model", type=str, default=None,
        help="Path to a threat model (text or PDF) to seed property extraction with. Worth having "
             "on a real engagement: the front half is the ecosystem's and reads it the same way "
             "the EVM pipeline does.",
    )
    parser.add_argument("--max-bug-rounds", type=int, default=3, help="Max bug-extraction rounds per component (default: 3)")
    parser.add_argument(
        "--max-properties", type=int, default=None,
        help="Author rules for at most this many extracted properties, taken in order across "
             "components. Omit for all of them. Bounds what the run takes on, where --budget bounds "
             "what it spends — the two are worth pairing on a program this backend has not seen.",
    )
    parser.add_argument("--budget", default=None, help="Path to a run-budget file (JSON or YAML): {total: USD, caps: {phase: USD, ...}}. Omit to run unbudgeted.")
    parser.add_argument("--time-budget", default=None, type=float, help="Total wall time to run the entire execution. Omit to run without in process limit")
    add_extra_context_args(parser)
    return parser


#: The working tree, withheld from the agent's own listing. Every source file in it is a copy of
#: one the project layer already serves at the same relative path, so listing it doubles the tree
#: and offers a second, *derived* spelling of every file — and reading the wrong one of a pair is
#: quieter than reading nothing. The tree still reaches the agent, as the layer above the project;
#: what is withheld is the path *through* ``.cvlr_work``, not the content.
_WORK_TREE_EXCLUDE = rf"(^{re.escape(WORK_DIR.as_posix())}/.*)"


def _source_surface(ecosystem_default: GlobalExcludeArg) -> GlobalExcludeArg:
    """The ecosystem's file-exclusion rule, plus this backend's own working tree.

    Composed here rather than in ``RUST_FORBIDDEN_READ`` because the tree is this backend's, not
    the Rust language facet's: Soroban will have one too and a wheel-based Rust backend need not.
    """
    if not isinstance(ecosystem_default, str):
        raise TypeError(
            f"the Rust source-exclusion rule is expected to be a regex to compose with, got "
            f"{type(ecosystem_default).__name__}"
        )
    return f"{ecosystem_default}|{_WORK_TREE_EXCLUDE}"


# ---------------------------------------------------------------------------
# The main program
# ---------------------------------------------------------------------------


#: What a Solana program can be named. Spelled out because of the specific mistake it catches: a
#: *cargo package* name passed where a program identifier belongs. Cargo permits hyphens and Rust
#: does not, so ``spl-stake-pool`` is a perfectly good package and an impossible program.
_RUST_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def parse_main_program(main_contract: str) -> tuple[str, str]:
    """Split ``path:identifier``, refusing an identifier no Solana program could have.

    The identifier is not a label this run picks for its own convenience. It is an *instruction*:
    ``_solana_analysis_extra_input`` orders the analysis to declare a program by exactly this name,
    ``_solana_validate`` rejects any submission that does not, and the rejection goes back to the
    model as feedback. So an impossible identifier does not surface as a bad argument — it surfaces
    as an analysis that can never validate, re-submitting a correct model against a name it is
    forbidden to use, until the budget stops it.

    Measured once, which is why this exists: ``spl-stake-pool`` cost 38 minutes and roughly thirty
    heavy-tier calls, and read from the outside like a slow model on a large program. The check that
    would have turned it into an immediate usage error is this regex.
    """
    path, colon, identifier = main_contract.partition(":")
    if not colon or not path:
        raise ValueError(
            f"{main_contract!r} is not a main program: expected <path>:<identifier>, e.g. "
            f"programs/vault/src/lib.rs:vault"
        )
    if _RUST_IDENTIFIER.match(identifier):
        return path, identifier
    corrected = identifier.replace("-", "_")
    hint = (
        f" Did you mean {path}:{corrected}? That is the crate name, which is what the program is "
        f"called in Rust."
        if _RUST_IDENTIFIER.match(corrected)
        else ""
    )
    raise ValueError(
        f"{identifier!r} cannot name a Solana program: it is not a Rust identifier, and the "
        f"analysis is required to declare a program by exactly this name.{hint}"
    )


# ---------------------------------------------------------------------------
# Confinement
# ---------------------------------------------------------------------------


def build_confinement() -> SandboxConfig:
    """The command sandbox a cargo build runs under.

    Fail-closed by default (``docs/cvlr-backend-plan.md`` §3 item 3): the provider is ``launcher``
    unless a developer sets ``COMPOSER_SANDBOX_PROVIDER``, and an unavailable launcher is an error
    rather than a silent degrade to passthrough. That is the opposite of
    :meth:`SandboxConfig.from_env`'s default, which is ``none`` — appropriate for a library seam
    with many callers, wrong for the one that compiles somebody else's ``build.rs``.

    The platform-tools root is granted read-only because ``cargo build-sbf`` reads its toolchain
    from there. :func:`composer.sandbox.recipes.rust_build_policy` already grants the two default
    locations; this adds whichever one ``$CERTORA_PLATFORM_TOOLS_ROOT`` names when it is set
    elsewhere, and is a no-op when it is not (a non-existent or duplicate grant is dropped).
    """
    return SandboxConfig(
        provider=os.environ.get("COMPOSER_SANDBOX_PROVIDER", "launcher"),
        extra_ro=(PLATFORM_TOOLS_ROOT,),
    )


def announce_confinement(sandbox: SandboxConfig) -> None:
    """Say on stderr when a run is unconfined, so its results are never read as production ones."""
    if not sandbox.enabled:
        _log.warning(
            "cvlr: builds are UNCONFINED (COMPOSER_SANDBOX_PROVIDER=none) — this project's "
            "build.rs and proc-macros run with your full environment, and results from this run "
            "are development results, not production ones"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _usage_exit_logger(summary: RunSummary, selected: SelectedPackage) -> AtExit:
    """Persist the run's LLM usage, and dump ``job_info.json`` beside the report whether or not the
    pipeline got as far as writing one — the foundry entry point's hook, with the package directory
    the CVLR store needs. Each step is guarded so a teardown failure cannot mask the run's own
    outcome."""
    async def exit_logger(run: SourceFields, logger: RunDataLogger) -> None:
        try:
            await logger("token_usage", summary.token_usage_summary())
            # A CVLR run's dominant cost is prover time, not tokens — logged for the same reason
            # the autoprove entry point logs it, and absent here for as long as CVLR's callbacks
            # were not recording it (§7.8.1).
            await logger("prover_usage", summary.prover_usage_summary())
        except Exception:
            _log.exception("failed to log cvlr usage to run data")
        try:
            CvlrArtifactStore(run.project_root, selected.package_dir).write_job_info(
                summary, user_id=get_uid()
            )
        except Exception:
            _log.exception("failed to dump cvlr job info")
    return exit_logger


@asynccontextmanager
async def _entry_point(summary: RunSummary) -> AsyncIterator[CvlrRunner]:
    args = cast(CvlrArgs, build_parser().parse_args())
    async with cvlr_executor(args, summary) as runner:
        yield runner


@asynccontextmanager
async def cvlr_executor(args: CvlrArgs, summary: RunSummary) -> AsyncIterator[CvlrRunner]:
    """Set up from already-parsed args and yield the pipeline runner.

    Split from :func:`_entry_point` the way ``autoprove_executor`` is split from its parser: a
    caller with an ``CvlrArgs`` in hand — a test, or an embedder of this pipeline — should not have
    to go through ``sys.argv`` to reach the run.
    """
    thread_id = f"cvlr_{uuid.uuid4().hex[:12]}"
    project_root = pathlib.Path(args.project_root).resolve()
    main_source, identifier = parse_main_program(args.main_contract)

    # Before any service starts: a workspace cargo cannot read, a package that is not a member, and
    # a multi-program workspace with nothing naming the one to verify are all usage errors, and
    # each of them is cheaper here by one analysis agent than inside preflight.
    selected = await select_package(
        project_root, args.package, main_source=(project_root / main_source).resolve()
    )
    if identifier != (crate := selected.name.replace("-", "_")):
        # Legal, so not a refusal — the identifier only has to be a name the analysis can carry and
        # ``locate_main`` can match. But naming the program something other than its crate is
        # unusual enough to say out loud, since the near miss is what the refusal above catches.
        _log.warning(
            "cvlr: the main program is named %r while the crate is %r — proceeding, but the "
            "analysis will be told to declare %r",
            identifier, crate, identifier,
        )
    sandbox = build_confinement()
    announce_confinement(sandbox)

    async def runner(fact: HandlerFactory[CvlrPhase, None]) -> CvlrPipelineResult:
        async with cli_pipeline(
            args=args,
            thread_id=thread_id,
            summary=summary,
            task_handler=fact,
            design_doc_phase=CvlrPhase.DISCOVER_DESIGN_DOC,
            at_exit=_usage_exit_logger(summary, selected),
            forbidden_read=SOLANA.language.default_forbidden_read,
            max_properties=args.max_properties,
            workflow="cvlr",
            package=selected.name,
            confined=sandbox.enabled,
        ) as (staged, cont):
            # Two layers, first hit wins: the run's derived working tree above the developer's
            # checkout. Before the tree exists — analysis and extraction — every read falls through
            # to the project, which is what those phases are about. Once the formalizer has made it,
            # a file the run has munged reads as munged, because the tree is the same bytes the
            # compiler is given. The alternative, which cost a run, is an agent reasoning about
            # pristine source while the prover verifies something else
            # (``docs/the-tree-is-a-vfs.md``).
            project = pathlib.Path(staged.source.project_root)
            basic = build_layered_source_tools(
                [
                    DirBackend(project / WORK_DIR / BUILD_DIR, cache_listing=False),
                    DirBackend(project, cache_listing=False),
                ],
                forbidden_read=_source_surface(staged.source.forbidden_read),
            )
            # ``library_source`` is left unset for the same reason the expensive gate leaves it
            # unset: it would mount the CVLR crates for the code explorer, but which crates those
            # are is only resolved by preflight, which has not run yet. The backend mounts them for
            # the *author* from ``prepare_system``, where the resolved graph is known.
            full = build_source_tools(
                basic,
                staged.llm_models,
                staged.conns.indexed_store,
                user_ns("source_agent", "cache", staged.root_key),
                recursion_limit=args.recursion_limit,
                ecosystem=SOLANA,
            )
            # The staged embedding model, not a fresh one: ``get_model`` is not memoized, and a
            # second load is a second multi-hundred-megabyte transformer doing the same job.
            rag_tools = (
                build_rag_tools(args.rag_corpus, staged.embed_model)
                if args.rag_corpus != "none" else ()
            )
            env = PureServiceHost(
                models=staged.llm_models, rag_tools=rag_tools, sort="existing"
            ).bind_source_tools(full)
            backend = CvlrBackend(
                artifact_store=CvlrArtifactStore(
                    staged.source.project_root, selected.package_dir
                ),
                prover_opts=make_prover_options(cloud=True, app="solana"),
                sandbox=sandbox,
                # Namespaced by thread: rule names repeat across runs, and a shared namespace would
                # let one run's counterexamples be read as another's.
                cex_analysis=CexAnalysisStore(
                    store=staged.conns.store, namespace=("cvlr_cex", thread_id)
                ),
                package=selected.name,
            )
            return await cont(env, backend, SOLANA)

    yield runner
