"""Async multi-buffer verification through the tool surface: submit_buffer launches a background
prover job per buffer, collect_results drains finished ones and stamps per-buffer completion, and a
shared-buffer edit invalidates every importer's stamp (the refine step). The prover/compiler seams are
mocked (see the ``certora_prover`` fixture); each buffer's response is keyed by name because jobs run
concurrently."""

import pytest

from graphcore.testing import Scenario, tool_call_raw

from composer.prover.core import ProverReport
from composer.prover.ptypes import RulePath
from composer.spec.source.buffer_tools import put_buffer
from composer.spec.source.prover import StateWithSkips, VALIDATION_KEY
from composer.spec.source.spec_buffers import (
    NamedBuffer, buffer_state_digest, check_buffer_completion,
)

from .conftest import ProverMock


SHARED = "ghost g(uint) returns uint;\n"


def _buf(name: str, rule: str) -> NamedBuffer:
    return NamedBuffer(
        name=name,
        cvl=f'import "shared.spec";\nrule {rule} {{ assert true; }}\n',
        property_rules={f"P-{name}": [rule]},
        imports=("shared",),
    )


def _buffers() -> dict[str, NamedBuffer]:
    return {
        "shared": NamedBuffer(name="shared", cvl=SHARED, is_run_target=False),
        "easy": _buf("easy", "r_easy"),
        "hard": _buf("hard", "r_hard"),
    }


def _report(**rule_status: bool) -> ProverReport:
    return ProverReport(
        result_str="Prover report output",
        link="local://test-run",
        raw_rule_status={
            RulePath(rule=k): "VERIFIED" if v else "VIOLATED" for (k, v) in rule_status.items()
        },
        certora_run_stdout="",
    )


def _scenario(certora_prover: ProverMock, buffers: dict[str, NamedBuffer], **responses: ProverReport):
    tools = [
        *certora_prover.buffers(dict(responses)),
        put_buffer(StateWithSkips),
    ]
    return Scenario(StateWithSkips, *tools).init(
        curr_spec=None,
        buffers=buffers,
        skipped=[],
        property_rules=[],
        validations={},
        required_validations=[VALIDATION_KEY],
        rule_skips={},
        config={"files": ["src/Foo.sol"]},
        reminders_channel=[],
        version_history=[],
    )


def _submit(name: str):
    # `name` is also tool_call_raw's first positional, so build the ToolCallDict directly.
    return {"name": "submit_buffer", "args": {"name": name}}


def _collect(wait: bool = False):
    return tool_call_raw("collect_results", wait=wait)


def _put_shared(cvl: str):
    return {
        "name": "put_buffer",
        "args": {"name": "shared", "cvl": cvl, "is_run_target": False,
                 "imports": [], "property_rules": {}},
    }


def _prover_complete(st: StateWithSkips) -> str | None:
    return check_buffer_completion(
        st["buffers"], st["validations"], ["prover"], skipped=[], version_history=[]
    )


@pytest.fixture(autouse=True)
def _accept_cvl(monkeypatch):
    """put_buffer/edit_buffer validate writes by shelling out to the real CVL typechecker jar
    (cvl_syntax_error); that jar is absent in unit-test CI, so every buffer write would be rejected.
    This suite exercises the async submit/collect flow, not CVL parsing, so accept all writes."""
    monkeypatch.setattr(
        "composer.spec.source.buffer_tools.cvl_syntax_error", lambda *a, **k: None
    )


@pytest.mark.asyncio
class TestBufferSubmitCollect:
    async def test_submit_then_collect_stamps_each_buffer(self, certora_prover: ProverMock):
        """Submitting each run-target buffer and collecting its result stamps that buffer's prover
        completion; once both are in, overall prover-completion holds."""
        st = await _scenario(
            certora_prover, _buffers(),
            easy=_report(r_easy=True), hard=_report(r_hard=True),
        ).turns(
            _submit("easy"),
            _submit("hard"),
            _collect(wait=True),
            _collect(wait=True),
        ).run()

        def digest(n: str) -> str:
            return buffer_state_digest(st["buffers"], n, skipped=[], version_history=[])

        assert st["validations"].get("prover:easy") == digest("easy")
        assert st["validations"].get("prover:hard") == digest("hard")
        assert _prover_complete(st) is None

    async def test_shared_edit_makes_verified_buffers_stale(self, certora_prover: ProverMock):
        """After both buffers verify, editing the shared buffer they import changes their digests, so
        their prover stamps no longer match — the refine case that must force a re-run."""
        st = await _scenario(
            certora_prover, _buffers(),
            easy=_report(r_easy=True), hard=_report(r_hard=True),
        ).turns(
            _submit("easy"), _submit("hard"), _collect(wait=True), _collect(wait=True),
            _put_shared(SHARED + "ghost h(uint) returns uint;\n"),
        ).run()
        assert _prover_complete(st) is not None

    async def test_resubmit_after_shared_edit_recompletes(self, certora_prover: ProverMock):
        """Re-submitting the invalidated buffers at the new shared content re-verifies and re-completes
        them — the stamp lands at the new digest."""
        st = await _scenario(
            certora_prover, _buffers(),
            easy=_report(r_easy=True), hard=_report(r_hard=True),
        ).turns(
            _submit("easy"), _submit("hard"), _collect(wait=True), _collect(wait=True),
            _put_shared(SHARED + "ghost h(uint) returns uint;\n"),
            _submit("easy"), _submit("hard"), _collect(wait=True), _collect(wait=True),
        ).run()
        assert _prover_complete(st) is None

    async def test_collect_without_finished_jobs_does_not_block(self, certora_prover: ProverMock):
        """A non-blocking collect with nothing submitted returns a status board rather than hanging."""
        st = await _scenario(certora_prover, _buffers()).turns(
            _collect(wait=False),
        ).run()
        assert _prover_complete(st) is not None  # nothing verified yet

    async def test_non_editing_feedback_stamps_per_buffer(self):
        """The non-editing feedback tool (structural invariants / immutable source) must review each
        buffer and stamp feedback:<buffer> — the path that previously read empty curr_spec and returned
        'No spec put yet', deadlocking publish."""
        from dataclasses import dataclass
        from composer.spec.source.author import BufferPropertyFeedbackTool
        from composer.spec.cvl_generation import FeedbackServices
        from composer.spec.types import PropertyTitle

        @dataclass
        class _V:
            good: bool
            feedback: str

        async def judge(spec, skipped, rebuttals, within_tool):
            return _V(good=True, feedback="")

        tool = BufferPropertyFeedbackTool.bind(
            FeedbackServices(feedback_thunk=judge, titles=[PropertyTitle("P-easy")])
        ).as_tool("feedback_tool")
        buffers = {
            "shared": NamedBuffer(name="shared", cvl=SHARED, is_run_target=False),
            "easy": _buf("easy", "r_easy"),
        }
        state = {
            "buffers": buffers, "curr_spec": None, "skipped": [],
            "validations": {}, "version_history": [], "messages": [],
            "required_validations": [], "property_rules": [], "rule_skips": {},
            "config": {}, "prover_history": [], "reminders_channel": [],
            "failed": None, "budget_curtailed": False,
        }
        res = await tool.ainvoke(
            {"name": "feedback_tool", "args": {"state": state, "rebuttals": []},
             "id": "t", "type": "tool_call"}
        )
        val = res.update.get("validations", {}) if hasattr(res, "update") else {}
        assert val.get("feedback:easy") == buffer_state_digest(
            buffers, "easy", skipped=[], version_history=[]
        )
