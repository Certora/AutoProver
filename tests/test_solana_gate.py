"""Phase-4 gate: the Solana front half (analysis + property extraction) end-to-end.

Runs the shared driver over the SOLANA ecosystem + a null backend on a sample Anchor vault,
with REAL models. Everything else (Postgres, source tools) runs for real; there is no prover or
AutoSetup (the null backend just records properties). Pass = the pipeline runs to completion and
extracts a non-empty set of properties; the printed properties let a human judge "sane".

Marked ``expensive`` (real LLM + containers). Run with:
    env -u CERTORA .venv/bin/python -m pytest tests/test_solana_gate.py -m expensive -q -s
"""
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, TYPE_CHECKING

import psycopg
import pytest
from psycopg.sql import SQL, Identifier, Literal

import composer.workflow.services as services
from composer.pipeline.core import PipelineRun, run_pipeline
from composer.pipeline.ecosystem import SOLANA, RUST_FORBIDDEN_READ
from composer.spec.context import SourceCode, WorkflowContext
from composer.spec.service_host import ModelProvider, PureServiceHost
from composer.llm.registry import get_provider_for
from composer.spec.solana.null_backend import NullSolanaArtifactStore, NullSolanaBackend
from composer.spec.source.source_env import build_basic_source_tools, build_source_tools
from composer.spec.system_model import SolidityIdentifier
from composer.ui.tool_display import async_tool_context
from composer.rustapp.frontend import GenericRustConsoleHandler
from composer.workflow.services import standard_connections, llm_factory
from composer.kb.knowledge_base import DefaultEmbedder

from tests.conftest import needs_postgres, MockSentenceTransformer
from tests.test_autoprove_integration import _RAG_DB, _VECTOR_DBS, _MEMORIES_DDL, _db_url

if TYPE_CHECKING:
    from testcontainers.postgres import PostgresContainer

pytestmark = [pytest.mark.expensive, needs_postgres, pytest.mark.asyncio]

_SCENARIO = Path(__file__).parent.parent / "test_scenarios" / "solana_vault"


def _model_args() -> object:
    return SimpleNamespace(
        heavy_model="claude-opus-4-6",
        lite_model="claude-sonnet-4-6",
        tokens=128_000,
        thinking_tokens=2048,
        memory_tool=False,
        interleaved_thinking=False,
    )


async def test_solana_vault_front_half(pg_container: "PostgresContainer", monkeypatch):
    assert _SCENARIO.is_dir(), _SCENARIO

    # 1. Roles + databases the pipeline expects (matches services._DATABASE_CONFIGS).
    admin_url = pg_container.get_connection_url(driver=None)
    with psycopg.connect(admin_url, autocommit=True) as admin:
        for cfg in services._DATABASE_CONFIGS.values():
            admin.execute(SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                Identifier(cfg["user"]), Literal(cfg["password"])))
            admin.execute(SQL("CREATE DATABASE {} OWNER {}").format(
                Identifier(cfg["database"]), Identifier(cfg["user"])))
        admin.execute(SQL("CREATE DATABASE {}").format(Identifier(_RAG_DB)))
    for db in _VECTOR_DBS:
        with psycopg.connect(_db_url(pg_container, db), autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    mem = services._DATABASE_CONFIGS["memory"]
    mem_url = (
        f"postgresql://{mem['user']}:{mem['password']}"
        f"@{pg_container.get_container_host_ip()}:{pg_container.get_exposed_port(5432)}/{mem['database']}"
    )
    with psycopg.connect(mem_url, autocommit=True) as conn:
        conn.execute(_MEMORIES_DDL)

    monkeypatch.setenv("CERTORA_AI_COMPOSER_PGHOST", pg_container.get_container_host_ip())
    monkeypatch.setenv("CERTORA_AI_COMPOSER_PGPORT", str(pg_container.get_exposed_port(5432)))

    model = MockSentenceTransformer()  # deterministic embedder; nothing here needs real embeddings
    args = _model_args()

    async with (
        standard_connections(provider="anthropic", embedder=DefaultEmbedder(model)) as conns,
        async_tool_context(),
    ):
        content = await conns.uploader.get_document(_SCENARIO / "system.md")
        assert content is not None
        source = SourceCode(
            content=content,
            project_root=str(_SCENARIO),
            contract_name=SolidityIdentifier("vault"),   # the target program's identifier
            relative_path="programs/vault/src/lib.rs",
            forbidden_read=RUST_FORBIDDEN_READ,
        )
        _tiered = get_provider_for(tiered=cast(Any, args))
        model_provider = ModelProvider(
            heavy_model=_tiered.heavy, lite_model=_tiered.lite, checkpointer=conns.checkpointer,
        )
        basic = build_basic_source_tools(root=str(_SCENARIO), forbidden_read=RUST_FORBIDDEN_READ)
        full = build_source_tools(basic, model_provider, conns.indexed_store, ("solana_gate", "src"), recursion_limit=100)
        env = PureServiceHost(models=model_provider, rag_tools=(), sort="existing").bind_source_tools(full)

        ctx = WorkflowContext.create(
            services=conns.memory,
            thread_id="solana_gate", store=conns.store, recursion_limit=100,
            cache_namespace=None, memory_namespace=None,
        )

        import asyncio
        backend = NullSolanaBackend(NullSolanaArtifactStore(str(_SCENARIO)))
        run = PipelineRun(ctx=ctx, source=source, _handler_factory=GenericRustConsoleHandler(set()).make_handler, _semaphore=asyncio.Semaphore(4), env=env)
        result = await run_pipeline(backend, run, ecosystem=SOLANA, interactive=False, threat_model=None, max_bug_rounds=1)

    # Pass: front half ran and extracted properties.
    print(f"\nSolana gate: {result.n_components} invariant(s), {result.n_properties} properties")
    for o in result.outcomes:
        print(f"\n== {o.feat.display_name} ==")
        for p in o.props:
            print(f"  [{p.sort}] {p.title}: {p.description}")
    assert result.n_components > 0, "no invariants extracted"
    assert result.n_properties > 0, "no properties extracted"
