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

**Sub-agents replay from lanes of their own.** The author routinely starts two ``cvlr_research``
calls in one turn, and both researchers run at once. Each is served from the lane named by the tool
call that started it (:func:`composer.testing.harness_tape.tape_lane_path`); sharing the author's
lane, they took each other's turns in whichever order they asked, and a researcher handed a
``result`` before its draft was rejected, shifting the lane under the author.

Both corpora are a prerequisite here as they are for the recording: a replayed search against an
absent or empty corpus is a tool result the recording never saw.
"""

import importlib
import importlib.util
import json
import shutil
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from composer.cargo.sbf import PLATFORM_TOOLS_ROOT, platform_tools_installed
from composer.diagnostics.timing import RunSummary
from composer.pipeline.core import PRIORITIZE_TASK_ID
from composer.pipeline.run_mode import RUN_MODE_ENV, RunMode
from composer.rustapp.frontend import GenericRustConsoleHandler
from composer.spec.cvlr.conf import PLATFORM_TOOLS_VERSION
from composer.spec.gen_types import CVLR_DELIVERABLE_DIR
from composer.spec.prioritize import PropertyGroup, PropertyRanking, RankedProperty
from composer.spec.source.report.grouping import GroupingResult, PropertyGroupDraft
from composer.spec.types import ComponentName, PropertyTitle
from composer.testing.cvlr_tape import (
    TAPE_NAME, missing_corpora, run_scenario, stage_scenario, tape_args,
)
from composer.testing.harness_tape import HarnessFakeLLM

from tests.conftest import MockSentenceTransformer, needs_postgres

pytestmark = [pytest.mark.expensive, needs_postgres, pytest.mark.asyncio]

_TAPE_MODULE = f"composer.testing.ui_harness_{TAPE_NAME}"


def _install_tape(monkeypatch) -> HarnessFakeLLM:
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
    fake = importlib.import_module(_TAPE_MODULE).install_harness_tape()
    monkeypatch.setattr("composer.pipeline.cli.get_model", MockSentenceTransformer)
    monkeypatch.setattr("composer.spec.source.report.build.RERAISE_REPORT_FAILURES", True)
    return fake


@pytest.fixture
def project(tmp_path: Path) -> Path:
    if importlib.util.find_spec(_TAPE_MODULE) is None:
        pytest.skip(f"no tape at {_TAPE_MODULE} — record one with scripts/record_cvlr_tape.sh")
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not on PATH")
    wanted = PLATFORM_TOOLS_VERSION
    if not platform_tools_installed(wanted):
        pytest.skip(f"Solana platform tools {wanted} are not installed under {PLATFORM_TOOLS_ROOT}")
    if missing := missing_corpora(tape_args(tmp_path)):
        pytest.skip(f"corpus {', '.join(missing)} is unreachable or empty — scripts/populate_cvlr_rag.sh")
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

    report = json.loads((project / CVLR_DELIVERABLE_DIR / "reports" / "report.json").read_text())
    environment = report["build_environment"]
    assert environment is not None, "a backend that compiles the project must answer for its builds"
    if cvlr_confinement.enabled:
        assert environment == {"kind": "confined", "provider": cvlr_confinement.provider}
    else:
        assert environment == {"kind": "unconfined"}


#: The tape's extracted properties, by the label the ranker sees them under.
_CANDIDATES: dict[str, list[str]] = {
    "0: Vault Lifecycle": [
        "initialize_records_signer_and_zero_balance",
        "initialize_vault_is_canonical_pda_with_canonical_bump",
        "initialize_cannot_reinitialize_existing_vault",
        "vault_lamports_cover_recorded_balance_plus_rent",
        "vault_authority_and_bump_immutable_after_creation",
        "initialize_reachable_for_fresh_authority",
    ],
    "1: Deposits": [
        "recorded_balance_backed_by_vault_lamports",
        "deposit_credits_exactly_amount_or_fails",
        "deposit_lamports_moved_match_balance_credited",
        "deposit_never_changes_vault_control_or_other_vaults",
        "deposit_permissionless_and_reachable",
        "vault_must_be_canonical_pda_of_its_recorded_authority",
        "credited_deposit_is_withdrawable_by_authority",
    ],
    "2: Withdrawals & Fee Sharing": [
        "vault_lamports_cover_recorded_balance_and_rent",
        "only_signing_recorded_authority_can_withdraw",
        "withdraw_never_exceeds_recorded_balance",
        "withdraw_debits_recorded_balance_by_exactly_amount",
        "withdraw_conserves_lamports_across_payees",
        "vault_lamports_fall_by_exactly_amount_for_any_fee_collector",
        "fee_split_is_the_fixed_design_fraction",
        "vault_authority_and_bump_immutable_after_creation",
        "written_vault_is_canonical_pda_of_its_recorded_authority",
        "withdraw_is_reachable_with_and_without_fee_collector",
    ],
}

#: The withdrawals unit's properties that the comprehensive recording's hoist moved out. What is
#: left is exactly what the recorded ``formalize-2`` lane formalized, so the focus picks that set
#: and the lane replays against the batch it was recorded for.
_HOISTED = {
    "vault_lamports_cover_recorded_balance_and_rent",
    "vault_authority_and_bump_immutable_after_creation",
    "written_vault_is_canonical_pda_of_its_recorded_authority",
}
_FOCUS_LABEL = "2: Withdrawals & Fee Sharing"
_FOCUS = [t for t in _CANDIDATES[_FOCUS_LABEL] if t not in _HOISTED]


def _ranking() -> PropertyRanking:
    """A ranking that puts :data:`_FOCUS` on top as one claim and every other candidate in a group
    of its own."""
    ranked: list[RankedProperty] = []
    groups: list[PropertyGroup] = []
    for label, titles in _CANDIDATES.items():
        for title in titles:
            key = (ComponentName(label), PropertyTitle(title))
            focused = label == _FOCUS_LABEL and title in _FOCUS
            ranked.append(RankedProperty(
                key=key, score=90 if focused else 40, critical_match=False,
                rationale="Withdrawals move the funds." if focused else "Lower stakes.",
            ))
            if not focused:
                groups.append(PropertyGroup(claim=f"{title} holds.", members=[key]))
    groups.append(PropertyGroup(
        claim="Only the authority withdraws, and a withdrawal pays out exactly what it debits.",
        members=[(ComponentName(_FOCUS_LABEL), PropertyTitle(t)) for t in _FOCUS],
    ))
    return PropertyRanking(
        ranked=ranked, groups=groups, justification="Withdrawals are where funds leave.",
    )


def _report_grouping() -> GroupingResult:
    """The report's grouping, over the focused unit's properties alone. The report keys them by the
    component's display name, without the ranker's index."""
    component = ComponentName(_FOCUS_LABEL.split(": ", 1)[1])
    return GroupingResult(groups=[
        PropertyGroupDraft(
            slug=title.replace("_", "-")[:64],
            title=title.replace("_", " "),
            description=f"{title.replace('_', ' ')}.",
            members=[(component, PropertyTitle(title))],
        )
        for title in _FOCUS
    ])


def _structured_reply(result: BaseModel) -> AIMessage:
    """What a structured-output call reads back from the fake: one call of the schema's tool."""
    return AIMessage(content="", tool_calls=[{
        "name": type(result).__name__,
        "args": result.model_dump(mode="json"),
        "id": f"toolu_{type(result).__name__}",
        "type": "tool_call",
    }])


async def test_a_prioritized_replay_formalizes_only_the_ranked_claim(
    langgraph_db, project, cvlr_confinement, monkeypatch
):
    """The tape replayed in prioritized mode: one ranking, one unit formalized, no hoist, and the
    rest of the candidates reported as set aside.

    The recording is comprehensive, so two replies are written here. The ranking has no recorded
    lane at all. The report's grouping was recorded over every unit's properties and is rejected
    for naming ones this run never formalized; it is replaced, and the rest of the report lane (the
    finding drafted from the withdrawals unit's counterexample) replays as recorded. Everything up
    to the ranking is shared with a comprehensive run, and the unit it picks replays the lane
    recorded for the same properties. The hoist's lane goes unread: a prioritized run does not
    hoist, and a run that did would take a ``formalize-3`` the ranking never chose.
    """
    fake = _install_tape(monkeypatch)
    recorded_report = [
        m for m in fake.lanes["report"]
        if not any(c["name"] == GroupingResult.__name__ for c in m.tool_calls)
    ]
    fake.lanes = {
        **fake.lanes,
        PRIORITIZE_TASK_ID: [_structured_reply(_ranking())],
        "report": [_structured_reply(_report_grouping()), *recorded_report],
    }
    monkeypatch.setenv(RUN_MODE_ENV, RunMode.PRIORITIZED.value)
    result = await run_scenario(project, RunSummary(), GenericRustConsoleHandler(set()).make_handler)

    assert result.run_mode is RunMode.PRIORITIZED
    assert [o.feat.display_name for o in result.outcomes] == ["Withdrawals & Fee Sharing"]
    assert result.n_properties == len(_FOCUS)
    assert result.n_delivered == 1, f"the focused unit did not publish: {result.failures}"
    assert fake.lane_cursors.get("hoist", 0) == 0

    reports = project / CVLR_DELIVERABLE_DIR / "reports"
    assert (reports / "property_ranking.json").is_file()
    report = json.loads((reports / "report.json").read_text())
    n_candidates = sum(len(ts) for ts in _CANDIDATES.values())
    assert len(report["deprioritized"]) == n_candidates - len(_FOCUS)
