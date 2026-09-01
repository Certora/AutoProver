"""Phase-4's exit criterion: the CVLR backend authors rules for a real program, end to end.

``docs/cvlr-backend-plan.md`` §7.5 built the authoring loop and unit-tested every deterministic part
of it, but nothing had ever authored a rule against a live prover. This is that run: the shared
driver over the SOLANA ecosystem and a real :class:`~composer.spec.cvlr.pipeline.CvlrBackend`, with
real models, a real cargo toolchain and real cloud submissions. Everything the loop gates on is the
genuine article; the only thing stubbed is the embedder, which nothing here depends on.

**Why this scenario.** ``test_scenarios/solana_vault_idl`` is the Anchor vault written for the
Crucible fuzzer's IDL path, reused verbatim. Its "IDL" name is a Crucible concern and means nothing
to CVLR — what makes it the right target here is a coincidence of that choice: it pins an Anchor
major that resolves ``solana-account-info`` 2.x, which is the platform generation the CVLR reference
set is bound to. Its sibling ``solana_vault`` pins Anchor 1.x, lands on the v3 split, and the
scaffold refuses it (see ``test_cvlr_scaffold``'s witness tests) — correctly, and only since the
generation gate learned to read the absence of ``solana-program`` as evidence rather than silence.

**What passing means, and what it does not.** The assertions are deliberately weak: properties were
extracted, at least one unit delivered a harness, and every rule it claims is a rule that exists.
Whether the rules are *good* is not a thing an assertion can settle, so the run prints them and the
verdicts, and a human reads them. That is the same standard ``test_solana_gate`` holds itself to,
and for the same reason.

Marked ``expensive``: real LLM spend, containers, and cloud prover jobs. Run with::

    source ~/.autoProverAnthropicApiKey.sh && \\
        env -u CERTORA uv run --no-sync pytest tests/test_cvlr_gate.py -m expensive -q -s

``env -u CERTORA`` is not optional — see :func:`_shipped_cli_only`.
"""

import asyncio
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from composer.cargo.sbf import PLATFORM_TOOLS_ROOT, platform_tools_installed
from composer.input.types import DEFAULT_RECURSION_LIMIT
from composer.llm.registry import get_provider_for
from composer.pipeline.core import run_pipeline
from composer.pipeline.ecosystem import RUST_FORBIDDEN_READ, SOLANA
from composer.pipeline.ptypes import PipelineRun
from composer.prover.core import make_prover_options
from composer.rag.models import DefaultEmbedder
from composer.rustapp.frontend import GenericRustConsoleHandler
from composer.sandbox.config import SandboxConfig
from composer.spec.context import SourceCode, WorkflowContext
from composer.spec.cvlr.conf import TEMPLATE_BASE, tools_version
from composer.spec.cvlr.harness import CvlrArtifactStore
from composer.spec.cvlr.pipeline import CvlrBackend
from composer.spec.cvlr.rules import rule_names
from composer.spec.service_host import ModelProvider, PureServiceHost
from composer.spec.source.source_env import build_basic_source_tools, build_source_tools
from composer.spec.types import RustIdentifier
from composer.ui.tool_display import async_tool_context
from composer.workflow.services import standard_connections

from tests.conftest import MockSentenceTransformer, needs_postgres

pytestmark = [pytest.mark.expensive, needs_postgres, pytest.mark.asyncio]

_SCENARIO = Path(__file__).parent.parent / "test_scenarios" / "solana_vault_idl"

#: The verified package inside the scenario's workspace.
_PACKAGE = "vault"


def _shipped_cli_only() -> None:
    """Refuse to run against a Certora source checkout.

    ``$CERTORA`` makes :func:`composer.certora_env.import_prover_entry` import the CLI from a local
    Prover build, and such a build reports itself as "no package installed" — so every submission is
    rejected *before upload*, naming neither this variable nor itself. The loop would read that as a
    build failure and spend its whole budget rewriting rules that were never wrong."""
    if os.environ.get("CERTORA"):
        pytest.skip(
            "$CERTORA points this run at a Prover source checkout, which refuses every cloud "
            "submission before upload. Rerun with `env -u CERTORA`."
        )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throwaway copy of the scenario.

    A copy because the run *writes* to it: the preflight scaffolds the workspace, and each unit gets
    its own workdir under it. Scaffolding a checked-in scenario in place would leave the repo dirty
    with generated harness files, which is exactly the kind of test people learn not to run."""
    _shipped_cli_only()
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not on PATH")
    wanted = tools_version(dict(TEMPLATE_BASE))
    if wanted is not None and not platform_tools_installed(wanted):
        pytest.skip(f"Solana platform tools {wanted} are not installed under {PLATFORM_TOOLS_ROOT}")
    destination = tmp_path / _SCENARIO.name
    shutil.copytree(_SCENARIO, destination)
    return destination


def _model_args() -> object:
    return SimpleNamespace(
        heavy_model="claude-opus-4-6",
        lite_model="claude-sonnet-4-6",
        tokens=128_000,
        thinking_tokens=2048,
        memory_tool=False,
        interleaved_thinking=False,
    )


async def test_the_backend_authors_cvlr_rules_for_the_vault(langgraph_db, project, capsys):
    assert (project / "programs" / _PACKAGE / "Cargo.toml").is_file(), project

    model = MockSentenceTransformer()  # nothing here searches by vector; the corpus is optional
    tiered = get_provider_for(tiered=cast(Any, _model_args()))
    async with (
        standard_connections(
            provider=tiered.provider_service, embedder=DefaultEmbedder(model)
        ) as conns,
        async_tool_context(),
    ):
        content = await conns.uploader.get_document(project / "system.md")
        assert content is not None
        source = SourceCode(
            content=content,
            project_root=str(project),
            contract_name=RustIdentifier(_PACKAGE),
            relative_path=f"programs/{_PACKAGE}/src/lib.rs",
            forbidden_read=RUST_FORBIDDEN_READ,
        )
        models = ModelProvider(
            heavy_model=tiered.heavy, lite_model=tiered.lite, checkpointer=conns.checkpointer
        )
        basic = build_basic_source_tools(root=str(project), forbidden_read=RUST_FORBIDDEN_READ)
        # ``library_source`` is deliberately left unset. It would mount the CVLR sources for the
        # code explorer (§5.5), but the crates are not resolved until preflight runs, which is after
        # this. The backend mounts them for the *author* instead, from ``prepare_system``, where the
        # resolved graph is known — so the reading that matters is covered and only the explorer's
        # broad-read shortcut is missing.
        full = build_source_tools(
            basic,
            models,
            conns.indexed_store,
            ("cvlr_gate", "src"),
            recursion_limit=DEFAULT_RECURSION_LIMIT,
            ecosystem=SOLANA,
        )
        # rag_tools=() on purpose for this first run: the corpus is declared optional by design
        # (composer.tools.rag_env degrades to the static cheat-sheet), so running without it first
        # measures the floor. A corpus-backed run is the comparison, not the baseline.
        env = PureServiceHost(models=models, rag_tools=(), sort="existing").bind_source_tools(full)

        ctx = WorkflowContext.create(
            services=conns.memory,
            thread_id="cvlr_gate",
            store=conns.store,
            # Production's default, not a smaller test-scale number. The authoring loop is not the
            # front half: a unit that compiles, submits, reads a counterexample and revises spends
            # graph steps at a rate `test_solana_gate`'s analysis-only run never approaches. At 100
            # every unit here exhausted the limit mid-iteration with a compiling draft on disk, and
            # a recursion abort — unlike a budget stop — discards it rather than curtailing it.
            recursion_limit=DEFAULT_RECURSION_LIMIT,
            cache_namespace=None,
            memory_namespace=None,
        )
        backend = CvlrBackend(
            artifact_store=CvlrArtifactStore(str(project), Path("programs") / _PACKAGE),
            prover_opts=make_prover_options(cloud=True, app="solana"),
            sandbox=SandboxConfig.from_env(),
            package=_PACKAGE,
        )
        run = PipelineRun(
            ctx=ctx,
            source=source,
            _handler_factory=GenericRustConsoleHandler(set()).make_handler,
            _agent_semaphore=asyncio.Semaphore(4),
            _cpu_semaphore=asyncio.Semaphore(2),
            env=env,
        )
        result = await run_pipeline(
            backend, run, ecosystem=SOLANA, interactive=False, threat_model=None, max_bug_rounds=1
        )

    with capsys.disabled():
        print(
            f"\nCVLR gate: {result.n_components} component(s), {result.n_properties} propert(ies), "
            f"{result.n_delivered} delivered"
        )
        for outcome in result.outcomes:
            print(f"\n== {outcome.feat.display_name} ==")
            harness = getattr(outcome.result, "value", None)
            if harness is None:
                print(f"  no deliverable: {outcome.result}")
                continue
            print(f"  {harness.final_link or 'no prover link'}")
            for title, rules in harness.property_checks():
                print(f"  [{title}] -> {', '.join(rules) or '(none)'}")
            for skip in harness.skipped:
                print(f"  skipped [{skip.title}]: {skip.reason}")

    assert result.n_properties > 0, "no properties extracted"
    assert result.n_delivered > 0, f"no unit delivered a harness: {result.failures}"

    # The one thing that *is* mechanically checkable about rule quality: every rule a delivered
    # harness claims must actually be declared in the source it shipped. The publish gate enforces
    # this, so a violation here means the gate did not run, not that the model misbehaved.
    for outcome in result.outcomes:
        harness = getattr(outcome.result, "value", None)
        if harness is None:
            continue
        declared = set(rule_names(harness.harness))
        claimed = {rule for _, rules in harness.property_checks() for rule in rules}
        assert claimed <= declared, f"{outcome.feat.display_name}: {claimed - declared} not declared"
