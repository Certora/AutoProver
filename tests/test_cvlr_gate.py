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
from composer.spec.cvlr.harness import CvlrArtifactStore, GeneratedHarness
from composer.spec.cvlr.pipeline import CvlrBackend
from composer.pipeline.ptypes import Curtailed, Delivered
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


def _harness(result: object) -> GeneratedHarness | None:
    """The published harness inside an outcome, or ``None`` if the unit delivered nothing.

    ``Delivered`` carries it as ``result``, and a budget-curtailed publish wraps a ``Delivered`` in a
    ``Curtailed``. Written out rather than reached for with ``getattr(..., "value")``, which silently
    returned ``None`` for every unit in a run where all three had in fact published — the report read
    "no deliverable" three times while the deliverables sat on disk.
    """
    if isinstance(result, Curtailed):
        result = result.value
    return result.result if isinstance(result, Delivered) else None


def _verifies_only_its_own_code(harness: GeneratedHarness) -> bool:
    """Whether this harness has rules and *none* of them drives the program.

    ``rule_subjects`` is the author's per-rule statement of what a rule drives: a program function,
    or harness-local code standing in for one. A harness whose every rule is a stand-in is by
    construction not evidence about the program, and that is what this catches.

    **A harness with no rules at all is not that**, and an earlier version of this predicate failed
    it. It asked "does any rule drive the program", which answers *no* both for a harness of pure
    mirrors and for one that concluded nothing was provable and skipped every property with a
    reason. Those are opposite outcomes: the first is worthless, the second is the best available
    answer to P6 (``docs/upstream-defects.md``) and is what the loop was pushed toward for three
    runs. A run then produced it — seven skips, each naming the CPI havoc, the summaries it tried and
    the absent accounting core — and this test failed the run for it. Hence the inversion: the
    question is whether any rule *fails* to reach the program, not whether one succeeds.

    An empty harness only reaches here having accounted for every property: the publish gate refuses
    a result unless each is mapped to a rule or explicitly skipped.

    This also replaced a text scan for ``crate::entry`` / ``crate::<package>``, which was measured
    wrong in both directions. It false-*passed* a harness that says ``crate::VaultState`` while
    driving a reimplementation, and false-*failed* one that does ``use crate::{..., vault_program};``
    then calls ``vault_program::initialize(ctx)``, because the call site carries no ``crate::``
    prefix. Reach is a question about name resolution, and the declaration answers it without this
    test doing the compiler's job.

    Not simply trusted: :func:`_undeclared_functions` fails a declaration the shipped source
    contradicts, and the publish gate refuses a subject rooted outside the program's own crate.
    """
    subjects = harness.rule_subjects
    return bool(subjects) and not any(s.subject == "program_function" for s in subjects)


def _mirrored_rules(harness: GeneratedHarness) -> list[str]:
    """Rules the author declared as driving a stand-in. Reported, not asserted on: a declared mirror
    can be legitimate — [3308] leaves no alternative for some handlers — and the judge is what
    weighs its reason. A reader of this test's output should still see which ones they are."""
    return [s.rule for s in harness.rule_subjects if s.subject != "program_function"]


def _undeclared_functions(harness: GeneratedHarness) -> list[str]:
    """Declared program functions whose name does not appear in the harness source.

    The declaration is the author's claim, so this is the part of it that can be checked cheaply: a
    rule cannot be driving ``crate::vault_program::deposit`` if the shipped module never writes
    ``deposit``. Matched on the final path segment, since the path may be reached through a ``use``.
    """
    code = "\n".join(
        line for line in harness.harness.splitlines()
        if not line.lstrip().startswith(("//", "/*", "*"))
    )
    return [
        fn
        for s in harness.rule_subjects
        if (fn := getattr(s, "function", None)) is not None
        and fn.rsplit("::", 1)[-1] not in code
    ]


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

    # Written to a file as well as printed. ``capsys.disabled()`` restores the stream pytest saved at
    # session start, which is not the one a redirected or piped run is reading — a full gate run's
    # summary was lost that way, and it is the only human-readable output this test produces.
    summary_path = project / "certora" / "cvlr" / "reports" / "gate-summary.txt"

    def emit(text: str) -> None:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        with summary_path.open("a") as handle:
            handle.write(text + "\n")
        with capsys.disabled():
            print(text)

    emit(
        f"\nCVLR gate: {result.n_components} component(s), {result.n_properties} propert(ies), "
        f"{result.n_delivered} delivered"
    )
    for outcome in result.outcomes:
        emit(f"\n== {outcome.feat.display_name} ==")
        harness = _harness(outcome.result)
        if harness is None:
            emit(f"  no deliverable: {outcome.result}")
            continue
        emit(f"  {harness.final_link or 'no prover link'}")
        for title, rules in harness.property_checks():
            emit(f"  [{title}] -> {', '.join(rules) or '(none)'}")
        for skip in harness.skipped:
            emit(f"  skipped [{skip.property_title}]: {skip.reason}")

    assert result.n_properties > 0, "no properties extracted"
    assert result.n_delivered > 0, f"no unit delivered a harness: {result.failures}"

    # The one thing that *is* mechanically checkable about rule quality: every rule a delivered
    # harness claims must actually be declared in the source it shipped. The publish gate enforces
    # this, so a violation here means the gate did not run, not that the model misbehaved.
    for outcome in result.outcomes:
        harness = _harness(outcome.result)
        if harness is None:
            continue
        declared = set(rule_names(harness.harness))
        claimed = {rule for _, rules in harness.property_checks() for rule in rules}
        assert claimed <= declared, f"{outcome.feat.display_name}: {claimed - declared} not declared"

    # The claim this test's name makes. Everything above checks that the harness is internally
    # consistent and that the prover agreed with it — and all of it passes for a harness that
    # reimplements the handler inside the spec module and verifies the copy. That is what happened
    # once: three units published 19 rules between them and not one invoked the vault, two of the
    # three never naming the crate at all. The mapping gate cannot see it, and no verdict-shaped
    # check can, because the verdicts are honest; they are about the wrong program.
    for outcome in result.outcomes:
        harness = _harness(outcome.result)
        if harness is None:
            continue
        phantom = _undeclared_functions(harness)
        assert not phantom, (
            f"{outcome.feat.display_name} declares rules driving {phantom}, but the harness it "
            f"shipped never names them. The declaration is the report's account of what was "
            f"verified, so one the source contradicts is worse than none."
        )
        if mirrors := _mirrored_rules(harness):
            emit(f"  {outcome.feat.display_name}: declared stand-ins — {', '.join(mirrors)}")

    self_verifying = [
        o.feat.display_name
        for o in result.outcomes
        if (h := _harness(o.result)) is not None and _verifies_only_its_own_code(h)
    ]
    assert not self_verifying, (
        f"delivered harnesses whose every rule drives harness-local code: {self_verifying}. Those "
        f"rules verify whatever the harness itself defines, so they are not evidence about "
        f"{_PACKAGE}. A harness with no rules at all is a different outcome and passes: see "
        f"{_verifies_only_its_own_code.__name__}."
    )
