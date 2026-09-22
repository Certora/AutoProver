"""The CVLR smoke gate: the whole pipeline over a Solana program with no real LLM calls.

``docs/cvlr-backend-plan.md`` §6 names this as a gate the backend did not have. The EVM and Foundry
pipelines each have one; this is CVLR's, and it is the same trade they make — **the tape fakes the
LLM and only the LLM.** The cargo builds are real, the confinement is real, and the prover job is a
real cloud submission, which is why this is still marked ``expensive``. Read that marker as "slow
and infrastructural" rather than "costly": we own the Prover, so a job costs wall clock and almost
no money, while the LLM calls the tape removes are the dominant dollar cost of a run.

**What it does and does not gate.** Lanes are keyed by ``run_task`` task id, never by prompt content
(:class:`composer.testing.harness_tape.HarnessFakeLLM` serves a lane in order and never looks at what
it was asked). So editing a system prompt replays the old responses against the new prompt and
passes: this is a *plumbing* gate. What it catches is the pipeline taking a different shape — a phase
that stops running, one that starts, an extra model call where there was none, a tool that now errors
where it used to succeed — and on this backend that is most of what breaks.

The run goes through :func:`composer.testing.cvlr_tape.run_scenario`, which is the same function the
recorder drives, so the replay cannot be configured differently from the recording. Everything about
the scenario lives there; nothing about it is restated here.

Run with::

    env -u CERTORA uv run --no-sync pytest tests/test_cvlr_tape.py -m expensive -q -s

Re-record with ``scripts/record_cvlr_tape.sh`` when the pipeline's shape changes on purpose.
"""

import importlib
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from composer.cargo.sbf import PLATFORM_TOOLS_ROOT, platform_tools_installed
from composer.diagnostics.timing import RunSummary
from composer.pipeline.ptypes import Curtailed, Delivered
from composer.rustapp.frontend import GenericRustConsoleHandler
from composer.spec.cvlr.conf import PLATFORM_TOOLS_VERSION
from composer.spec.cvlr.harness import DELIVERABLE_DIR
from composer.spec.cvlr.rules import rule_names
from composer.testing.cvlr_tape import TAPE_NAME, run_scenario, stage_scenario

from tests.conftest import MockSentenceTransformer, needs_postgres

pytestmark = [pytest.mark.expensive, needs_postgres, pytest.mark.asyncio]

_TAPE_MODULE = f"composer.testing.ui_harness_{TAPE_NAME}"


def _install_tape(monkeypatch) -> None:
    """Route the pipeline's models to the tape, and close the two gaps a taped run leaves open.

    The embedder is swapped for the deterministic mock — nothing in a taped run depends on real
    embeddings, and loading the real transformer costs hundreds of megabytes for no reason. Note it
    is patched at ``composer.pipeline.cli``, where it was imported by name; the *provider* lookup
    needs no such fixup, because that one is reached through its module precisely so a tape can
    replace it.

    And the report phase is flipped into re-raise. It is best-effort in production — a grouping
    failure degrades to a single bucket and the outer guard logs and continues — which in a test
    means a missing or mis-keyed ``report`` lane passes silently while exercising the fallback.
    """
    importlib.import_module(_TAPE_MODULE).install_harness_tape()
    monkeypatch.setattr("composer.pipeline.cli.get_model", MockSentenceTransformer)
    monkeypatch.setattr("composer.spec.source.report.build.RERAISE_REPORT_FAILURES", True)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    if importlib.util.find_spec(_TAPE_MODULE) is None:
        pytest.skip(f"no tape at {_TAPE_MODULE} — record one with scripts/record_cvlr_tape.sh")
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not on PATH")
    wanted = PLATFORM_TOOLS_VERSION
    if not platform_tools_installed(wanted):
        pytest.skip(f"Solana platform tools {wanted} are not installed under {PLATFORM_TOOLS_ROOT}")
    return stage_scenario(tmp_path)


async def test_the_taped_vault_run_reaches_a_published_harness(
    langgraph_db, project, cvlr_confinement, monkeypatch, capsys
):
    """Pass == the pipeline ran start to finish on the tape and published something.

    Deliberately few assertions about *content*: the tape decides what the model says, so asserting
    on the rules would be asserting that a recording is still itself. What is worth asserting is
    that the run reached the end — a tape that has drifted fails inside
    :class:`~composer.testing.harness_tape.HarnessFakeLLM` with the lane and the prompt that
    diverged, which is a better message than any check here would produce.
    """
    _install_tape(monkeypatch)

    summary = RunSummary()
    result = await run_scenario(
        project, summary, GenericRustConsoleHandler(set()).make_handler
    )

    with capsys.disabled():
        print(f"\n{summary.format()}")
        print(
            f"taped CVLR run: {result.n_components} component(s), {result.n_properties} "
            f"propert(ies), {result.n_delivered} delivered"
        )

    assert result.n_properties > 0, "the tape's extraction lane produced no properties"
    assert result.n_delivered > 0, f"no unit published a harness: {result.failures}"

    # Every rule a delivered harness claims must exist in the module it shipped. The publish gate
    # enforces this, so a violation here means the gate did not run — which is exactly the kind of
    # shape change a plumbing gate exists to catch, and the kind a taped run can still see.
    for outcome in result.outcomes:
        published = outcome.result
        if isinstance(published, Curtailed):
            published = published.partial
        if not isinstance(published, Delivered):
            continue
        harness = published.result
        claimed = {rule for _, rules in harness.property_checks() for rule in rules}
        assert claimed <= set(rule_names(harness.harness)), (
            f"{outcome.feat.display_name}: {claimed - set(rule_names(harness.harness))} claimed but "
            f"not declared"
        )


async def test_the_taped_run_writes_a_report_that_marks_its_builds(
    langgraph_db, project, cvlr_confinement, monkeypatch
):
    """The report is written, and it records how the builds behind its verdicts ran (§7.8.4).

    Worth its own test rather than a line in the one above: the report phase is the last thing a run
    does and the only one that is best-effort in production, so it is the phase most likely to be
    silently absent. And ``build_environment`` is the field whose *absence* must never be read as
    reassurance, which makes "it is present and it agrees with the sandbox this run used" the whole
    assertion.
    """
    _install_tape(monkeypatch)

    await run_scenario(project, RunSummary(), GenericRustConsoleHandler(set()).make_handler)

    report = json.loads((project / DELIVERABLE_DIR / "reports" / "report.json").read_text())
    environment = report["build_environment"]
    assert environment is not None, "a backend that compiles the project must answer for its builds"
    if cvlr_confinement.enabled:
        assert environment == {"kind": "confined", "provider": cvlr_confinement.provider}
    else:
        assert environment == {"kind": "unconfined"}
