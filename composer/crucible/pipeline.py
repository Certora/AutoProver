"""Assemble + run the Crucible application's pipeline.

The generic rust-app host builds a single-file :class:`RustArtifactStore`; Crucible
needs the crate-assembling :class:`CrucibleArtifactStore` (docs §7.1) plus its
host-resolved dependency (`CrucibleDep`, §6.1) and build/fuzz timeouts. So the
Crucible application supplies this thin pipeline wrapper — the small bespoke Python
the doc's §7.1 carve-out allows — while everything else (the driver, the SOLANA
ecosystem front half, the setup/per-component IoC loops) is reused unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from composer.crucible.harness import CrucibleDep
from composer.crucible.store import CrucibleArtifactStore
from composer.io.multi_job import HandlerFactory
from composer.pipeline.core import CorePipelineResult, PipelineRun, run_pipeline
from composer.pipeline.ecosystem import RUST_FORBIDDEN_READ
from composer.rustapp.adapter import RustBackend
from composer.rustapp.host import (
    build_core_phases,
    build_phase_enum,
    load_descriptor,
    load_module,
    resolve_ecosystem,
)
from composer.rustapp.result import RustFormalResult
from composer.sandbox.config import SandboxConfig
from composer.spec.context import SourceCode, WorkflowContext
from composer.spec.service_host import ModelProvider, PureServiceHost, ServiceHost
from composer.spec.source.source_env import build_basic_source_tools, build_source_tools

_log = logging.getLogger(__name__)

CRUCIBLE_MODULE = "crucible_app"


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
    full = build_source_tools(basic, model_provider, store, source_question_ns, recursion_limit=recursion_limit)

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

    return PureServiceHost(models=model_provider, rag_tools=rag_tools, sort="existing").bind_source_tools(full)


def resolve_crucible_repo(explicit: str | None = None) -> Path:
    """Locate the crucible checkout (source of the harness crate deps, §6.1): an
    explicit path, else ``$CRUCIBLE_REPO``, else ``~/src/crucible``. Errors if the
    crates aren't there (the harness would fail to build)."""
    candidate = Path(explicit or os.environ.get("CRUCIBLE_REPO", str(Path.home() / "src" / "crucible")))
    if not (candidate / "crates" / "crucible-fuzzer").is_dir():
        raise FileNotFoundError(
            f"crucible checkout not found at {candidate} (no crates/crucible-fuzzer). "
            "Set --crucible-repo / $CRUCIBLE_REPO to a local crucible clone."
        )
    return candidate


def build_crucible_backend(
    source_input: SourceCode,
    *,
    crucible_repo: Path,
    fuzz_timeout_s: int,
    command_timeout_s: int,
    sandbox: SandboxConfig | None = None,
    module_name: str = CRUCIBLE_MODULE,
) -> RustBackend:
    """Build the Crucible :class:`RustBackend` with the crate store + resolved deps."""
    module = load_module(module_name)
    descriptor = load_descriptor(module)
    ecosystem = resolve_ecosystem(descriptor)
    program = str(source_input.contract_name)
    dep = CrucibleDep(
        crucible_repo=crucible_repo,
        program_crate=program,
        program_rel=f"../../programs/{program}",
    )
    store = CrucibleArtifactStore(source_input.project_root, program=program, dep=dep)
    phase = build_phase_enum(descriptor)
    return RustBackend(
        module=module,
        descriptor=descriptor,
        _phase=phase,
        _core_phases=build_core_phases(descriptor, phase),
        artifact_store=store,
        ecosystem=ecosystem,
        command_timeout_s=command_timeout_s,
        fuzz_timeout_s=fuzz_timeout_s,
        sandbox=sandbox,
    )


def _crucible_sandbox(crucible_repo: Path) -> SandboxConfig:
    """The command-sandbox config for a Crucible run (``docs/command-sandbox.md``).

    **Defaults to the ``launcher`` provider** — Crucible compiles + runs untrusted
    native code, so every ``RunCommand`` is confined (Landlock + seccomp) by default.
    Requires the ``run-confined`` binary to be built/on PATH; if it isn't, the run is
    **fail-closed** (refuses rather than running unsandboxed). Override with
    ``COMPOSER_SANDBOX_PROVIDER=none`` for a trusted-input/dev run without the binary.

    The Crucible-specific read-only grants — the crucible checkout (path deps) and the
    ``crucible`` binary — extend the shared Rust toolchain set the launcher discovers;
    ``CARGO_HOME`` is granted rw so the offline `cargo build` can extract crate sources
    (§11 notes a per-run ``CARGO_HOME`` as the tighter follow-up)."""
    import shutil

    crucible_bin = shutil.which("crucible")
    extra_ro = tuple(
        p for p in (crucible_repo, Path(crucible_bin).parent if crucible_bin else None) if p is not None
    )
    cargo_home = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo"))
    provider = os.environ.get("COMPOSER_SANDBOX_PROVIDER", "launcher")
    return SandboxConfig(provider=provider, extra_ro=extra_ro, extra_rw=(cargo_home,))


async def run_crucible_pipeline(
    source_input: SourceCode,
    ctx: WorkflowContext[None],
    handler_factory: HandlerFactory,
    env: ServiceHost,
    *,
    crucible_repo: str | Path | None = None,
    fuzz_timeout_s: int = 30,
    command_timeout_s: int = 1800,
    max_concurrent: int = 4,
    max_bug_rounds: int = 3,
    interactive: bool = False,
) -> CorePipelineResult[RustFormalResult, Any]:
    """Run the whole Crucible vertical: the SOLANA ecosystem front half (analysis +
    property extraction) → the Crucible backend (shared fixture via the setup
    session, then per-component test authoring + fuzzing) → report."""
    # Build the program to sBPF up front (docs §5.1) — the harness loads the `.so`,
    # and it's the shared Solana build step, not per-component work.
    from composer.spec.solana.build import build_program

    repo = resolve_crucible_repo(str(crucible_repo) if crucible_repo else None)
    sandbox = _crucible_sandbox(repo)  # provider from env; default none (unsandboxed)

    await build_program(
        source_input.project_root, str(source_input.contract_name),
        timeout_s=command_timeout_s, sandbox=sandbox,
    )

    backend = build_crucible_backend(
        source_input,
        crucible_repo=repo,
        fuzz_timeout_s=fuzz_timeout_s,
        command_timeout_s=command_timeout_s,
        sandbox=sandbox,
    )
    run = PipelineRun(ctx, env, source_input, handler_factory, asyncio.Semaphore(max_concurrent))
    return await run_pipeline(
        backend, run, backend.ecosystem,
        interactive=interactive, threat_model=None, max_bug_rounds=max_bug_rounds,
    )
