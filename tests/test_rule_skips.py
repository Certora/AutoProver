"""Prover report handling and rule-skip machinery over the async multi-buffer path.

Each run-target buffer is verified whole as a background job (submit_buffer) whose outcome is
drained and stamped by collect_results. These tests drive that flow with a monkeypatched prover
(the certora_prover fixture) to cover: how a buffer's prover report (string error, summarized
to-do list, raw rule statuses) is surfaced and stamped; how a skipped rule that fails is forgiven
so its buffer still completes; and the rule-skip reducer itself.
"""
import pytest

from composer.spec.source.author import ExpectRuleFailure, ExpectRulePassage
from composer.spec.source.prover import StateWithSkips, VALIDATION_KEY
from composer.prover.core import ProverReport
from composer.prover.ptypes import RulePath

from graphcore.testing import Scenario, tool_call_raw, ToolCallDict
from composer.spec.source.spec_buffers import NamedBuffer, check_buffer_completion

from .conftest import ProverMock

pytestmark = pytest.mark.asyncio


_SKIP = "expect_rule_failure"
_UNSKIP = "expect_rule_passage"


# ---------------------------------------------------------------------------
# Buffer + tool-call constructors
# ---------------------------------------------------------------------------


def _buf(name: str, *rules: str) -> NamedBuffer:
    """A run-target buffer declaring exactly ``rules`` — the mocked ``declared_rules_list`` parses
    these declarations back out, and the buffer owns them (property->rule mapping), so completion
    requires each to verify against this buffer."""
    cvl = "".join(f"rule {r} {{ assert true; }}\n" for r in rules)
    return NamedBuffer(
        name=name,
        cvl=cvl,
        property_rules={f"P-{name}": list(rules)} if rules else {},
    )


def _buffers(**named: NamedBuffer) -> dict[str, NamedBuffer]:
    return dict(named)


def _submit(name: str) -> ToolCallDict:
    # `name` is also tool_call_raw's first positional, so build the ToolCallDict directly.
    return {"name": "submit_buffer", "args": {"name": name}}


def _collect(wait: bool = False) -> ToolCallDict:
    return tool_call_raw("collect_results", wait=wait)


def _skip(rule_name: str, reason: str) -> ToolCallDict:
    return tool_call_raw(_SKIP, rule_name=rule_name, reason=reason)


def _unskip(rule_name: str) -> ToolCallDict:
    return tool_call_raw(_UNSKIP, rule_name=rule_name)


# ---------------------------------------------------------------------------
# Prover response constructors
# ---------------------------------------------------------------------------


def _raw_report(**rule_status: bool) -> ProverReport:
    return ProverReport(
        result_str="Prover report output", link="local://test-run",
        raw_rule_status={
            RulePath(rule=k): "VERIFIED" if v else "VIOLATED" for (k, v) in rule_status.items()
        },
        certora_run_stdout="certoraRun output",
    )


def _summarized_report(todo: str, **rule_status: bool) -> ProverReport:
    return ProverReport(
        result_str=todo, link="local://test-run",
        raw_rule_status={
            RulePath(rule=k): "VERIFIED" if v else "VIOLATED" for (k, v) in rule_status.items()
        },
        certora_run_stdout="certoraRun output",
    )


# ---------------------------------------------------------------------------
# Scenario builder + extractors
# ---------------------------------------------------------------------------


def _scenario(
    certora_prover: ProverMock,
    buffers: dict[str, NamedBuffer],
    *,
    rule_skips: dict[str, str] | None = None,
    **responses: ProverReport | str,
):
    """A scenario over the buffer prover tools (submit_buffer / collect_results) plus the skip
    tools. ``responses`` maps a buffer name to the report (or error string) its job returns."""
    tools = [
        *certora_prover.buffers(dict(responses)),
        ExpectRuleFailure.as_tool(_SKIP),
        ExpectRulePassage.as_tool(_UNSKIP),
    ]
    return Scenario(StateWithSkips, *tools).init(
        curr_spec=None,
        buffers=buffers,
        skipped=[],
        property_rules=[],
        validations={},
        required_validations=[VALIDATION_KEY],
        rule_skips=rule_skips or {},
        config={"files": ["src/Foo.sol"]},
        reminders_channel=[],
        version_history=[],
    )


def _prover_complete(st: StateWithSkips) -> str | None:
    """None once every run-target buffer carries a prover stamp at its current digest."""
    return check_buffer_completion(
        st["buffers"], st["validations"], ["prover"], skipped=[], version_history=[]
    )


def _rule_skips(st: StateWithSkips) -> dict[str, str]:
    return st["rule_skips"]


# =========================================================================
# Prover report handling
# =========================================================================


class TestProverReportHandling:
    async def test_no_run_targets_returns_error(self, certora_prover: ProverMock):
        msg = await _scenario(certora_prover, _buffers()).turn(
            _collect()
        ).run_last_single_tool("collect_results")
        assert "no run-target" in msg.lower()

    async def test_string_error_passthrough(self, certora_prover: ProverMock):
        msg = await _scenario(
            certora_prover, _buffers(b=_buf("b", "r")),
            b="Internal prover error: out of memory",
        ).turns(
            _submit("b"), _collect(wait=True),
        ).run_last_single_tool("collect_results")
        assert "out of memory" in msg

    async def test_summarized_report_surfaces_todo(self, certora_prover: ProverMock):
        msg = await _scenario(
            certora_prover, _buffers(b=_buf("b", "foo", "bar")),
            b=_summarized_report("1. Fix rule foo\n2. Fix rule bar", foo=False, bar=False),
        ).turns(
            _submit("b"), _collect(wait=True),
        ).run_last_single_tool("collect_results")
        assert "Fix rule foo" in msg

    async def test_raw_report_failures_no_stamp(self, certora_prover: ProverMock):
        st = await _scenario(
            certora_prover, _buffers(b=_buf("b", "foo", "bar")),
            b=_raw_report(foo=True, bar=False),
        ).turns(
            _submit("b"), _collect(wait=True),
        ).run()
        assert _prover_complete(st) is not None

    async def test_raw_report_all_verified_stamps(self, certora_prover: ProverMock):
        st = await _scenario(
            certora_prover, _buffers(b=_buf("b", "foo", "bar")),
            b=_raw_report(foo=True, bar=True),
        ).turns(
            _submit("b"), _collect(wait=True),
        ).run()
        assert _prover_complete(st) is None

    async def test_partial_coverage_doesnt_stamp(self, certora_prover: ProverMock):
        # The buffer owns foo and bar, but its run reports only foo — bar was never exercised
        # against this buffer, so the buffer stays uncovered and does not stamp.
        st = await _scenario(
            certora_prover, _buffers(b=_buf("b", "foo", "bar")),
            b=_raw_report(foo=True),
        ).turns(
            _submit("b"), _collect(wait=True),
        ).run()
        assert _prover_complete(st) is not None


# =========================================================================
# Rule skip interactions with the prover
# =========================================================================


class TestRuleSkipProverInteraction:
    async def test_skipped_failure_counts_as_verified(self, certora_prover: ProverMock):
        """ruleA fails but is skipped, ruleB passes → the buffer completes."""
        st = await _scenario(
            certora_prover, _buffers(b=_buf("b", "ruleA", "ruleB")),
            b=_raw_report(ruleA=False, ruleB=True),
        ).turn(
            _skip("ruleA", "known issue"),
        ).turns(
            _submit("b"), _collect(wait=True),
        ).run()
        assert _prover_complete(st) is None

    async def test_unskipped_failure_blocks_verification(self, certora_prover: ProverMock):
        """Skip ruleA, then unskip it. The prover returns ruleA=fail → the buffer does not complete."""
        st = await _scenario(
            certora_prover, _buffers(b=_buf("b", "ruleA", "ruleB")),
            b=_raw_report(ruleA=False, ruleB=True),
        ).turn(
            _skip("ruleA", "temp"),
        ).turn(
            _unskip("ruleA"),
        ).turns(
            _submit("b"), _collect(wait=True),
        ).run()
        assert _prover_complete(st) is not None

    async def test_non_skipped_failure_blocks_despite_other_skips(self, certora_prover: ProverMock):
        """ruleA is skipped and fails, ruleB is NOT skipped and also fails → the buffer does not
        complete."""
        st = await _scenario(
            certora_prover, _buffers(b=_buf("b", "ruleA", "ruleB")),
            b=_raw_report(ruleA=False, ruleB=False),
        ).turn(
            _skip("ruleA", "known"),
        ).turns(
            _submit("b"), _collect(wait=True),
        ).run()
        assert _prover_complete(st) is not None


# =========================================================================
# Rule skip reducer integration
# =========================================================================


class TestRuleSkipReducer:
    async def test_multiple_skips_merge(self, certora_prover: ProverMock):
        skips = await _scenario(certora_prover, _buffers()).turn(
            _skip("ruleA", "reason A"),
        ).turn(
            _skip("ruleB", "reason B"),
        ).map_run(_rule_skips)
        assert skips == {"ruleA": "reason A", "ruleB": "reason B"}

    async def test_skip_preserves_existing(self, certora_prover: ProverMock):
        skips = await _scenario(
            certora_prover, _buffers(), rule_skips={"ruleA": "existing"},
        ).turn(
            _skip("ruleB", "new"),
        ).map_run(_rule_skips)
        assert skips == {"ruleA": "existing", "ruleB": "new"}

    async def test_skip_overwrites_reason(self, certora_prover: ProverMock):
        skips = await _scenario(certora_prover, _buffers()).turn(
            _skip("ruleA", "old"),
        ).turn(
            _skip("ruleA", "new"),
        ).map_run(_rule_skips)
        assert skips["ruleA"] == "new"

    async def test_unskip_removes(self, certora_prover: ProverMock):
        skips = await _scenario(certora_prover, _buffers()).turn(
            _skip("ruleA", "temp"),
        ).turn(
            _unskip("ruleA"),
        ).map_run(_rule_skips)
        assert "ruleA" not in skips
