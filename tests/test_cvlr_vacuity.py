"""`explain_vacuity`: a SANITY_FAILED rule rerun for its unsat core, and the core read.

The CVLR counterpart of the CVL sanity analyzer (`sanity_analyzer`). No LLM, no network, no cargo:
the submission, the results API and the analyst are all stubbed at the seams the tool calls.
"""

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from composer.prover.conf import SelectRules
from composer.prover.core import ProverReport
from composer.prover.ptypes import RulePath
from composer.spec.cvlr import verify as verify_mod
from composer.spec.cvlr.conf import (
    CheckVerdicts,
    CollectUnsatCore,
    ProverSettings,
    RunOverlay,
    solana_conf,
)
from composer.spec.cvlr.prover import Checked, Prepared
from composer.spec.cvlr.prover import Submission as CvlrSubmission
from composer.spec.cvlr.tree import Reconciled
from composer.spec.cvlr.vacuity import (
    AssertionInCore,
    AssertionNotInCore,
    VacuityAnalysis,
    core_finding,
)
from composer.spec.source.report_prover import fetch_unsat_cores
from composer.templates.loader import load_jinja_template

_CORE = (Path(__file__).parent / "data" / "cvlr_unsat_core" / "assertion_in_core.txt").read_text()

_DRAFT = """
use cvlr::prelude::*;

#[rule]
fn rule_backed() { cvlr_assert!(true); }

#[rule]
fn rule_other() { cvlr_assert!(true); }
"""


# ---------------------------------------------------------------------------------------------
# the conf


def test_an_unsat_core_run_turns_the_vacuity_check_off_and_cores_on():
    """With the check on, a vacuous rule is SANITY_FAILED and the prover writes no core for it; off,
    it verifies, and `coverage_info` makes the prover write the core of that proof. `basic`, not the
    `advanced` CVL's reruns use: on a Solana rule `advanced` ran over 25 minutes where `basic` took
    the ordinary 25s."""
    conf = solana_conf(
        ProverSettings(), RunOverlay(build_script=Path("/w/b.py"), purpose=CollectUnsatCore())
    )
    assert conf["rule_sanity"] == "none"
    assert conf["coverage_info"] == "basic"


def test_an_ordinary_run_is_unchanged():
    conf = solana_conf(ProverSettings(), RunOverlay(build_script=Path("/w/b.py")))
    assert conf["rule_sanity"] == "basic"
    assert "coverage_info" not in conf
    assert RunOverlay(build_script=Path("/w/b.py")).purpose == CheckVerdicts()


# ---------------------------------------------------------------------------------------------
# the one fact about a core that needs no agent


def test_a_core_that_needs_the_assertion_is_recognized():
    """Measured on certora-vault-tutorial: a rule SANITY_FAILED under `u128` sums whose core, with the
    check off, contains its own assertion — and which verified once restated over `u64`."""
    finding = core_finding(_CORE)
    assert isinstance(finding, AssertionInCore)
    assert "ASSERT B548:bool" in finding.line
    assert "standard_deposits.rs:149:1" in finding.line


def test_a_core_without_the_assertion_is_recognized():
    """The assertion as a `[context]` line is not in the core — only `[in UC]` lines are."""
    without = _CORE.replace("[in UC] 66: ASSERT", "[context] 66: ASSERT")
    assert isinstance(core_finding(without), AssertionNotInCore)


def test_an_assertion_named_only_in_an_annotation_is_not_the_assertion():
    """`snippet.cmd := Assert(…)` annotations name the assertion without being it."""
    only_annotation = "\n".join(
        line for line in _CORE.splitlines() if not line.startswith("[in UC] 66: ASSERT")
    )
    assert "Assert(displayMessage=assert" in only_annotation
    assert isinstance(core_finding(only_annotation), AssertionNotInCore)


# ---------------------------------------------------------------------------------------------
# fetching a rule's cores


class _FakeResults:
    def __init__(self, core_map: dict[str, list[str]], files: dict[str, str]) -> None:
        self.core_map = core_map
        self.files = files
        self.jobs: list[str] = []

    def unsat_core_map(self, job: str) -> dict[str, list[str]]:
        self.jobs.append(job)
        return self.core_map

    def fetch_output_file(self, job: str, name: str) -> str:
        return self.files[name]


def test_a_rule_s_cores_are_the_ones_its_checks_are_keyed_by():
    """A core is keyed by check — the rule, or a sub-check `<rule>-Assertions` — and a rule whose
    name merely starts with another's is not one of its checks."""
    api = _FakeResults(
        {
            "rule_backed-Assertions": ["UnsatCoreTAC-rule_backed-0.txt"],
            "rule_backed_extra-Assertions": ["UnsatCoreTAC-rule_backed_extra-0.txt"],
            "rule_other-Assertions": ["UnsatCoreTAC-rule_other-0.txt"],
        },
        {
            "UnsatCoreTAC-rule_backed-0.txt": "mine",
            "UnsatCoreTAC-rule_backed_extra-0.txt": "not mine",
            "UnsatCoreTAC-rule_other-0.txt": "not mine either",
        },
    )
    assert fetch_unsat_cores(api, "https://x/output/1/abc", "rule_backed") == ("mine",)  # type: ignore[arg-type]


def test_a_job_status_link_is_handed_over_as_its_job_id():
    """The Solana report link is `/jobStatus/…`, which POU cannot parse (upstream-defects P5)."""
    api = _FakeResults({}, {})
    fetch_unsat_cores(api, "https://prover.certora.com/jobStatus/37632/c1734c04?anonymousKey=k", "r")  # type: ignore[arg-type]
    assert api.jobs == ["c1734c04"]


# ---------------------------------------------------------------------------------------------
# the tool


def _state(draft: str = _DRAFT) -> dict:
    return {
        "messages": [],
        "curr_spec": draft,
        "expected_failures": {},
        "skipped": [],
        "summaries": [],
        "munges": [],
        "property_rules": [],
        "rule_subjects": [],
        "prover_settings": ProverSettings(loop_iter=3),
        "required_validations": [],
        "validations": {},
        "failed": None,
        "budget_curtailed": False,
    }


async def _stubbed_stage(draft, summaries=(), munges=()) -> Reconciled:
    return Reconciled(written=(), drifted=())


class _FakeAnalyzer:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def explain(self, **kwargs) -> VacuityAnalysis:
        self.calls.append(kwargs)
        return VacuityAnalysis(
            issue_type="Not a Contradiction",
            mitigation_options=[],
            short_summary="the sum",
            root_cause="widening",
            detailed_analysis="…",
            solution="## Solution: restate over u64",
        )


def _deps(analyzer: _FakeAnalyzer, results: _FakeResults) -> verify_mod.VacuityDeps:
    verify = SimpleNamespace(
        lock=asyncio.Lock(),
        target=SimpleNamespace(session=None, stage=_stubbed_stage, build_slot=contextlib.nullcontext),
        submission=CvlrSubmission(manifest_path=Path("/w/Cargo.toml"), stem="unit_x"),
        prover_opts=None,
    )
    return verify_mod.VacuityDeps(verify, analyzer, lambda: results)  # type: ignore[arg-type]


async def _run(deps: verify_mod.VacuityDeps, state: dict, rule: str) -> str:
    token = verify_mod.ExplainVacuity._dep_ctx.set(deps)
    try:
        return await verify_mod.ExplainVacuity(state=state, tool_call_id="tc", rule=rule).run()
    finally:
        verify_mod.ExplainVacuity._dep_ctx.reset(token)


def _checked(statuses: dict[str, str]) -> Checked:
    report = ProverReport(
        raw_rule_status={RulePath(rule=r): s for r, s in statuses.items()},  # type: ignore[misc]
        result_str="",
        link="https://prover.certora.com/jobStatus/37632/probe1?anonymousKey=k",
        certora_run_stdout="",
    )
    return Checked(build=None, report=report)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_rule_the_draft_does_not_declare_is_refused_before_anything_runs():
    answer = await _run(_deps(_FakeAnalyzer(), _FakeResults({}, {})), _state(), "rule_missing")
    assert "declares no rule `rule_missing`" in answer and "rule_backed" in answer


@pytest.mark.asyncio
async def test_the_probe_checks_one_rule_in_a_conf_of_its_own(monkeypatch):
    """One rule, not the draft's: the others are not what is being explained. Its own conf stem: the
    unit's conf ships with the deliverable, and a diagnostic run must not overwrite it."""
    captured: dict = {}

    async def fake_prepare(session, submission, **kwargs):
        captured["submission"] = submission
        return Prepared(build=None, conf_path=Path("/w/c.conf"))  # type: ignore[arg-type]

    async def fake_run(session, prepared, **kwargs):
        return _checked({"rule_backed": "VERIFIED"})

    monkeypatch.setattr(verify_mod, "prepare_submission", fake_prepare)
    monkeypatch.setattr(verify_mod, "run_submission", fake_run)
    results = _FakeResults({"rule_backed-Assertions": ["c.txt"]}, {"c.txt": _CORE})
    await _run(_deps(_FakeAnalyzer(), results), _state(), "rule_backed")

    sub: CvlrSubmission = captured["submission"]
    assert sub.purpose == CollectUnsatCore()
    assert sub.stem == "unit_x_unsat_core"
    assert sub.rules == SelectRules(("rule_backed",))
    assert sub.settings == ProverSettings(loop_iter=3)


@pytest.mark.asyncio
async def test_the_answer_leads_with_whether_the_assertion_is_in_the_core(monkeypatch):
    async def fake_prepare(session, submission, **kwargs):
        return Prepared(build=None, conf_path=Path("/w/c.conf"))  # type: ignore[arg-type]

    async def fake_run(session, prepared, **kwargs):
        return _checked({"rule_backed": "VERIFIED"})

    monkeypatch.setattr(verify_mod, "prepare_submission", fake_prepare)
    monkeypatch.setattr(verify_mod, "run_submission", fake_run)
    analyzer = _FakeAnalyzer()
    results = _FakeResults({"rule_backed-Assertions": ["c.txt"]}, {"c.txt": _CORE})
    answer = await _run(_deps(analyzer, results), _state(), "rule_backed")

    assert answer.startswith("The rule's assertion is in the unsat core")
    assert "# Vacuity Analysis" in answer and "restate over u64" in answer
    assert "jobStatus/37632/probe1" in answer
    (call,) = analyzer.calls
    assert isinstance(call["finding"], AssertionInCore)
    assert call["core"] == _CORE and call["harness"] == _DRAFT
    # The conf the rule was vacuous under, with the author's settings, not the probe's.
    assert call["conf"]["rule_sanity"] == "basic" and call["conf"]["loop_iter"] == "3"


@pytest.mark.asyncio
async def test_a_rule_that_does_not_verify_without_the_check_has_no_core_to_read(monkeypatch):
    """No proof, no core — and no analyst call spent on nothing."""

    async def fake_prepare(session, submission, **kwargs):
        return Prepared(build=None, conf_path=Path("/w/c.conf"))  # type: ignore[arg-type]

    async def fake_run(session, prepared, **kwargs):
        return _checked({"rule_backed": "VIOLATED"})

    monkeypatch.setattr(verify_mod, "prepare_submission", fake_prepare)
    monkeypatch.setattr(verify_mod, "run_submission", fake_run)
    analyzer = _FakeAnalyzer()
    answer = await _run(_deps(analyzer, _FakeResults({}, {})), _state(), "rule_backed")
    assert "did not verify" in answer and "VIOLATED" in answer
    assert analyzer.calls == []


# ---------------------------------------------------------------------------------------------
# the prompts


def test_the_cvlr_prompts_render():
    system = load_jinja_template("cvlr_vacuity_system_prompt.j2")
    initial = load_jinja_template("cvlr_vacuity_prompt.j2")
    assert "Whether the rule's own `ASSERT` is among them" in system
    # The core format is shared with the CVL analyzer's prompt, not restated.
    assert "Understanding the Unsat Core Structure" in initial
    assert "Understanding the Unsat Core Structure" in load_jinja_template("sanity_tool_prompt.j2")
