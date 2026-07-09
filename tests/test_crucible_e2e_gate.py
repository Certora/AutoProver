"""Phase-5 gate: the WHOLE Crucible vertical, with a REAL model.

Runs `run_crucible_pipeline` on the `solana_vault` scenario — the SOLANA ecosystem
front half (analysis → property extraction) → the Crucible backend (shared fixture
via the setup session, then per-component test authoring + fuzzing) → report — as a
single pipeline, exactly as `console-crucible` would. Pass = it analyzes the program
into instructions, extracts properties, and produces per-component fuzz verdicts
with no human edits.

The heaviest, most expensive gate: real LLM across every phase + a fuzz campaign per
instruction. Same prerequisites as the other crucible gates (toolchain, `crucible`,
`CRUCIBLE_REPO`). Run in the background.

    CRUCIBLE_REPO=/path/to/crucible \
      .venv/bin/python -m pytest tests/test_crucible_e2e_gate.py -m expensive -q -s
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, TYPE_CHECKING

import psycopg
import pytest
from psycopg.sql import SQL, Identifier, Literal

import composer.workflow.services as services
from composer.crucible.pipeline import run_crucible_pipeline
from composer.kb.knowledge_base import DefaultEmbedder
from composer.pipeline.core import Delivered
from composer.pipeline.ecosystem import RUST_FORBIDDEN_READ
from composer.rustapp.frontend import GenericRustConsoleHandler
from composer.spec.context import SourceCode, WorkflowContext
from composer.spec.service_host import ModelProvider, PureServiceHost
from composer.spec.source.source_env import build_basic_source_tools, build_source_tools
from composer.spec.system_model import SolidityIdentifier
from composer.ui.tool_display import async_tool_context
from composer.workflow.services import llm_factory, standard_connections

from tests.conftest import MockSentenceTransformer, needs_postgres
from tests.test_autoprove_integration import _MEMORIES_DDL, _RAG_DB, _VECTOR_DBS, _db_url

if TYPE_CHECKING:
    from testcontainers.postgres import PostgresContainer

pytestmark = [pytest.mark.expensive, needs_postgres, pytest.mark.asyncio]

_SCENARIO = Path(__file__).parent.parent / "test_scenarios" / "solana_vault"
_PROGRAM = "vault"


def _model_args() -> object:
    return SimpleNamespace(
        heavy_model="claude-opus-4-6", lite_model="claude-sonnet-4-6",
        tokens=128_000, thinking_tokens=2048, memory_tool=False, interleaved_thinking=False,
    )


def _crucible_repo() -> Path | None:
    repo = Path(os.environ.get("CRUCIBLE_REPO", str(Path.home() / "src" / "crucible")))
    return repo if (repo / "crates" / "crucible-fuzzer").is_dir() else None


def _require(cond: bool, why: str) -> None:
    if not cond:
        pytest.skip(why)


async def test_crucible_full_vertical(pg_container: "PostgresContainer", monkeypatch):
    _require(_SCENARIO.is_dir(), f"scenario missing: {_SCENARIO}")
    _require(shutil.which("cargo-build-sbf") is not None, "cargo-build-sbf not on PATH")
    _require(shutil.which("crucible") is not None, "crucible CLI not on PATH")
    _require(_crucible_repo() is not None, "set CRUCIBLE_REPO to a local crucible checkout")

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

    args = _model_args()
    async with (
        standard_connections(embedder=DefaultEmbedder(MockSentenceTransformer())) as conns,
        async_tool_context(),
    ):
        content = await conns.uploader.get_document(_SCENARIO / "system.md")
        assert content is not None
        source = SourceCode(
            content=content, project_root=str(_SCENARIO),
            contract_name=SolidityIdentifier(_PROGRAM),
            relative_path=f"programs/{_PROGRAM}/src/lib.rs", forbidden_read=RUST_FORBIDDEN_READ,
        )
        model_provider = ModelProvider(
            checkpointer=conns.checkpointer, factory=llm_factory(cast(Any, args)),
            heavy_model=args.heavy_model, lite_model=args.lite_model,
        )
        basic = build_basic_source_tools(root=str(_SCENARIO), forbidden_read=RUST_FORBIDDEN_READ)
        full = build_source_tools(basic, model_provider, conns.indexed_store, ("crucible_e2e", "src"), recursion_limit=100)
        env = PureServiceHost(models=model_provider, rag_tools=(), sort="existing").bind_source_tools(full)
        ctx = WorkflowContext.create(
            services=conns.memory,
            thread_id="crucible_e2e", store=conns.store, recursion_limit=100,
            cache_namespace=None, memory_namespace=None,
        )

        handler = GenericRustConsoleHandler({"fuzz_pulse", "fuzz_finding", "build_output"})
        result = await run_crucible_pipeline(
            source, ctx, handler.make_handler, env,
            fuzz_timeout_s=12, max_concurrent=2, max_bug_rounds=1, interactive=False,
        )

    print(f"\nCrucible E2E: {result.n_components} instruction(s), {result.n_properties} properties")
    for o in result.outcomes:
        verdicts = o.result.result.verdicts if isinstance(o.result, Delivered) else "(not delivered)"
        print(f"  == {o.feat.display_name} == delivered={isinstance(o.result, Delivered)} verdicts={verdicts}")
    if result.failures:
        print("failures:", result.failures)

    # The front half ran (instructions analyzed, properties extracted) ...
    assert result.n_components > 0, "no instructions analyzed"
    assert result.n_properties > 0, "no properties extracted"
    # ... and at least one component was formalized into a fuzz-verdicted test.
    delivered = [o for o in result.outcomes if isinstance(o.result, Delivered)]
    assert delivered, f"no component was delivered; failures={result.failures}"
    assert any(o.result.result.verdicts for o in delivered), "no fuzz verdicts produced"
