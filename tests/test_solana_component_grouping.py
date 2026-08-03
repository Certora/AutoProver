"""Probe: run ONLY the Solana system-analysis phase against a real program and print the
``ProgramComponent`` grouping it produces.

This is the tool docs/crucible-component-units.md (PR3) §13 staging step 1 calls for — *"ship the model
+ prompt + validation, then read the groupings that come back on real programs"*. It stops after
analysis (no property extraction, no backend), so it costs one agent run rather than a full front
half, and it exercises exactly the three things stage 1 added: the model, the prompt section, and
``_solana_validate``'s component rules.

Points at a program via environment, and skips when unset (there is no large Solana program in
this repo — ``test_scenarios/solana_vault`` is a 3-instruction toy whose grouping proves nothing):

    SOLANA_PROBE_ROOT=/path/to/workspace \\
    SOLANA_PROBE_DOC=docs/DESIGN.md \\
    SOLANA_PROBE_SRC=programs/<program>/src/lib.rs \\
    SOLANA_PROBE_MAIN=<program identifier> \\
    env -u CERTORA .venv/bin/python -m pytest tests/test_solana_component_grouping.py \\
        -m expensive -q -s
"""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TYPE_CHECKING, cast

import psycopg
import pytest
from psycopg.sql import SQL, Identifier, Literal

import composer.workflow.services as services
from composer.io.multi_job import TaskInfo
from composer.kb.knowledge_base import DefaultEmbedder
from composer.llm.registry import get_provider_for
from composer.pipeline.core import PipelineRun
from composer.pipeline.ecosystem import RUST_FORBIDDEN_READ, SOLANA
from composer.rustapp.frontend import GenericRustConsoleHandler
from composer.spec.context import CacheKey, SourceCode, WorkflowContext
from composer.spec.service_host import ModelProvider, PureServiceHost
from composer.spec.solana.model import SolanaApplication
from composer.spec.solana.null_backend import SolanaPhase
from composer.spec.source.source_env import build_basic_source_tools, build_source_tools
from composer.spec.source.task_ids import SYSTEM_ANALYSIS_TASK_ID
from composer.spec.system_analysis import run_component_analysis
from composer.spec.system_model import SolidityIdentifier
from composer.ui.tool_display import async_tool_context
from composer.workflow.services import standard_connections

from tests.conftest import (
    _MEMORIES_DDL,
    _RAG_DB,
    _VECTOR_DBS,
    MockSentenceTransformer,
    _db_url,
    needs_postgres,
)

if TYPE_CHECKING:
    from testcontainers.postgres import PostgresContainer

pytestmark = [pytest.mark.expensive, needs_postgres, pytest.mark.asyncio]


def _target() -> tuple[Path, Path, str, str] | None:
    root = os.environ.get("SOLANA_PROBE_ROOT")
    if not root:
        return None
    return (
        Path(root),
        Path(root) / os.environ["SOLANA_PROBE_DOC"],
        os.environ["SOLANA_PROBE_SRC"],
        os.environ["SOLANA_PROBE_MAIN"],
    )


def _model_args() -> object:
    return SimpleNamespace(
        heavy_model="claude-opus-4-6",
        lite_model="claude-sonnet-4-6",
        tokens=128_000,
        thinking_tokens=8192,
        memory_tool=False,
        interleaved_thinking=False,
    )


def _provision(pg_container: "PostgresContainer") -> None:
    """The roles/databases the pipeline expects (same as the solana gate)."""
    admin_url = pg_container.get_connection_url(driver=None)
    with psycopg.connect(admin_url, autocommit=True) as admin:
        for cfg in services._DATABASE_CONFIGS.values():
            admin.execute(
                SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    Identifier(cfg["user"]), Literal(cfg["password"])
                )
            )
            admin.execute(
                SQL("CREATE DATABASE {} OWNER {}").format(
                    Identifier(cfg["database"]), Identifier(cfg["user"])
                )
            )
        admin.execute(SQL("CREATE DATABASE {}").format(Identifier(_RAG_DB)))
    for db in _VECTOR_DBS:
        with psycopg.connect(_db_url(pg_container, db), autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # As the memory ROLE, not the superuser — the table must be owned by the role that will read
    # it, or the agent's first memory-tool call dies on "permission denied for table memories_fs".
    mem = services._DATABASE_CONFIGS["memory"]
    mem_url = (
        f"postgresql://{mem['user']}:{mem['password']}"
        f"@{pg_container.get_container_host_ip()}:{pg_container.get_exposed_port(5432)}"
        f"/{mem['database']}"
    )
    with psycopg.connect(mem_url, autocommit=True) as conn:
        conn.execute(_MEMORIES_DDL)


def _report(app: SolanaApplication, main: str) -> None:
    print(f"\n=== {app.application_type}: {app.description}")
    print(f"    authorities: {', '.join(a.name for a in app.authorities) or '(none)'}")
    for prog in app.programs:
        star = " <== main" if prog.program_identifier == main else ""
        print(f"\n--- program {prog.name} ({prog.program_identifier}){star}")
        print(f"    {len(prog.instructions)} instructions, {len(prog.components)} components")
        assigned: set[str] = set()
        for comp in prog.components:
            assigned.update(comp.instructions)
            print(f"\n  [{comp.name}] {comp.description}")
            print(f"    instructions ({len(comp.instructions)}): {', '.join(comp.instructions)}")
            if comp.account_types:
                print(f"    state: {', '.join(comp.account_types)}")
            for req in comp.requirements:
                print(f"    req: {req}")
            for inter in comp.interactions:
                target = getattr(inter, "authority", None) or (
                    f"{inter.program}.{inter.component}"  # type: ignore[union-attr]
                )
                print(f"    -> {target}: {inter.description}")
        # The validator already enforces this; print it so a human can see the shape at a glance.
        overlap = [i.name for i in prog.instructions if sum(
            i.name in c.instructions for c in prog.components) > 1]
        print(f"\n    coverage: {len(assigned)}/{len(prog.instructions)} instructions assigned"
              f"{f'; {len(overlap)} in >1 component: {overlap}' if overlap else ''}")


async def test_component_grouping_on_a_real_program(pg_container: "PostgresContainer", monkeypatch):
    target = _target()
    if target is None:
        pytest.skip("set SOLANA_PROBE_ROOT/_DOC/_SRC/_MAIN to point at a real Solana workspace")
    root, doc, src_rel, main_id = target
    assert root.is_dir() and doc.is_file(), (root, doc)

    _provision(pg_container)
    monkeypatch.setenv("CERTORA_AI_COMPOSER_PGHOST", pg_container.get_container_host_ip())
    monkeypatch.setenv("CERTORA_AI_COMPOSER_PGPORT", str(pg_container.get_exposed_port(5432)))

    args = _model_args()
    root_s = str(root)
    async with (
        standard_connections(
            provider="anthropic", embedder=DefaultEmbedder(MockSentenceTransformer())
        ) as conns,
        async_tool_context(),
    ):
        content = await conns.uploader.get_document(doc)
        assert content is not None
        source = SourceCode(
            content=content,
            project_root=root_s,
            contract_name=SolidityIdentifier(main_id),
            relative_path=src_rel,
            forbidden_read=RUST_FORBIDDEN_READ,
        )
        tiered = get_provider_for(tiered=cast(Any, args))
        models = ModelProvider(
            heavy_model=tiered.heavy, lite_model=tiered.lite, checkpointer=conns.checkpointer
        )
        basic = build_basic_source_tools(root=root_s, forbidden_read=RUST_FORBIDDEN_READ)
        full = build_source_tools(
            basic, models, conns.indexed_store, ("solana_grouping", "src"), recursion_limit=100
        )
        env = PureServiceHost(models=models, rag_tools=(), sort="existing").bind_source_tools(full)
        ctx: WorkflowContext[Any] = WorkflowContext.create(
            services=conns.memory,
            thread_id="solana_grouping",
            store=conns.store,
            recursion_limit=100,
            cache_namespace=None,
            memory_namespace=None,
        )

        # Go through PipelineRun.runner so the IO handler / task scope the analysis graph needs is
        # installed — the same path run_pipeline takes, minus every phase after analysis.
        run = PipelineRun(
            ctx=ctx,
            source=source,
            _handler_factory=GenericRustConsoleHandler(set()).make_handler,
            _semaphore=asyncio.Semaphore(4),
            env=env,
        )
        analyzed = await run.runner(
            TaskInfo(SYSTEM_ANALYSIS_TASK_ID, "System Analysis", SolanaPhase.ANALYSIS),
            lambda: run_component_analysis(
                ty=SOLANA.system_model,
                child_ctxt=ctx.child(CacheKey("solana-analysis")),
                input=source,
                env=env,
                extra_input=list(SOLANA.analysis_extra_input(source)),
                expected_main_id=source.contract_name,
                system_template=SOLANA.analysis_prompts.system,
                initial_template=SOLANA.analysis_prompts.initial,
                validate=SOLANA.validate_analysis,
            ),
        )

    assert analyzed is not None, "system analysis produced no result"
    assert isinstance(analyzed, SolanaApplication)
    _report(analyzed, main_id)

    # Stage 1's contract: analysis emits components, and they satisfy the validator (which the
    # analysis loop already enforced via retry — re-asserted here so a regression is loud).
    main = next(p for p in analyzed.programs if p.program_identifier == main_id)
    assert main.components, "the main program came back with no components"
    assert SOLANA.validate_analysis(analyzed, source.contract_name) is None
