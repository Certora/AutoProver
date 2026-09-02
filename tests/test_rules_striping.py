"""Tests for rules striping: satisfying a spec's rules piecemeal across several
``verify_spec`` calls (rule-scoped includes and excludes) instead of one full run.

Covers:

- the pure helpers — which rules a logged run executed (``_executed_rules``) and
  whether the run history against the current authoring state adds up to full
  coverage (``_is_completion_history``);
- the ``verify_spec`` tool surface — include/exclude plumbing into the conf, the
  ``ProverRunLog`` entries, and completion (validation stamp + reminder) arriving
  on whichever run completes coverage, however it was scoped;
- ``declared_rules_list``, with its certoraRun/typechecker subprocesses faked;
- the ``known_rules`` cross-check of ``validate_property_rules``.

The prover core is mocked throughout (the ``certora_prover`` fixture's seams:
``run_prover`` + ``declared_rules_list``); no prover jobs run.
"""
import asyncio
import json
from pathlib import Path
from typing import Annotated

import pytest

from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command

from composer.authoring.state import check_completion, spec_digest
from composer.prover.core import (
    ProverReport, SpecCompilationError, declared_rules_list
)
from composer.prover.ptypes import RulePath, StatusCodes
from composer.spec.cvl_generation import PropertyRuleMapping, validate_property_rules
from composer.spec.source.author import ExpectRuleFailure
from composer.spec.source.prover import (
    NagMarker, ProverHistoryItem, ProverRunLog, RuleSelection, StateWithSkips,
    VALIDATION_KEY, _executed_rules, _is_completion_history,
)
from composer.spec.types import PropertyTitle, RuleName

from graphcore.graph import tool_state_update
from graphcore.testing import Scenario, ToolCallDict, tool_call_raw
from graphcore.tools.results import result_tool_generator

from .conftest import ProverMock, ProverToolResponse

RA = RulePath(rule="a")
RB = RulePath(rule="b")


# ---------------------------------------------------------------------------
# ProverRunLog constructors
# ---------------------------------------------------------------------------


def _inc(*rules: str) -> RuleSelection:
    return {"sort": "include", "selector": list(rules)}


def _exc(*rules: str) -> RuleSelection:
    return {"sort": "exclude", "selector": list(rules)}


def _log(
    *results: tuple[RulePath, StatusCodes],
    digest: str = "d1",
    rules: RuleSelection | None = None,
    declared: tuple[str, ...] = ("a", "b"),
) -> ProverRunLog:
    return ProverRunLog(
        tool_call_id="tc",
        prover_results=list(results),
        rules=rules,
        spec_digest="spec-hash",
        sort="run",
        declared_rules=list(declared),
        state_digest=digest,
    )


# =========================================================================
# _executed_rules: which rules a logged run actually exercised
# =========================================================================


class TestExecutedRules:
    def test_full_run_executes_all_declared(self):
        assert _executed_rules(_log(declared=("a", "b", "c"))) == ["a", "b", "c"]

    def test_include_executes_the_selector(self):
        assert _executed_rules(_log(rules=_inc("b"), declared=("a", "b", "c"))) == ["b"]

    def test_exclude_executes_the_complement(self):
        assert _executed_rules(_log(rules=_exc("b"), declared=("a", "b", "c"))) == ["a", "c"]


# =========================================================================
# _is_completion_history: piecemeal coverage accounting
# =========================================================================


def _complete(
    history: list[ProverHistoryItem],
    curr: list[tuple[RulePath, StatusCodes]],
    *,
    digest: str = "d1",
    expected_to_fail: set[str] | None = None,
    all_rules: tuple[str, ...] = ("a", "b"),
) -> bool:
    return _is_completion_history(
        l=history,
        curr_digest=digest,
        expected_to_fail=expected_to_fail or set(),
        curr_status=curr,
        all_rules=list(all_rules),
    )


class TestCompletionHistory:
    def test_single_full_run_completes(self):
        assert _complete([], [(RA, "VERIFIED"), (RB, "VERIFIED")])

    def test_piecemeal_runs_complete_together(self):
        history: list[ProverHistoryItem] = [_log((RA, "VERIFIED"), rules=_inc("a"))]
        assert _complete(history, [(RB, "VERIFIED")])

    def test_uncovered_rule_blocks(self):
        assert not _complete([], [(RA, "VERIFIED")])

    def test_current_failure_blocks(self):
        assert not _complete([], [(RA, "VERIFIED"), (RB, "VIOLATED")])

    def test_expected_failure_is_forgiven_and_covered(self):
        assert _complete(
            [], [(RA, "VERIFIED"), (RB, "VIOLATED")], expected_to_fail={"b"}
        )

    def test_historic_failure_of_unskipped_rule_blocks(self):
        history: list[ProverHistoryItem] = [_log((RB, "TIMEOUT"), rules=_inc("b"))]
        assert not _complete(history, [(RA, "VERIFIED")])

    def test_state_digest_mismatch_severs_coverage(self):
        history: list[ProverHistoryItem] = [_log((RA, "VERIFIED"), digest="d0")]
        assert not _complete(history, [(RB, "VERIFIED")], digest="d1")

    def test_stale_run_stops_the_walk(self):
        # The walk stops at the first run against a different state: coverage from a
        # matching run BEHIND it does not count, even though its digest matches.
        history: list[ProverHistoryItem] = [
            _log((RA, "VERIFIED"), digest="d1"),
            _log((RB, "VERIFIED"), digest="d0"),
        ]
        assert not _complete(history, [(RB, "VERIFIED")], digest="d1")

    def test_nag_markers_are_transparent(self):
        history: list[ProverHistoryItem] = [
            _log((RA, "VERIFIED"), rules=_inc("a")),
            NagMarker(sort="nag", nagged_rules=[RA]),
        ]
        assert _complete(history, [(RB, "VERIFIED")])

    def test_overlapping_coverage_completes(self):
        # A rule re-verified by the current run also appears in the matching history
        # entry that supplies the rest of the coverage.
        history: list[ProverHistoryItem] = [_log((RA, "VERIFIED"), (RB, "VERIFIED"))]
        assert _complete(history, [(RA, "VERIFIED")])

    def test_overlapping_coverage_that_stays_incomplete_terminates(self):
        # Same rule verified twice with nothing covering the rest: must simply report
        # incomplete (this is the shape that used to re-walk the same history entry).
        history: list[ProverHistoryItem] = [_log((RA, "VERIFIED"), rules=_inc("a"))]
        assert not _complete(history, [(RA, "VERIFIED")])

    def test_undeclared_result_rules_are_ignored(self):
        # The prover reports checks the declared-rules list withholds (e.g. the
        # envfree static check); they must count for nothing rather than crash.
        static_check = RulePath(rule="envfreeFuncsStaticCheck")
        assert _complete(
            [], [(RA, "VERIFIED"), (static_check, "VERIFIED")], all_rules=("a",)
        )

    def test_parametric_instantiations_share_one_rule(self):
        assert _complete(
            [],
            [
                (RulePath(rule="a", method="f()"), "VERIFIED"),
                (RulePath(rule="a", method="g()"), "VERIFIED"),
                (RB, "VERIFIED"),
            ],
        )


# =========================================================================
# declared_rules_list: the certoraRun/typechecker rule-listing pre-pass
# =========================================================================


class _FakeProc:
    def __init__(self, rc: int, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = rc
        self._streams = (stdout, stderr)

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._streams


def _install_fake_prover_procs(
    monkeypatch,
    *,
    certora_rc: int = 0,
    java_rc: int = 0,
    rules_text: str = "a\nb\n",
    conf_msg: str | None = None,
    certora_output: bytes = b"",
    java_output: bytes = b"",
):
    """Fake the two subprocesses of ``declared_rules_list``: certoraRun materializes a
    build-mirror dir whose run.conf carries the ``--msg`` key it was passed (or
    ``conf_msg``, to simulate a foreign run), java writes ``rules_text`` to its
    ``-listRules`` target."""

    async def fake_exec(*argv, cwd=None, stdout=None, stderr=None):
        assert cwd is not None
        argv = [str(a) for a in argv]
        if argv[0] == "certoraRun":
            assert "--compilation_steps_only" in argv
            key = argv[argv.index("--msg") + 1]
            build = Path(cwd) / ".certora_internal" / "build_mirror"
            build.mkdir(parents=True, exist_ok=True)
            (build / "run.conf").write_text(
                json.dumps({"msg": conf_msg if conf_msg is not None else key})
            )
            return _FakeProc(certora_rc, stderr=certora_output)
        assert argv[0] == "java"
        Path(argv[argv.index("-listRules") + 1]).write_text(rules_text)
        return _FakeProc(java_rc, stderr=java_output)

    monkeypatch.setattr("asyncio.subprocess.create_subprocess_exec", fake_exec)


@pytest.mark.asyncio
class TestDeclaredRulesList:
    async def test_lists_rules_filtering_the_static_check(self, tmp_path, monkeypatch):
        _install_fake_prover_procs(
            monkeypatch, rules_text="a\n  b  \n\nenvfreeFuncsStaticCheck\nc\n"
        )
        assert await declared_rules_list(tmp_path, ["x.conf"]) == ["a", "b", "c"]

    async def test_rejects_caller_supplied_msg(self, tmp_path):
        with pytest.raises(ValueError, match="msg"):
            await declared_rules_list(tmp_path, ["x.conf", "--msg", "hello"])

    async def test_build_failure_carries_the_compiler_output(
        self, tmp_path, monkeypatch
    ):
        """The output is the whole point: it names the file and line, which is what
        an authoring agent needs to repair the spec."""
        _install_fake_prover_procs(
            monkeypatch, certora_rc=1,
            certora_output=b'Error in spec file (invariants.spec:34:5): could not '
                           b'type expression "sdai.pot()"',
        )
        with pytest.raises(SpecCompilationError) as caught:
            await declared_rules_list(tmp_path, ["x.conf"])
        assert "invariants.spec:34:5" in caught.value.output

    async def test_typechecker_failure_carries_the_compiler_output(
        self, tmp_path, monkeypatch
    ):
        _install_fake_prover_procs(
            monkeypatch, java_rc=1, java_output=b"rule 'foo' is not well typed",
        )
        with pytest.raises(SpecCompilationError) as caught:
            await declared_rules_list(tmp_path, ["x.conf"])
        assert "not well typed" in caught.value.output

    async def test_unmatched_build_dir_raises(self, tmp_path, monkeypatch):
        _install_fake_prover_procs(monkeypatch, conf_msg="someone else's run")
        with pytest.raises(ValueError, match="build dir"):
            await declared_rules_list(tmp_path, ["x.conf"])

    async def test_a_chatty_child_does_not_deadlock(self, tmp_path, monkeypatch):
        """Real subprocess, real pipes: a child that outfills the ~64KB pipe buffer
        blocks forever if nobody drains it, so this is the one case the fakes above
        cannot cover."""
        import sys

        from composer.prover.core import _run_captured

        rc, output = await asyncio.wait_for(
            _run_captured(
                sys.executable, "-c",
                "import sys; sys.stdout.write('o' * 200_000); "
                "sys.stderr.write('e' * 200_000); sys.exit(3)",
                cwd=tmp_path,
            ),
            timeout=30,
        )
        assert rc == 3
        assert output  # drained, not lost


    async def test_discovery_ignores_decoy_entries(self, tmp_path, monkeypatch):
        # Pre-existing .certora_internal clutter: a plain file, a dir without a
        # run.conf, one with unparseable json, one with a non-string msg, one with
        # another run's msg. Discovery must land on the dir the fake writes.
        internal = tmp_path / ".certora_internal"
        (internal / "no_conf_dir").mkdir(parents=True)
        (internal / "plain_file").write_text("not a dir")
        bad_json = internal / "bad_json"
        bad_json.mkdir()
        (bad_json / "run.conf").write_text("{oops")
        bad_msg = internal / "bad_msg"
        bad_msg.mkdir()
        (bad_msg / "run.conf").write_text(json.dumps({"msg": 42}))
        other = internal / "other_run"
        other.mkdir()
        (other / "run.conf").write_text(json.dumps({"msg": "not this one"}))

        _install_fake_prover_procs(monkeypatch, rules_text="a\n")
        assert await declared_rules_list(tmp_path, ["x.conf"]) == ["a"]


# =========================================================================
# verify_spec: striped runs through the tool surface
# =========================================================================

_PROVER = "verify_spec"
_SKIP = "expect_rule_failure"
_RESULT = "result"


result_tool = result_tool_generator(
    "result",
    (str, "Commentary"),
    "Signal completion",
    validator=(StateWithSkips, lambda st, *_: check_completion(st)),
)


@tool
def set_spec(
    spec: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Replace the spec under authoring (a digest-changing edit)."""
    return tool_state_update(tool_call_id=tool_call_id, content="spec updated", curr_spec=spec)


def _spec_decls(*rules: str) -> str:
    """A spec declaring exactly ``rules`` — the mocked ``declared_rules_list``
    parses these declarations back out as the run's declared-rules ground truth."""
    return "\n".join(f"rule {r} {{ assert true; }}" for r in rules)


def _report(**rule_status: bool) -> ProverReport:
    return ProverReport(
        result_str="Prover report output",
        link="local://test-run",
        raw_rule_status={
            RulePath(rule=k): "VERIFIED" if v else "VIOLATED"
            for (k, v) in rule_status.items()
        },
        certora_run_stdout=""
    )


def _verify(
    rules: list[str] | None = None, exclude_rules: list[str] | None = None
) -> ToolCallDict:
    return tool_call_raw(_PROVER, rules=rules, exclude_rules=exclude_rules)


def _result(commentary: str) -> ToolCallDict:
    return tool_call_raw(_RESULT, value=commentary)


def _set_spec(spec: str) -> ToolCallDict:
    return tool_call_raw("set_spec", spec=spec)


def _skip(rule_name: str, reason: str) -> ToolCallDict:
    return tool_call_raw(_SKIP, rule_name=rule_name, reason=reason)


def _scenario(
    certora_prover: ProverMock,
    *responses: ProverToolResponse,
    curr_spec: str,
    rule_skips: dict[str, str] | None = None,
):
    tools = [
        certora_prover(responses),
        ExpectRuleFailure.as_tool(_SKIP),
        result_tool,
        set_spec,
    ]
    return Scenario(StateWithSkips, *tools).init(
        curr_spec=curr_spec,
        skipped=[],
        property_rules=[],
        validations={},
        required_validations=[VALIDATION_KEY],
        rule_skips=rule_skips or {},
        config={"files": ["src/Foo.sol"]},
        reminders_channel=[],
        version_history=[],
    )


def _result_accepted(st: StateWithSkips) -> str:
    assert "result" in st
    return st["result"]


def _is_result_rejection(st: StateWithSkips) -> bool:
    return "result" not in st and Scenario.last_single_tool(
        _RESULT, st
    ).startswith("Completion REJECTED:")


@pytest.mark.asyncio
class TestStripedVerification:
    async def test_a_spec_that_does_not_compile_comes_back_to_the_agent(
        self, certora_prover: ProverMock, monkeypatch
    ):
        """The rule-listing pre-pass runs the compiler before the prover. A spec that
        fails to compile is the author agent's to fix and the compiler says exactly
        where, so it has to arrive as a tool result — raising past the agent ends the
        whole run over a repairable mistake."""
        async def failing(**_kwargs):
            raise SpecCompilationError(
                'Error in spec file (invariants.spec:34:5): could not type '
                'expression "sdai.pot()", message: Missing environment parameter '
                'to non-envfree function SavingsDai.pot()'
            )

        monkeypatch.setattr(
            "composer.spec.source.prover.declared_rules_list", failing
        )
        msg = await _scenario(
            certora_prover, curr_spec=_spec_decls("a", "b"),
        ).turn(
            _verify()
        ).run_last_single_tool(_PROVER)
        assert "failed to compile" in msg
        assert "invariants.spec:34:5" in msg
        assert "non-envfree" in msg
        # The prover is never reached — there is nothing to verify.
        assert certora_prover.calls == []


    async def test_rules_and_exclude_rules_mutually_exclusive(self, certora_prover: ProverMock):
        msg = await _scenario(
            certora_prover, curr_spec=_spec_decls("a", "b"),
        ).turn(
            _verify(rules=["a"], exclude_rules=["b"])
        ).run_last_single_tool(_PROVER)
        assert "both" in msg
        assert certora_prover.calls == []

    async def test_include_selection_reaches_conf_and_history(self, certora_prover: ProverMock):
        spec = _spec_decls("a", "b")
        history = await _scenario(
            certora_prover, _report(a=True), curr_spec=spec,
        ).turn(
            _verify(rules=["a"])
        ).map_run(lambda st: st["prover_history"])
        [entry] = history
        assert entry["sort"] == "run"
        assert entry["rules"] == {"sort": "include", "selector": ["a"]}
        assert entry["declared_rules"] == ["a", "b"]
        assert entry["state_digest"] == spec_digest(spec, [], [])
        [call] = certora_prover.calls
        assert call.conf["rule"] == ["a"]
        assert "exclude_rule" not in call.conf

    async def test_exclude_selection_reaches_conf_and_history(self, certora_prover: ProverMock):
        history = await _scenario(
            certora_prover, _report(a=True), curr_spec=_spec_decls("a", "b"),
        ).turn(
            _verify(exclude_rules=["b"])
        ).map_run(lambda st: st["prover_history"])
        [entry] = history
        assert entry["sort"] == "run"
        assert entry["rules"] == {"sort": "exclude", "selector": ["b"]}
        [call] = certora_prover.calls
        assert call.conf["exclude_rule"] == ["b"]
        assert "rule" not in call.conf

    async def test_piecemeal_completion_stamps(self, certora_prover: ProverMock):
        # The whole point of striping: two rule-scoped runs that together cover the
        # spec complete the task, no full run required.
        assert await _scenario(
            certora_prover, _report(a=True), _report(b=True),
            curr_spec=_spec_decls("a", "b"),
        ).turns(
            _verify(rules=["a"]),
            _verify(rules=["b"]),
            _result("done"),
        ).map_run(_result_accepted) == "done"

    async def test_exclude_run_alone_doesnt_complete(self, certora_prover: ProverMock):
        assert await _scenario(
            certora_prover, _report(a=True), curr_spec=_spec_decls("a", "b"),
        ).turns(
            _verify(exclude_rules=["b"]),
            _result("done"),
        ).map_run(_is_result_rejection)

    async def test_exclude_then_include_completes(self, certora_prover: ProverMock):
        assert await _scenario(
            certora_prover, _report(a=True, b=True), _report(c=True),
            curr_spec=_spec_decls("a", "b", "c"),
        ).turns(
            _verify(exclude_rules=["c"]),
            _verify(rules=["c"]),
            _result("done"),
        ).map_run(_result_accepted) == "done"

    async def test_spec_edit_resets_piecemeal_coverage(self, certora_prover: ProverMock):
        spec_v1 = _spec_decls("a", "b")
        spec_v2 = spec_v1 + "\n// tightened"
        assert await _scenario(
            certora_prover, _report(a=True), _report(b=True), curr_spec=spec_v1,
        ).turns(
            _verify(rules=["a"]),
            _set_spec(spec_v2),
            _verify(rules=["b"]),
            _result("done"),
        ).map_run(_is_result_rejection)

    async def test_skipped_rule_failure_counts_toward_coverage(self, certora_prover: ProverMock):
        assert await _scenario(
            certora_prover, _report(a=True), _report(b=False),
            curr_spec=_spec_decls("a", "b"),
        ).turn(
            _skip("b", "known limitation"),
        ).turns(
            _verify(rules=["a"]),
            _verify(rules=["b"]),
            _result("done"),
        ).map_run(_result_accepted) == "done"

    async def test_completion_reminder_delivered_on_completing_run(self, certora_prover: ProverMock):
        reminders = await _scenario(
            certora_prover, _report(a=True), _report(b=True),
            curr_spec=_spec_decls("a", "b"),
        ).turns(
            _verify(rules=["a"]),
            _verify(rules=["b"]),
        ).map_run(lambda st: st["reminders_channel"])
        assert any("task is completed" in r for r in reminders)

    async def test_no_completion_reminder_while_coverage_is_partial(self, certora_prover: ProverMock):
        reminders = await _scenario(
            certora_prover, _report(a=True), curr_spec=_spec_decls("a", "b"),
        ).turn(
            _verify(rules=["a"]),
        ).map_run(lambda st: st["reminders_channel"])
        assert reminders == []


# =========================================================================
# validate_property_rules: the known_rules cross-check
# =========================================================================


def _mapping(title: str, *rules: str) -> PropertyRuleMapping:
    return PropertyRuleMapping(
        property_title=PropertyTitle(title), rules=[RuleName(r) for r in rules]
    )


class TestValidatePropertyRules:
    def test_known_rules_reject_unran_claims(self):
        err = validate_property_rules(
            [_mapping("p1", "ghost_rule")], [], [PropertyTitle("p1")],
            known_rules={"real_rule"},
        )
        assert err is not None and "ghost_rule" in err

    def test_known_rules_reject_unclaimed_rules(self):
        err = validate_property_rules(
            [_mapping("p1", "a")], [], [PropertyTitle("p1")],
            known_rules={"a", "orphan"},
        )
        assert err is not None and "orphan" in err

    def test_matching_known_rules_accepted(self):
        assert validate_property_rules(
            [_mapping("p1", "a"), _mapping("p2", "b")],
            [],
            [PropertyTitle("p1"), PropertyTitle("p2")],
            known_rules={"a", "b"},
        ) is None

    def test_without_known_rules_names_arent_cross_checked(self):
        assert validate_property_rules(
            [_mapping("p1", "anything_goes")], [], [PropertyTitle("p1")],
        ) is None
