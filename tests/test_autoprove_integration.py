"""End-to-end integration test for the autoprove pipeline.

The LLM is mocked — the hand-authored Counter tape, installed via
``install_harness_tape`` (which also disables the agent-index cache) — and so is
AutoSetup, which makes its own LLM calls inside a subprocess and so can't be
taped; ``_fake_autosetup_phase`` returns a canned ``SetupSuccess`` for Counter.
Everything else runs for real: Postgres (checkpoint / store / memory) in a
testcontainer and the live Certora cloud prover. Given the deterministic tape +
fixed spec/code, the prover is reasonably deterministic. Pass/fail is simply: the
pipeline runs start to finish without raising.

Marked ``expensive`` (live cloud prover + containers + the embedding model load)
and skipped without testcontainers. Run with ``-m expensive``.
"""
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast, TYPE_CHECKING

import psycopg
import pytest
from psycopg.sql import SQL, Identifier, Literal

import composer.workflow.services as services
from composer.diagnostics.timing import RunSummary
from composer.spec.source.autoprove_common import autoprove_executor, AutoProveArgs
from composer.spec.source.autosetup import SetupSuccess
from composer.ui.autoprove_console import AutoProveConsoleHandler
from composer.testing.ui_harness_autoprove_Counter import install_harness_tape

if TYPE_CHECKING:
    from testcontainers.postgres import PostgresContainer


from tests.conftest import needs_postgres, MockSentenceTransformer

pytestmark = [pytest.mark.expensive, needs_postgres, pytest.mark.asyncio]

_SCENARIO = Path(__file__).parent.parent / "test_scenarios" / "autoprove_counter"
_RAG_DB = "rag_db"
# DBs that hold pgvector embeddings and need the extension (the store role's DB
# + the RAG DB); checkpoint/memory are plain.
_VECTOR_DBS = ("langgraph_store_db", _RAG_DB)

# The config AutoSetup produced for Counter on a local run (its outputs aren't
# checked in). DummyERC20Impl is dropped — Counter is standalone and the mock
# generated against it doesn't ship. ``verify`` is overlaid per-spec by
# ``prover_config_overlay`` at run time, so it need not name a spec that exists.
_COUNTER_PROVER_CONFIG = {
    "assert_autofinder_success": True,
    "files": ["src/Counter.sol"],
    "global_timeout": "1200",
    "parametric_contracts": "Counter",
    "prover_args": ["-quiet"],
    "run_source": "AUTO_PROVER",
    "solc": "solc",
    "verify": "Counter:certora/specs/sanity-Counter.spec",
    "wait_for_results": "none",
}
# AutoSetup's summaries spec, relative to certora/ (the SetupSuccess contract).
_SUMMARIES_REL = "specs/summaries/Counter_base_summaries.spec"


async def _fake_autosetup_phase(*_args, **_kwargs) -> SetupSuccess:
    """Stand in for the AutoSetup subprocess, which makes its LLM
    calls that we, in autoprover land, aren't going to start taping.
    It is also not an intersting unit of test for this workflow, so just use the
    trivial, precomputed setups.
    Writes out the (trivial, no-op) summaries spec the generated CVL imports
    against, then returns the config AutoSetup would have produced for Counter."""
    summaries = _SCENARIO / "certora" / _SUMMARIES_REL
    summaries.parent.mkdir(parents=True, exist_ok=True)
    summaries.write_text(
        "// Auto-generated base summaries for Counter\n// No summaries needed for Counter\n"
    )
    return SetupSuccess(
        prover_config=dict(_COUNTER_PROVER_CONFIG),
        summaries_path=_SUMMARIES_REL,
        user_types=[],
    )

# graphcore's Postgres memory backend doesn't self-create its schema; this mirrors
# the memories_fs DDL in graphcore/tests/conftest.py (keep in sync if that moves).
_MEMORIES_DDL = """
CREATE TABLE IF NOT EXISTS memories_fs(
    namespace TEXT NOT NULL,
    entry_name TEXT NOT NULL,
    full_path TEXT,
    parent_path TEXT,
    is_directory BOOL NOT NULL,
    contents TEXT,
    FOREIGN KEY(parent_path, namespace) REFERENCES memories_fs(full_path, namespace) ON DELETE CASCADE,
    UNIQUE (namespace, full_path),
    UNIQUE (namespace, parent_path, entry_name),
    CHECK (parent_path is NOT NULL OR (full_path = '/memories' AND is_directory AND entry_name = 'memories')),
    CHECK (parent_path is NULL OR (full_path = concat(parent_path, '/', entry_name))),
    CHECK (contents IS NOT NULL != is_directory)
);
CREATE INDEX IF NOT EXISTS memories_namespace_path ON memories_fs(namespace, full_path text_pattern_ops);
"""


def _db_url(pg: "PostgresContainer", database: str) -> str:
    return (
        f"postgresql://{pg.username}:{pg.password}"
        f"@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{database}"
    )


def _make_args(rag_conn: str, system_doc: str | None = str(_SCENARIO / "system.md")) -> AutoProveArgs:
    """Hand-built ``AutoProveArgs`` (the CLI path builds this via argparse).

    ``system_doc`` defaults to the scenario's ``system.md``; pass ``None`` to exercise
    the auto-discovery path."""
    return cast(AutoProveArgs, SimpleNamespace(
        project_root=str(_SCENARIO),
        main_contract=f"{_SCENARIO / "src/Counter.sol"}:Counter",
        system_doc=system_doc,
        max_concurrent=4,
        cache_ns=None,
        memory_ns=None,
        cloud=True,
        interactive=False,
        threat_model=None,
        recursion_limit=100,
        max_bug_rounds=1,
        rag_db=rag_conn,
        # Model-config fields: only read through ``get_provider_for(tiered=args)``,
        # which the tape patches to ignore them, so the values are inert — present
        # to satisfy the AutoProveArgs surface.
        heavy_model="fake-heavy",
        lite_model="fake-lite",
        tokens=128_000,
        thinking_tokens=2048,
        memory_tool=False,
        interleaved_thinking=False,
    ))

@pytest.fixture(scope="session")
def provisioned_rag_url(pg_container: "PostgresContainer | None") -> str:
    """Stand up the fixed-name roles / databases / extensions / schema the autoprove
    pipeline connects to, once per session; return the RAG DB url.

    These database names *and* login roles are hardcoded in
    ``services._DATABASE_CONFIGS`` — only host/port are overridable (redirected
    per-test in ``_install_mocks``). The pipeline therefore always talks to these same
    databases: there is nothing per-test to make unique, so they are created once,
    unconditionally. No other fixture creates these fixed names, so there is nothing to
    collide with (unlike ``get_test_database``'s per-invocation ``test_store_<uuid>``).

    Per-test isolation comes from namespacing, not fresh databases: each run uses a
    unique ``thread_id`` (checkpoint/store) and ``memory_ns``, with caching off, so the
    two integration tests don't step on each other despite sharing these DBs."""
    if pg_container is None:
        pytest.skip("No pgcontainers")
    # Roles + databases matching services._DATABASE_CONFIGS (a login role owning its
    # own DB), so the real connection-string path works unpatched.
    admin_url = pg_container.get_connection_url(driver=None)
    with psycopg.connect(admin_url, autocommit=True) as admin:
        for cfg in services._DATABASE_CONFIGS.values():
            admin.execute(SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                Identifier(cfg["user"]), Literal(cfg["password"])))
            admin.execute(SQL("CREATE DATABASE {} OWNER {}").format(
                Identifier(cfg["database"]), Identifier(cfg["user"])))
        admin.execute(SQL("CREATE DATABASE {}").format(Identifier(_RAG_DB)))
    # pgvector must be installed by a superuser, so do it on the admin connection.
    for db in _VECTOR_DBS:
        with psycopg.connect(_db_url(pg_container, db), autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # The memory backend doesn't self-create its schema (the checkpointer/store do,
    # via .setup()), so create memories_fs as the memory role (its DB owner), not the
    # superuser — the backend connects as that role and must own the table.
    mem = services._DATABASE_CONFIGS["memory"]
    mem_url = (
        f"postgresql://{mem['user']}:{mem['password']}"
        f"@{pg_container.get_container_host_ip()}:{pg_container.get_exposed_port(5432)}/{mem['database']}"
    )
    with psycopg.connect(mem_url, autocommit=True) as conn:
        conn.execute(_MEMORIES_DDL)
    return _db_url(pg_container, _RAG_DB)


def _install_mocks(pg_container: "PostgresContainer", monkeypatch) -> None:
    """Function-scoped connection redirection + LLM/AutoSetup mocks (undone per test by
    ``monkeypatch``). The databases themselves are stood up once by the
    ``provisioned_rag_url`` session fixture."""
    # Only host/port need redirecting — the role creds already match the configs.
    monkeypatch.setenv("CERTORA_AI_COMPOSER_PGHOST", pg_container.get_container_host_ip())
    monkeypatch.setenv("CERTORA_AI_COMPOSER_PGPORT", str(pg_container.get_exposed_port(5432)))

    # Mock only the LLM (Counter tape) + disable the agent-index cache.
    install_harness_tape(with_delay=False)
    # autoprove_common imported `get_provider_for` by name, so install_harness_tape's
    # patch of registry.get_provider_for doesn't reach that binding — rebind it here.
    import composer.llm.registry as registry
    monkeypatch.setattr(
        "composer.spec.source.autoprove_common.get_provider_for", registry.get_provider_for
    )
    # Swap the real sentence-transformer for the deterministic mock: no model
    # download, and nothing in this run depends on real embeddings (index cache
    # disabled by the tape, RAG DB empty).
    monkeypatch.setattr(
        "composer.spec.source.autoprove_common.get_model", MockSentenceTransformer
    )
    # AutoSetup runs an LLM in a subprocess we can't tape — swap the phase for a
    # canned Counter SetupSuccess. Patch the name the pipeline imported, not the
    # definition in harness.py.
    monkeypatch.setattr(
        "composer.spec.source.pipeline.run_autosetup_phase", _fake_autosetup_phase
    )
    # The report phase is best-effort and absorbs failures (grouping degrades to a
    # fallback bucket; the outer guard logs-and-continues). Flip both into re-raise
    # so a broken report lane fails this test instead of passing silently.
    monkeypatch.setattr(
        "composer.spec.source.report.build.RERAISE_REPORT_FAILURES", True
    )


def _read_job_info() -> dict:
    """The always-written run manifest the entry point's ``finally`` dumps."""
    return json.loads((_SCENARIO / "certora" / "ap_report" / "job_info.json").read_text())


async def test_autoprove_counter_runs_end_to_end(
    pg_container: "PostgresContainer", provisioned_rag_url: str, monkeypatch
):
    assert _SCENARIO.is_dir(), _SCENARIO
    _install_mocks(pg_container, monkeypatch)
    monkeypatch.setenv("AUTOPROVER_USER_ID", "e2e-user")
    # Run the whole pipeline. Pass == it completes without raising.
    summary = RunSummary()
    async with autoprove_executor(_make_args(provisioned_rag_url), summary) as run:
        await run(AutoProveConsoleHandler().make_handler)

    # The finally dumped the run manifest to ap_report, carrying this run's identity
    # and its (non-empty, since the pipeline ran the prover) usage totals.
    job_info = _read_job_info()
    assert job_info["user_id"] == "e2e-user"
    assert job_info["run_id"] == summary.run_id
    assert "token_usage" in job_info and "prover_usage" in job_info


async def test_autoprove_dumps_job_info_when_pipeline_crashes(
    pg_container: "PostgresContainer", provisioned_rag_url: str, monkeypatch
):
    """The core guarantee: job_info.json is written even when the run crashes. Patch the
    pipeline to blow up after the executor is set up; the entry-point ``finally`` must
    still land the manifest (with this run's identity) in ap_report."""
    assert _SCENARIO.is_dir(), _SCENARIO
    _install_mocks(pg_container, monkeypatch)
    monkeypatch.setenv("AUTOPROVER_USER_ID", "crash-user")

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(
        "composer.spec.source.autoprove_common.run_autoprove_pipeline", _boom
    )

    summary = RunSummary()
    with pytest.raises(RuntimeError, match="pipeline exploded"):
        async with autoprove_executor(_make_args(provisioned_rag_url), summary) as run:
            await run(AutoProveConsoleHandler().make_handler)

    job_info = _read_job_info()
    assert job_info["user_id"] == "crash-user"
    assert job_info["run_id"] == summary.run_id


async def test_autoprove_counter_no_doc_runs_end_to_end(
    pg_container: "PostgresContainer", provisioned_rag_url: str, monkeypatch
):
    """Same pipeline, design doc OMITTED: the Design Doc Discovery phase runs the
    finder (its tape lane selects ``system.md``) and the run completes via the
    discovered doc."""
    assert _SCENARIO.is_dir(), _SCENARIO
    _install_mocks(pg_container, monkeypatch)
    summary = RunSummary()
    async with autoprove_executor(_make_args(provisioned_rag_url, system_doc=None), summary) as run:
        await run(AutoProveConsoleHandler().make_handler)
