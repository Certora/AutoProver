"""Assemble + run the Crucible application's pipeline.

Uses the generic rust-app host (:func:`~composer.rustapp.host.build_application` /
:func:`~composer.rustapp.host.run_application`) with Crucible's store factory and
:class:`~composer.crucible.backend.CrucibleBackend`. The only Crucible-only pre-step
is the shared sBPF ``build_program`` before the pipeline starts.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from composer.crucible.backend import CrucibleBackend
from composer.crucible.harness import CrucibleDep
from composer.crucible.store import CrucibleArtifactStore
from composer.io.multi_job import HandlerFactory
from composer.pipeline.core import CorePipelineResult
from composer.pipeline.ecosystem import RUST_FORBIDDEN_READ
from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.host import (
    BackendOptions,
    RustApplication,
    StoreFactory,
    build_application,
    run_application,
)
from composer.rustapp.result import RustFormalResult
from composer.sandbox.config import SandboxConfig
from composer.spec.context import SourceCode, WorkflowContext
from composer.spec.service_host import ModelProvider, PureServiceHost, ServiceHost
from composer.spec.source.source_env import build_basic_source_tools, build_source_tools

_log = logging.getLogger(__name__)

CRUCIBLE_MODULE = "crucible_app"
DEFAULT_COMMAND_TIMEOUT_S = 1800
DEFAULT_FUZZ_TIMEOUT_S = 30


def build_crucible_env(
    *,
    model_provider: ModelProvider,
    project_root: str,
    store: Any,
    source_question_ns: tuple[str, ...],
    recursion_limit: int,
    forbidden_read: str = RUST_FORBIDDEN_READ,
) -> ServiceHost:
    """The Crucible author's env: Rust source-navigation tools + the `crucible_kb`
    RAG search tools (§7.5). Falls back to no RAG (just the static cheat-sheet) if
    the embedding model / DB isn't available, so a run still works without it."""
    basic = build_basic_source_tools(root=project_root, forbidden_read=forbidden_read)
    full = build_source_tools(
        basic, model_provider, store, source_question_ns, recursion_limit=recursion_limit
    )

    rag_tools: tuple = ()
    try:
        from composer.rag.db import CRUCIBLE_DEFAULT_CONNECTION, PostgreSQLRAGDatabase
        from composer.rag.models import get_model
        from composer.tools.crucible_rag import get_tools as crucible_tools

        # Lazy pool — opens on first search; the DB must already be populated.
        db = PostgreSQLRAGDatabase(CRUCIBLE_DEFAULT_CONNECTION, get_model())
        rag_tools = tuple(crucible_tools(db))
    except Exception as e:  # noqa: BLE001 — RAG is optional; the cheat-sheet suffices
        _log.warning("crucible_kb RAG unavailable (%s); using the static cheat-sheet only", e)

    return PureServiceHost(
        models=model_provider, rag_tools=rag_tools, sort="existing"
    ).bind_source_tools(full)


def resolve_crucible_repo(explicit: str | None = None) -> Path:
    """Locate the crucible checkout (source of the harness crate deps, §6.1).

    Requires an explicit path or ``$CRUCIBLE_REPO``. Errors if neither is set or
    the crates aren't at that path (the harness would fail to build).
    """
    raw = explicit or os.environ.get("CRUCIBLE_REPO")
    if not raw:
        raise FileNotFoundError(
            "crucible checkout not configured. Set --crucible-repo / $CRUCIBLE_REPO "
            "to a local crucible clone (must contain crates/crucible-fuzzer)."
        )
    candidate = Path(raw)
    if not (candidate / "crates" / "crucible-fuzzer").is_dir():
        raise FileNotFoundError(
            f"crucible checkout not found at {candidate} (no crates/crucible-fuzzer). "
            "Set --crucible-repo / $CRUCIBLE_REPO to a local crucible clone."
        )
    return candidate


def crucible_sandbox(crucible_repo: Path) -> SandboxConfig:
    """The command-sandbox config for a Crucible run (``docs/command-sandbox.md``).

    **Defaults to the ``launcher`` provider** — Crucible compiles + runs untrusted
    native code, so every ``RunCommand`` is confined (Landlock + seccomp) by default.
    Requires the ``run-confined`` binary to be built/on PATH; if it isn't, the run is
    **fail-closed** (refuses rather than running unsandboxed). Override with
    ``COMPOSER_SANDBOX_PROVIDER=none`` for a trusted-input/dev run without the binary.

    The Crucible-specific read-only grants — the crucible checkout (path deps) and the
    ``crucible`` binary — extend the shared Rust toolchain set the launcher discovers.
    Offline cargo writes go to a **private per-run ``CARGO_HOME``** under the workdir
    (see ``sandbox_cargo_home``); the shared ``~/.cargo`` stays read-only.
    """
    import shutil

    crucible_bin = shutil.which("crucible")
    extra_ro = tuple(
        p
        for p in (crucible_repo, Path(crucible_bin).parent if crucible_bin else None)
        if p is not None
    )
    provider = os.environ.get("COMPOSER_SANDBOX_PROVIDER", "launcher")
    return SandboxConfig(provider=provider, extra_ro=extra_ro)


def _crucible_store_factory(crucible_repo: str | Path | None) -> StoreFactory:
    """Lazy store factory — resolves ``CRUCIBLE_REPO`` on first ``make_backend``, not at app build.

    So the CLI can synthesize the phase enum / argparse from the wheel without requiring
    a crucible checkout until the pipeline actually runs.
    """

    def factory(source: SourceCode, _descriptor: AppDescriptor) -> CrucibleArtifactStore:
        repo = resolve_crucible_repo(str(crucible_repo) if crucible_repo else None)
        program = str(source.contract_name)
        dep = CrucibleDep(
            crucible_repo=repo,
            program_crate=program,
            program_rel=f"../../programs/{program}",
        )
        return CrucibleArtifactStore(source.project_root, program=program, dep=dep)

    return factory


def build_crucible_application(
    *,
    crucible_repo: str | Path | None = None,
    fuzz_timeout_s: int = DEFAULT_FUZZ_TIMEOUT_S,
    command_timeout_s: int = DEFAULT_COMMAND_TIMEOUT_S,
    sandbox: SandboxConfig | None = None,
    module_name: str = CRUCIBLE_MODULE,
) -> RustApplication:
    """Synthesize the Crucible :class:`RustApplication` (one phase enum for UI + pipeline).

    Does not require a crucible checkout until :meth:`RustApplication.make_backend` /
    :func:`run_crucible_pipeline` runs. Pass ``sandbox=`` explicitly, or leave ``None``
    and let :func:`run_crucible_pipeline` install the default launcher config.
    """
    return build_application(
        module_name,
        store_factory=_crucible_store_factory(crucible_repo),
        backend_cls=CrucibleBackend,
        command_timeout_s=command_timeout_s,
        fuzz_timeout_s=fuzz_timeout_s,
        sandbox=sandbox,
    )


def build_crucible_backend(
    source_input: SourceCode,
    *,
    crucible_repo: Path | str | None = None,
    fuzz_timeout_s: int = DEFAULT_FUZZ_TIMEOUT_S,
    command_timeout_s: int = DEFAULT_COMMAND_TIMEOUT_S,
    sandbox: SandboxConfig | None = None,
    module_name: str = CRUCIBLE_MODULE,
) -> CrucibleBackend:
    """Headless convenience: build only the backend (tests / scripts)."""
    repo = resolve_crucible_repo(str(crucible_repo) if crucible_repo else None)
    sb = sandbox if sandbox is not None else crucible_sandbox(repo)
    app = build_crucible_application(
        crucible_repo=repo,
        fuzz_timeout_s=fuzz_timeout_s,
        command_timeout_s=command_timeout_s,
        sandbox=sb,
        module_name=module_name,
    )
    backend = app.make_backend(source_input)
    assert isinstance(backend, CrucibleBackend)
    return backend


async def run_crucible_pipeline(
    source_input: SourceCode,
    ctx: WorkflowContext[None],
    handler_factory: HandlerFactory,
    env: ServiceHost,
    *,
    crucible_repo: str | Path | None = None,
    fuzz_timeout_s: int = DEFAULT_FUZZ_TIMEOUT_S,
    command_timeout_s: int = DEFAULT_COMMAND_TIMEOUT_S,
    max_concurrent: int = 4,
    max_bug_rounds: int = 3,
    interactive: bool = False,
    app: RustApplication | None = None,
) -> CorePipelineResult[RustFormalResult]:
    """Run the whole Crucible vertical: sBPF build → SOLANA front half →
    Crucible setup + per-component authoring/fuzz → report.

    Pass a pre-built ``app`` (from :func:`build_crucible_application`) so the CLI
    frontend and pipeline share one phase enum; otherwise one is synthesized here.
    """
    from composer.spec.solana.build import build_program

    repo = resolve_crucible_repo(str(crucible_repo) if crucible_repo else None)
    sandbox = crucible_sandbox(repo)

    if app is None:
        app = build_crucible_application(
            crucible_repo=repo,
            fuzz_timeout_s=fuzz_timeout_s,
            command_timeout_s=command_timeout_s,
            sandbox=sandbox,
        )
    else:
        # Same app object as the frontend — update mutable options, keep phase enum.
        opts: BackendOptions = app.options
        opts.fuzz_timeout_s = fuzz_timeout_s
        opts.command_timeout_s = command_timeout_s
        if opts.sandbox is None:
            opts.sandbox = sandbox
        sandbox = opts.sandbox

    # Shared Solana build step (docs §5.1) — the harness loads the `.so`.
    await build_program(
        source_input.project_root,
        str(source_input.contract_name),
        timeout_s=command_timeout_s,
        sandbox=sandbox,
    )

    return await run_application(
        app,
        source_input,
        ctx,
        handler_factory,
        env,
        max_concurrent=max_concurrent,
        max_bug_rounds=max_bug_rounds,
        interactive=interactive,
    )
