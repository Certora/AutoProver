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
from composer.rag.models import DefaultEmbedder
from composer.pipeline.core import GaveUp, PipelineRun
from composer.pipeline.ecosystem import RUST_FORBIDDEN_READ
from composer.rustapp.adapter import emit_event, write_wheel_files
from composer.rustapp.session import run_session
from composer.rustapp.wire import ComponentInput, CrateRootInput, Property, parse_files
from composer.rustapp.frontend import GenericRustConsoleHandler
from composer.rustapp.host import build_phase_model, load_descriptor, load_module
from composer.spec.context import SourceCode, WorkflowContext
from composer.spec.service_host import ModelProvider, PureServiceHost
from composer.llm.registry import get_provider_for
from composer.spec.solana.build import build_program
from composer.spec.source.source_env import build_basic_source_tools, build_source_tools
from composer.spec.system_model import SolidityIdentifier
from composer.ui.tool_display import async_tool_context
from composer.workflow.services import standard_connections

from tests.conftest import MockSentenceTransformer, needs_postgres
from tests.conftest import _MEMORIES_DDL, _RAG_DB, _VECTOR_DBS, _db_url

if TYPE_CHECKING:
    from testcontainers.postgres import PostgresContainer

pytestmark = [pytest.mark.expensive, needs_postgres, pytest.mark.asyncio]

_SCENARIO = Path(__file__).parent.parent / "test_scenarios" / "solana_vault"
_PROGRAM = "vault"
_SLUG = "deposit"
_FEATURE = f"c_{_SLUG}"  # this property's check (one per property)
# Every Crucible property shares ONE fuzz target/harness fn (docs/crucible-unit-granularity.md
# §3); its name gates `main` via the `#[invariant_test]` macro. `validate` is called with the
# *target* (as the real pipeline does: `u["target"] or u["unit"]`), not the per-property unit —
# building with any other feature leaves `main` cfg'd out (E0601). Mirrors crucible-app's
# SINGLE_HARNESS_FN.
_TARGET = "c_invariants"
# The one fn name every component's suite defines (crucible-app's `SECTION_FN`), which the crate
# root's generated entry calls into.
_SECTION_FN = "invariants"

# A fixed, known-good shared fixture (`struct Fixture`) — the per-component decider
# authors a test *against* it. Mirrors the Phase-1 harness, renamed to `Fixture`. It names
# `PROGRAM_SO` rather than a path: the wheel declares that constant in the crate root it renders
# around this text, because the path is relative to a harness dir an author cannot see.
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
        ctx.add_program(&program_id, PROGRAM_SO).unwrap();

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
        "component": "vault",
        "title": "recorded balance never exceeds deposited funds",
        "sort": "invariant",
        "slug": _SLUG,  # host-assigned unit slug (the pipeline sets this via unique_slugs)
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
    raw = os.environ.get("CRUCIBLE_REPO")
    if not raw:
        return None
    repo = Path(raw)
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

    # Built BEFORE the connections: `standard_connections` takes the resolved provider
    # service (it asks it for an uploader and the memory tool), not a provider name.
    _tiered = get_provider_for(tiered=cast(Any, args))
    async with (
        standard_connections(
            provider=_tiered.provider_service, embedder=DefaultEmbedder(MockSentenceTransformer())
        ) as conns,
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
            heavy_model=_tiered.heavy, lite_model=_tiered.lite, checkpointer=conns.checkpointer,
        )
        basic = build_basic_source_tools(root=str(_SCENARIO), forbidden_read=RUST_FORBIDDEN_READ)
        full = build_source_tools(basic, model_provider, conns.indexed_store, ("crucible_fmz", "src"), recursion_limit=100)
        env = PureServiceHost(models=model_provider, rag_tools=(), sort="existing").bind_source_tools(full)
        ctx = WorkflowContext.create(
            services=conns.memory,
            thread_id="crucible_fmz", store=conns.store, recursion_limit=100,
            cache_namespace=None, memory_namespace=None,
        )

        module = load_module("crucible_app")
        descriptor = load_descriptor(module)
        phases = build_phase_model(descriptor)

        # The component artifact: author the test(s), `compile` (dry-run), then `validate`
        # the unit (fuzz). Unsandboxed here (trusted inputs), so the argv prefix is empty.
        component_input = ComponentInput(
            program=_PROGRAM,
            unit=_COMPONENT,
            props=[Property.model_validate(p) for p in _PROPS],
            setup=_FIXTURE,
            args={"fuzz_timeout": 15},
        )

        # Place the crate root and manifest, which a per-component gate does NOT write: since
        # sections moved into their own files (docs/crucible-component-units.md §17), `compile` and
        # `validate` emit only `src/<feature>.rs` — the crate root is rendered ONCE from the fixture
        # plus the whole unit set. The pipeline does this in `adapter.write_crate_root` between the
        # setup step and fan-out; this drives the same callout with the same payload. Without it the
        # harness dir holds a section file and no `Cargo.toml`, so every gate fails to build and the
        # author burns its rounds revising a spec that was never the problem.
        #
        # `source_unit`/`prep_facts` are left at their defaults deliberately: `component_input`
        # carries none either, so both callouts derive the same crate (the `programs/<program>`
        # layout convention, crate path, no IDL) and the root matches what `validate` builds.
        write_wheel_files(Path(_SCENARIO), parse_files(module.crate_root(CrateRootInput(
            program=_PROGRAM, setup=_FIXTURE, units=[_COMPONENT],
        ).model_dump_json())))
        input_json = component_input.model_dump_json()
        sandbox_dict = {"argv_prefix": [], "timeout_s": 1200}
        sandbox_json = json.dumps(sandbox_dict)

        run = PipelineRun(ctx=ctx, source=source, _handler_factory=GenericRustConsoleHandler(set()).make_handler, _agent_semaphore=asyncio.Semaphore(4), _cpu_semaphore=asyncio.Semaphore(2), env=env)
        outcome = await run.runner(
            TaskInfo("crucible_fmz", "Harness Authoring", phases.member("formalization")),
            lambda: run_session(
                module=module, input=component_input,
                kind="component", titles=[p["title"] for p in _PROPS], env=env, ctx=ctx, run=run,
                workdir=Path(_SCENARIO), sandbox_dict=sandbox_dict, descriptor=descriptor,
                emit=emit_event, description="Harness Authoring",
            ),
        )
        if isinstance(outcome, GaveUp):
            pytest.fail(f"per-component authoring gave up: {outcome.reason}")
        test_src = outcome.spec

        # Whatever the author named its checks, they all map to the one shared *target*
        # `c_invariants`, whose fn name gates `main` — the grouping the session handed `validate`,
        # asserted here as the stamping run recorded it.
        checks = [c for t in outcome.ran for c in t.checks]
        assert [t.name for t in outcome.ran] == [_TARGET], checks
        # validate is the fused build+fuzz; a clean vault run → GOOD verdicts (not build_failed).
        target = outcome.ran[0].model_dump_json()
        res = json.loads(
            await asyncio.to_thread(module.validate, input_json, test_src, target, str(_SCENARIO), sandbox_json)
        )

    print("\n===== authored test =====\n" + test_src)
    print("checks:", checks, "validate:", res)
    # The authored spec is the section BODY: a bare `pub fn invariants`, which the crate root's
    # generated `#[invariant_test]` entry delegates into. Asserting the attribute is here would
    # contradict the contract in both directions — `test_cheat_sheet.j2` tells the author not to
    # write it (the macro expands `fn main()` as a sibling, so it only works at crate root), and
    # `Section::file` strips it if one is written anyway.
    assert f"pub fn {_SECTION_FN}" in test_src, test_src
    root = (_SCENARIO / f"certora/crucible/fuzz/{_PROGRAM}/src/main.rs").read_text()
    assert f"#[invariant_test]\nfn {_TARGET}(fixture: &mut Fixture)" in root, root
    assert res["kind"] == "verdicts", res
    (check_name, verdict), = res["verdicts"]
    assert verdict["outcome"] == "GOOD", res
    # The author declared it, so the name is its own — what must hold is that the one property is
    # carried by the check that ran, under the one fuzz target.
    assert [(c.name, c.properties, c.target_or_name()) for c in checks] == [
        (check_name, [_PROPS[0]["title"]], _TARGET)
    ]
    assert dict(outcome.property_checks) == {_PROPS[0]["title"]: [check_name]}
