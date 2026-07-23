"""Phase-3 gate: the Crucible fixture-authoring loop, with a REAL model.

Drives the crucible wheel's `new_setup_session` decider through the IoC loop
(`drive_session` + `RealEffects`) on the `solana_vault` scenario: the agent reads
the program source (tool-enabled `call_llm`), authors a `Fixture`, and the loop
validates it with `crucible run … --dry-run`, revising on failure. Pass = the
session *publishes* a fixture (i.e. a dry-run went green) with no human edits.

Heavy + paid: real LLM + Postgres (testcontainers) + the Solana/Anchor toolchain +
the `crucible` CLI + a local crucible checkout (`CRUCIBLE_REPO`). The first
harness build compiles litesvm/libafl (minutes); run
it in the background. Skips cleanly if a prerequisite is missing.

    CRUCIBLE_REPO=/path/to/crucible \
      .venv/bin/python -m pytest tests/test_crucible_setup_gate.py -m expensive -q -s
"""

import asyncio
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, TYPE_CHECKING

import psycopg
import pytest
from psycopg.sql import SQL, Identifier, Literal

import composer.workflow.services as services
from composer.io.multi_job import TaskInfo
from composer.kb.knowledge_base import DefaultEmbedder
from composer.pipeline.core import GaveUp, PipelineRun
from composer.pipeline.ecosystem import RUST_FORBIDDEN_READ
from composer.rustapp.adapter import author_and_compile, make_emitter
from composer.rustapp.frontend import GenericRustConsoleHandler
from composer.rustapp.host import build_phase_enum, load_descriptor, load_module
from composer.spec.context import SourceCode, WorkflowContext
from composer.spec.service_host import ModelProvider, PureServiceHost
from composer.llm.registry import get_provider_for
from composer.spec.solana.build import build_program
from composer.spec.source.source_env import build_basic_source_tools, build_source_tools
from composer.spec.system_model import SolidityIdentifier
from composer.ui.tool_display import async_tool_context
from composer.workflow.services import llm_factory, standard_connections

from tests.conftest import MockSentenceTransformer, needs_postgres
from tests.conftest import _MEMORIES_DDL, _RAG_DB, _VECTOR_DBS, _db_url

if TYPE_CHECKING:
    from testcontainers.postgres import PostgresContainer

pytestmark = [pytest.mark.expensive, needs_postgres, pytest.mark.asyncio]

_SCENARIO = Path(__file__).parent.parent / "test_scenarios" / "solana_vault"
_PROGRAM = "vault"

# Minimal analyzed model — enough context for the prompt; the agent reads the real
# source for exact signatures. (The full front-half analysis produces this in prod.)
_ANALYZED: dict = {
    "programs": [
        {
            "name": "vault",
            "program_identifier": "vault",
            "description": "A lamports vault: each user owns a PDA vault (seeds [b\"vault\", authority]) "
            "holding SOL, recording an authority and a balance.",
            "instructions": [
                {"name": "initialize", "description": "Create the caller's vault PDA; set authority=caller, balance=0."},
                {"name": "deposit", "args": ["amount: u64"], "description": "Transfer `amount` lamports from the depositor into the vault PDA (System Program transfer); increase balance."},
                {"name": "withdraw", "args": ["amount: u64"], "description": "Transfer `amount` lamports from the vault to the authority (only the authority may sign); decrease balance."},
            ],
        }
    ],
}


def _model_args() -> object:
    return SimpleNamespace(
        heavy_model="claude-opus-4-6",
        lite_model="claude-sonnet-4-6",
        tokens=128_000,
        thinking_tokens=2048,
        memory_tool=False,
        interleaved_thinking=False,
    )


def _crucible_repo() -> Path | None:
    raw = os.environ.get("CRUCIBLE_REPO")
    if not raw:
        return None
    repo = Path(raw)
    return repo if (repo / "crates" / "crucible-fuzzer").is_dir() else None


def _require(cond: bool, why: str) -> None:
    if not cond:
        pytest.skip(why)


async def test_crucible_fixture_authoring(pg_container: "PostgresContainer", monkeypatch):
    _require(_SCENARIO.is_dir(), f"scenario missing: {_SCENARIO}")
    _require(shutil.which("cargo-build-sbf") is not None, "cargo-build-sbf not on PATH")
    _require(shutil.which("crucible") is not None, "crucible CLI not on PATH")
    crucible_repo = _crucible_repo()
    _require(crucible_repo is not None, "set CRUCIBLE_REPO to a local crucible checkout")
    assert crucible_repo is not None

    # --- Postgres roles + databases (matches services._DATABASE_CONFIGS) ---
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
    embedder = MockSentenceTransformer()

    # --- Build the program up front (target/deploy/vault.so) ---
    built = await build_program(_SCENARIO, _PROGRAM, timeout_s=590)
    assert built.so_path.is_file(), built.so_path

    async with (
        standard_connections(provider="anthropic", embedder=DefaultEmbedder(embedder)) as conns,
        async_tool_context(),
    ):
        content = await conns.uploader.get_document(_SCENARIO / "system.md")
        assert content is not None
        source = SourceCode(
            content=content,
            project_root=str(_SCENARIO),
            contract_name=SolidityIdentifier(_PROGRAM),
            relative_path=f"programs/{_PROGRAM}/src/lib.rs",
            forbidden_read=RUST_FORBIDDEN_READ,
        )
        _tiered = get_provider_for(tiered=cast(Any, args))
        model_provider = ModelProvider(
            heavy_model=_tiered.heavy, lite_model=_tiered.lite, checkpointer=conns.checkpointer,
        )
        basic = build_basic_source_tools(root=str(_SCENARIO), forbidden_read=RUST_FORBIDDEN_READ)
        full = build_source_tools(basic, model_provider, conns.indexed_store, ("crucible_setup", "src"), recursion_limit=100)
        env = PureServiceHost(models=model_provider, rag_tools=(), sort="existing").bind_source_tools(full)

        ctx = WorkflowContext.create(
            services=conns.memory,
            thread_id="crucible_setup", store=conns.store, recursion_limit=100,
            cache_namespace=None, memory_namespace=None,
        )

        # No manifest pre-placement needed: `compile` materializes the harness crate
        # (Cargo.toml + main.rs) itself per run (docs/rust-pure-app.md §4).
        module = load_module("crucible_app")
        descriptor = load_descriptor(module)
        phase = build_phase_enum(descriptor)

        # The setup artifact: author the fixture, then `compile` (crucible --dry-run) it.
        # Unsandboxed here (the gate trusts its inputs), so the argv prefix is empty.
        setup_input = {
            "kind": "setup", "program": _PROGRAM,
            "component": _ANALYZED, "props": [], "context": {},
        }
        sandbox_dict = {"argv_prefix": [], "timeout_s": 1200}
        run = PipelineRun(ctx=ctx, source=source, _handler_factory=GenericRustConsoleHandler(set()).make_handler, _semaphore=asyncio.Semaphore(2), env=env)

        result = await run.runner(
            TaskInfo("crucible_setup", "Build Harness", phase["build_harness"]),
            lambda: author_and_compile(
                module, setup_input, env=env, sandbox_dict=sandbox_dict,
                workdir=Path(_SCENARIO), recursion_limit=100, backend_name="crucible",
                emit=make_emitter(),
            ),
        )

    if isinstance(result, GaveUp):
        pytest.fail(f"fixture authoring gave up: {result.reason}")
    fixture = result
    print("\n===== authored fixture =====\n" + fixture)
    # It published, so a --dry-run went green. Sanity-check the shape.
    assert "struct Fixture" in fixture, "fixture must define `struct Fixture`"
    assert "fuzz_fixture" in fixture, "fixture must use #[fuzz_fixture]"
    # And it must NOT smuggle in a test fn (those are per-component, later). Check for
    # the attributes, not bare substrings — `use crucible_fuzzer::*` is expected.
    assert "#[invariant_test]" not in fixture and "#[crucible_fuzz]" not in fixture
