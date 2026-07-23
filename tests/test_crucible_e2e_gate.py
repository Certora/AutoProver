"""Phase-5 gate: the WHOLE Crucible vertical, with a REAL model.

Runs the whole vertical via the generic host (`build_application` + `run_application`) on
the `solana_vault` scenario — the SOLANA ecosystem front half (analysis → property
extraction) → the crucible_app wheel (shared fixture via the setup step, then per-component
test authoring + fuzzing) → report — exactly as `console-crucible` would. Pass = it analyzes the program
into instructions, extracts properties, and produces per-component fuzz verdicts
with no human edits.

The heaviest, most expensive gate: real LLM across every phase + a fuzz campaign per
instruction. Same prerequisites as the other crucible gates (toolchain, `crucible`,
`CRUCIBLE_REPO`). Run in the background.

    CRUCIBLE_REPO=/path/to/crucible \
      .venv/bin/python -m pytest tests/test_crucible_e2e_gate.py -m expensive -q -s
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
from composer.kb.knowledge_base import DefaultEmbedder
from composer.pipeline.core import Delivered
from composer.pipeline.ecosystem import RUST_FORBIDDEN_READ
from composer.rustapp.frontend import GenericRustConsoleHandler
from composer.rustapp.host import build_application, run_application
from composer.sandbox.config import SandboxConfig
from composer.spec.context import SourceCode, WorkflowContext
from composer.spec.service_host import ModelProvider, PureServiceHost
from composer.llm.registry import get_provider_for
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


def _model_args() -> object:
    return SimpleNamespace(
        heavy_model="claude-opus-4-6", lite_model="claude-sonnet-4-6",
        tokens=128_000, thinking_tokens=2048, memory_tool=False, interleaved_thinking=False,
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


async def test_crucible_full_vertical(pg_container: "PostgresContainer", monkeypatch, tmp_path):
    _require(_SCENARIO.is_dir(), f"scenario missing: {_SCENARIO}")
    _require(shutil.which("cargo-build-sbf") is not None, "cargo-build-sbf not on PATH")
    _require(shutil.which("crucible") is not None, "crucible CLI not on PATH")
    _require(_crucible_repo() is not None, "set CRUCIBLE_REPO to a local crucible checkout")

    # Work on a writable copy, not the committed scenario. The run writes hundreds
    # of MB of build artifacts into project_root (.sandbox_cargo/, target/, fuzz/,
    # …); an in-container run's image copy is read-only for the non-root runtime
    # user, and a host run would otherwise pollute test_scenarios/ (see
    # docs/crucible-demo.md §3). Exclude the heavy generated dirs from the copy.
    scenario = tmp_path / "solana_vault"
    shutil.copytree(
        _SCENARIO, scenario,
        ignore=shutil.ignore_patterns(
            ".sandbox_cargo", ".sandbox_tmp", "target", "corpus", "output",
            "fuzz", "certora", ".certora_internal",
        ),
    )

    # Idempotent provisioning: testcontainers hands out a fresh DB per session, but
    # the containerized flow reuses the persistent compose `postgres`, where roles/
    # DBs may already exist (e.g. after `setup-db`). autocommit=True means a failed
    # CREATE doesn't poison the connection, so ignore "already exists".
    # duplicate_object / duplicate_database / duplicate_table SQLSTATEs.
    _dup_sqlstates = {"42710", "42P04", "42P07"}

    def _ignore_dup(conn, statement) -> None:
        try:
            conn.execute(statement)
        except Exception as e:  # psycopg.Error; keyed by SQLSTATE to stay stub-agnostic
            if getattr(e, "sqlstate", None) not in _dup_sqlstates:
                raise

    admin_url = pg_container.get_connection_url(driver=None)
    with psycopg.connect(admin_url, autocommit=True) as admin:
        for cfg in services._DATABASE_CONFIGS.values():
            _ignore_dup(admin, SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                Identifier(cfg["user"]), Literal(cfg["password"])))
            _ignore_dup(admin, SQL("CREATE DATABASE {} OWNER {}").format(
                Identifier(cfg["database"]), Identifier(cfg["user"])))
        _ignore_dup(admin, SQL("CREATE DATABASE {}").format(Identifier(_RAG_DB)))
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
        standard_connections(provider="anthropic", embedder=DefaultEmbedder(MockSentenceTransformer())) as conns,
        async_tool_context(),
    ):
        content = await conns.uploader.get_document(scenario / "system.md")
        assert content is not None
        source = SourceCode(
            content=content, project_root=str(scenario),
            contract_name=SolidityIdentifier(_PROGRAM),
            relative_path=f"programs/{_PROGRAM}/src/lib.rs", forbidden_read=RUST_FORBIDDEN_READ,
        )
        _tiered = get_provider_for(tiered=cast(Any, args))
        model_provider = ModelProvider(
            heavy_model=_tiered.heavy, lite_model=_tiered.lite, checkpointer=conns.checkpointer,
        )
        basic = build_basic_source_tools(root=str(scenario), forbidden_read=RUST_FORBIDDEN_READ)
        full = build_source_tools(basic, model_provider, conns.indexed_store, ("crucible_e2e", "src"), recursion_limit=100)
        env = PureServiceHost(models=model_provider, rag_tools=(), sort="existing").bind_source_tools(full)
        ctx = WorkflowContext.create(
            services=conns.memory,
            thread_id="crucible_e2e", store=conns.store, recursion_limit=100,
            cache_namespace=None, memory_namespace=None,
        )

        handler = GenericRustConsoleHandler({"fuzz_pulse", "fuzz_finding", "build_output"})

        # The whole vertical via the generic host — `console-crucible` drives exactly this (the
        # wheel's workspace_prep builds the .so + warms deps; setup authors the fixture; per-unit
        # validate fuzzes). Mirror what the entry point does: thread the fuzz budget in as a
        # declared arg and build the launcher policy from the wheel's sandbox grants.
        app = build_application("crucible_app", command_timeout_s=1800)
        app.options.declared_args = {"fuzz_timeout": 12}
        grants = json.loads(app.module.sandbox_grants("{}"))
        app.options.sandbox = SandboxConfig(
            provider=os.environ.get("COMPOSER_SANDBOX_PROVIDER", "launcher"),
            extra_ro=tuple(Path(p) for p in grants.get("extra_ro", [])),
        )
        result = await run_application(
            app, source, ctx, handler.make_handler, env,
            max_concurrent=2, max_bug_rounds=1, interactive=False,
        )

    print(f"\nCrucible E2E: {result.n_components} invariant(s), {result.n_properties} properties")
    for o in result.outcomes:
        verdicts = o.result.result.verdicts if isinstance(o.result, Delivered) else "(not delivered)"
        print(f"  == {o.feat.display_name} == delivered={isinstance(o.result, Delivered)} verdicts={verdicts}")
    if result.failures:
        print("failures:", result.failures)

    # The front half ran (instructions analyzed, properties extracted) ...
    assert result.n_components > 0, "no invariants extracted"
    assert result.n_properties > 0, "no properties extracted"
    # ... and at least one component was formalized into a fuzz-verdicted test.
    delivered = [o for o in result.outcomes if isinstance(o.result, Delivered)]
    assert delivered, f"no component was delivered; failures={result.failures}"
    assert any(o.result.result.verdicts for o in delivered), "no fuzz verdicts produced"
