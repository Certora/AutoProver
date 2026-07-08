"""Phase-4 gate: per-component test authoring + fuzzing + verdict, with a REAL model.

Drives the crucible wheel's `new_session` (per-component) decider on one vault
instruction: the agent authors a `#[invariant_test]`/`#[crucible_fuzz]` fn against a
fixed known-good `Fixture`, the loop runs `crucible run … --mode explore --timeout`,
and bakes a verdict (clean run to budget = GOOD; a `[FUZZ_FINDING]` = BAD). Pass =
the session publishes a compiling test with a GOOD verdict on the clean vault.

Uses a fixed fixture (Phase 3 already gates fixture *authoring*) to isolate the
per-component loop. Heavy + paid; same prerequisites as the other crucible gates.

    CRUCIBLE_REPO=/path/to/crucible \
      .venv/bin/python -m pytest tests/test_crucible_formalize_gate.py -m expensive -q -s
"""

from __future__ import annotations

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
from composer.crucible.harness import CrucibleDep
from composer.crucible.store import CrucibleArtifactStore
from composer.io.multi_job import TaskInfo
from composer.kb.knowledge_base import DefaultEmbedder
from composer.pipeline.core import PipelineRun
from composer.pipeline.ecosystem import RUST_FORBIDDEN_READ
from composer.rustapp.adapter import RealEffects
from composer.rustapp.frontend import GenericRustConsoleHandler
from composer.rustapp.host import build_phase_enum, load_descriptor, load_module
from composer.rustapp.loop import GaveUp, RustFormalized, drive_session
from composer.spec.context import SourceCode, WorkflowContext
from composer.spec.service_host import ModelProvider, PureServiceHost
from composer.spec.solana.build import build_program
from composer.spec.source.source_env import build_basic_source_tools, build_source_tools
from composer.spec.system_model import SolidityIdentifier
from composer.ui.tool_display import async_tool_context
from composer.workflow.services import llm_factory, standard_connections
from graphcore.tools.memory import async_memory_tool

from tests.conftest import MockSentenceTransformer, needs_postgres
from tests.test_autoprove_integration import _MEMORIES_DDL, _RAG_DB, _VECTOR_DBS, _db_url

if TYPE_CHECKING:
    from testcontainers.postgres import PostgresContainer

pytestmark = [pytest.mark.expensive, needs_postgres, pytest.mark.asyncio]

_SCENARIO = Path(__file__).parent.parent / "test_scenarios" / "solana_vault"
_PROGRAM = "vault"
_SLUG = "deposit"
_FEATURE = f"c_{_SLUG}"

# A fixed, known-good shared fixture (`struct Fixture`) — the per-component decider
# authors a test *against* it. Mirrors the Phase-1 harness, renamed to `Fixture`.
_FIXTURE = """\
use crucible_fuzzer::*;
use crucible_fuzzer::anchor_lang::system_program;
use vault::*;
use solana_keypair::Keypair;
use solana_pubkey::Pubkey;
use solana_signer::Signer;
use std::rc::Rc;

const INITIAL_BALANCE: u64 = 10_000_000_000;

#[derive(Clone)]
struct Fixture {
    ctx: TestContext,
    program_id: Pubkey,
    authority: Rc<Keypair>,
    vault_pda: Pubkey,
}

#[fuzz_fixture]
impl Fixture {
    pub fn setup() -> Self {
        let mut ctx = TestContext::new();
        let program_id = Pubkey::new_from_array(ID.to_bytes());
        ctx.add_program(&program_id, "../../target/deploy/vault.so").unwrap();

        let authority = Rc::new(Keypair::new());
        ctx.create_account()
            .pubkey(authority.pubkey())
            .lamports(INITIAL_BALANCE)
            .owner(system_program::ID)
            .create()
            .unwrap();

        let (vault_pda, _) =
            Pubkey::find_program_address(&[b"vault", authority.pubkey().as_ref()], &program_id);

        ctx.program(program_id)
            .call(instruction::Initialize {})
            .accounts(accounts::Initialize {
                vault: vault_pda,
                authority: authority.pubkey(),
                system_program: system_program::ID,
            })
            .signers(&[&*authority])
            .send()
            .expect("initialize must succeed during setup");

        Self { ctx, program_id, authority, vault_pda }
    }

    pub fn action_deposit(&mut self, #[range(1..1_000_000)] amount: u64) -> bool {
        self.ctx.program(self.program_id)
            .call(instruction::Deposit { amount })
            .accounts(accounts::Deposit {
                vault: self.vault_pda,
                depositor: self.authority.pubkey(),
                system_program: system_program::ID,
            })
            .signers(&[&*self.authority])
            .send().map(|o| o.is_success()).unwrap_or(false)
    }

    pub fn action_withdraw(&mut self, #[range(1..1_000_000)] amount: u64) -> bool {
        self.ctx.program(self.program_id)
            .call(instruction::Withdraw { amount })
            .accounts(accounts::Withdraw {
                vault: self.vault_pda,
                authority: self.authority.pubkey(),
            })
            .signers(&[&*self.authority])
            .send().map(|o| o.is_success()).unwrap_or(false)
    }
}
"""

_COMPONENT = {
    "program": "vault",
    "instruction": {
        "name": "deposit",
        "args": ["amount: u64"],
        "description": "Transfer `amount` lamports into the vault PDA and increase the recorded balance.",
    },
}
_PROPS = [
    {
        "title": "recorded balance never exceeds deposited funds",
        "sort": "invariant",
        "description": "The vault's recorded `balance` never exceeds the authority's initial funds "
        "(a conservation bound: you cannot have more recorded than could have been deposited).",
    },
]


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


async def test_crucible_per_component_formalize(pg_container: "PostgresContainer", monkeypatch):
    _require(_SCENARIO.is_dir(), f"scenario missing: {_SCENARIO}")
    _require(shutil.which("cargo-build-sbf") is not None, "cargo-build-sbf not on PATH")
    _require(shutil.which("crucible") is not None, "crucible CLI not on PATH")
    crucible_repo = _crucible_repo()
    _require(crucible_repo is not None, "set CRUCIBLE_REPO to a local crucible checkout")
    assert crucible_repo is not None

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
    built = await build_program(_SCENARIO, _PROGRAM, timeout_s=590)
    assert built.so_path.is_file(), built.so_path

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
        full = build_source_tools(basic, model_provider, conns.indexed_store, ("crucible_fmz", "src"), recursion_limit=100)
        env = PureServiceHost(models=model_provider, rag_tools=(), sort="existing").bind_source_tools(full)
        ctx = WorkflowContext.create(
            services=lambda ns: async_memory_tool(conns.memory(ns)),
            thread_id="crucible_fmz", store=conns.store, recursion_limit=100,
            cache_namespace=None, memory_namespace=None,
        )

        # Pre-place the manifest with the component's feature; the decider writes main.rs.
        dep = CrucibleDep(crucible_repo=crucible_repo, program_crate=_PROGRAM, program_rel=f"../../programs/{_PROGRAM}")
        store = CrucibleArtifactStore(str(_SCENARIO), program=_PROGRAM, dep=dep)
        store.harness.fixture_source = _FIXTURE
        store.harness.write_manifest(store.fuzz_dir(), (_FEATURE,))

        module = load_module("crucible_app")
        phase = build_phase_enum(load_descriptor(module))
        session = module.new_session(json.dumps({
            "label": "deposit", "component": _COMPONENT, "props": _PROPS,
            "config": {"fixture": _FIXTURE, "slug": _SLUG, "program": _PROGRAM, "fuzz_timeout": 15},
        }))

        run = PipelineRun(ctx, env, source, GenericRustConsoleHandler(set()).make_handler, asyncio.Semaphore(2))
        effects = RealEffects(cast(Any, ctx), run, command_timeout_s=1200)
        result = await run.runner(
            TaskInfo("crucible_fmz", "Harness Authoring", phase["formalization"]),
            lambda: drive_session(session, effects, max_steps=60),
        )

    if isinstance(result, GaveUp):
        pytest.fail(f"per-component formalize gave up: {result.reason}")
    assert isinstance(result, RustFormalized)
    test_src = result.data.get("artifact_text", "")
    verdicts = result.data.get("verdicts", {})
    print("\n===== authored test =====\n" + test_src)
    print("verdicts:", verdicts)
    # A compiling test that fuzzed to the timeout on the clean vault → GOOD.
    assert "#[invariant_test]" in test_src or "#[crucible_fuzz]" in test_src
    assert verdicts.get(_FEATURE, {}).get("outcome") == "GOOD", verdicts
    # property → this test's unit is recorded for the report.
    units = dict(result.data.get("property_units", []))
    assert _FEATURE in units.get(_PROPS[0]["title"], [])
