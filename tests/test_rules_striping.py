"""Tests for coverage accrual across spec buffers: each run-target buffer is verified whole, and
the task completes once every buffer carries a fresh prover stamp.

Covers:

- the pure helpers — which rules a logged run executed (``_executed_rules``) and whether the run
  history against the current authoring state adds up to full coverage (``_is_completion_history``);
- the buffer prover surface — ``submit_buffer`` / ``collect_results`` logging ``ProverRunLog``
  entries and stamping per-buffer completion, so coverage accrues across buffers and the completion
  reminder arrives once the last run-target buffer verifies;
- ``declared_rules_list``, with its certoraRun/typechecker subprocesses faked;
- the ``known_rules`` cross-check of ``validate_property_rules``.

The prover core is mocked throughout (the ``certora_prover`` fixture's seams:
``run_prover`` + ``declared_rules_list``); no prover jobs run.
"""
import asyncio
import json
from pathlib import Path

import pytest

from composer.prover.core import (
    ProverReport, SpecCompilationError, declared_rules_list
)
from composer.prover.ptypes import RulePath, StatusCodes
from composer.spec.cvl_generation import PropertyRuleMapping, validate_property_rules
from composer.spec.source.author import ExpectRuleFailure
from composer.spec.source.buffer_tools import put_buffer
from composer.spec.source.prover import (
    NagMarker, ProverHistoryItem, ProverRunLog, RuleSelection, StateWithSkips,
    VALIDATION_KEY, _executed_rules, _is_completion_history,
)
from composer.spec.source.spec_buffers import NamedBuffer, check_buffer_completion
from composer.spec.types import PropertyTitle, RuleName

from graphcore.testing import Scenario, ToolCallDict, tool_call_raw

from .conftest import ProverMock

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

    # ``**kwargs`` mirrors the real signature: the production call also passes
    # ``process_group`` so a timeout can kill the whole tree.
    async def fake_exec(*argv, cwd=None, stdout=None, stderr=None, **kwargs):
        assert cwd is not None
        assert kwargs.get("process_group") == 0, (
            "children must lead their own process group so a timeout kill "
            "reaches a grandchild JVM"
        )
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

        from composer.prover.core import BUILD_TIMEOUT_S, _run_captured

        rc, output = await asyncio.wait_for(
            _run_captured(
                sys.executable, "-c",
                "import sys; sys.stdout.write('o' * 200_000); "
                "sys.stderr.write('e' * 200_000); sys.exit(3)",
                cwd=tmp_path,
                timeout=BUILD_TIMEOUT_S,
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
# Coverage accrual across buffers: submit_buffer / collect_results
# =========================================================================

_SKIP = "expect_rule_failure"


@pytest.fixture(autouse=True)
def _accept_cvl(monkeypatch):
    """put_buffer validates writes by shelling out to the real CVL typechecker jar, which is absent
    in unit-test CI. These tests exercise the async submit/collect flow, not CVL parsing, so accept
    all writes."""
    monkeypatch.setattr(
        "composer.spec.source.buffer_tools.cvl_syntax_error", lambda *a, **k: None
    )


def _buf(name: str, *rules: str) -> NamedBuffer:
    """A run-target buffer declaring and owning exactly ``rules``; the mocked ``declared_rules_list``
    parses these declarations back out as the run's declared-rules ground truth."""
    cvl = "".join(f"rule {r} {{ assert true; }}\n" for r in rules)
    return NamedBuffer(
        name=name, cvl=cvl,
        property_rules={f"P-{name}": list(rules)} if rules else {},
    )


def _buffers(**named: NamedBuffer) -> dict[str, NamedBuffer]:
    return dict(named)


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


def _submit(name: str) -> ToolCallDict:
    # `name` is also tool_call_raw's first positional, so build the ToolCallDict directly.
    return {"name": "submit_buffer", "args": {"name": name}}


def _collect(wait: bool = False) -> ToolCallDict:
    return tool_call_raw("collect_results", wait=wait)


def _put(name: str, cvl: str) -> ToolCallDict:
    return {"name": "put_buffer", "args": {"name": name, "cvl": cvl,
                                           "property_rules": {}, "imports": [], "is_run_target": True}}


def _skip(rule_name: str, reason: str) -> ToolCallDict:
    return tool_call_raw(_SKIP, rule_name=rule_name, reason=reason)


def _scenario(
    certora_prover: ProverMock,
    buffers: dict[str, NamedBuffer],
    *,
    rule_skips: dict[str, str] | None = None,
    **responses: ProverReport | str,
):
    """A scenario over the buffer prover tools plus put_buffer and the skip tool. ``responses`` maps
    a buffer name to the report (or error string) its background job returns."""
    tools = [
        *certora_prover.buffers(dict(responses)),
        put_buffer(StateWithSkips),
        ExpectRuleFailure.as_tool(_SKIP),
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


# Dropped from the striping suite: test_rules_and_exclude_rules_mutually_exclusive,
# test_include_selection_reaches_conf_and_history, and test_exclude_selection_reaches_conf_and_history
# tested the removed single-run rule-scoping surface (verify_spec's include/exclude args plumbed into
# one conf). A buffer is verified whole, so there is no include/exclude arg to plumb; the surviving
# "a run reaches prover_history" intent is kept below as test_buffer_run_is_logged_in_history.


@pytest.mark.asyncio
class TestBufferCoverage:
    async def test_a_buffer_that_does_not_compile_comes_back_to_the_agent(
        self, certora_prover: ProverMock, monkeypatch
    ):
        """The rule-listing pre-pass runs the compiler before the prover. A buffer that fails to
        compile is the author agent's to fix and the compiler says exactly where, so it has to arrive
        as a collect_results tool result rather than sinking the whole run over a repairable mistake."""
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
            certora_prover, _buffers(b=_buf("b", "a", "b")),
        ).turns(
            _submit("b"), _collect(wait=True),
        ).run_last_single_tool("collect_results")
        assert "failed to compile" in msg
        assert "invariants.spec:34:5" in msg
        assert "non-envfree" in msg
        # The prover is never reached — there is nothing to verify.
        assert certora_prover.calls == []

    async def test_buffer_run_is_logged_in_history(self, certora_prover: ProverMock):
        # A whole-buffer run is logged in prover_history against the buffer's current state, with its
        # declared rules and results — the buffer analogue of the old include/exclude conf-history check
        # (rules is None because a buffer is verified whole, not rule-scoped).
        history = await _scenario(
            certora_prover, _buffers(b=_buf("b", "a", "b")),
            b=_report(a=True, b=True),
        ).turns(
            _submit("b"), _collect(wait=True),
        ).map_run(lambda st: st["prover_history"])
        [entry] = [h for h in history if h["sort"] == "run"]
        assert entry["buffer"] == "b"
        assert entry["rules"] is None
        assert sorted(entry["declared_rules"]) == ["a", "b"]
        assert (RulePath(rule="a"), "VERIFIED") in entry["prover_results"]

    async def test_all_buffers_verified_completes(self, certora_prover: ProverMock):
        # Coverage accrues across buffers: two run-target buffers, each verified whole, together
        # complete the task — no single full run required.
        st = await _scenario(
            certora_prover, _buffers(b1=_buf("b1", "a"), b2=_buf("b2", "b")),
            b1=_report(a=True), b2=_report(b=True),
        ).turns(
            _submit("b1"), _submit("b2"), _collect(wait=True), _collect(wait=True),
        ).run()
        assert _prover_complete(st) is None

    async def test_one_unverified_buffer_blocks_completion(self, certora_prover: ProverMock):
        # One run-target buffer verified, the other never submitted → coverage is partial.
        st = await _scenario(
            certora_prover, _buffers(b1=_buf("b1", "a"), b2=_buf("b2", "b")),
            b1=_report(a=True),
        ).turns(
            _submit("b1"), _collect(wait=True),
        ).run()
        assert _prover_complete(st) is not None

    async def test_coverage_accrues_across_separate_collect_rounds(self, certora_prover: ProverMock):
        # The buffers are verified in separate submit/collect rounds; completion arrives on the round
        # that verifies the last run-target buffer.
        st = await _scenario(
            certora_prover, _buffers(b1=_buf("b1", "a"), b2=_buf("b2", "b")),
            b1=_report(a=True), b2=_report(b=True),
        ).turns(
            _submit("b1"), _collect(wait=True),
            _submit("b2"), _collect(wait=True),
        ).run()
        assert _prover_complete(st) is None

    async def test_editing_a_buffer_resets_its_coverage(self, certora_prover: ProverMock):
        # Both buffers verify, then editing b1 changes its digest, so its prover stamp goes stale and
        # overall completion is withdrawn until b1 is re-verified.
        st = await _scenario(
            certora_prover, _buffers(b1=_buf("b1", "a"), b2=_buf("b2", "b")),
            b1=_report(a=True), b2=_report(b=True),
        ).turns(
            _submit("b1"), _submit("b2"), _collect(wait=True), _collect(wait=True),
            _put("b1", "rule a { assert true; }\n// tightened\n"),
        ).run()
        assert _prover_complete(st) is not None

    async def test_skipped_rule_failure_counts_toward_coverage(self, certora_prover: ProverMock):
        # b2's rule fails but is skipped, so its buffer still completes and coverage is met across both.
        st = await _scenario(
            certora_prover, _buffers(b1=_buf("b1", "a"), b2=_buf("b2", "b")),
            b1=_report(a=True), b2=_report(b=False),
        ).turn(
            _skip("b", "known limitation"),
        ).turns(
            _submit("b1"), _submit("b2"), _collect(wait=True), _collect(wait=True),
        ).run()
        assert _prover_complete(st) is None

    async def test_completion_reminder_delivered_once_all_buffers_verify(self, certora_prover: ProverMock):
        reminders = await _scenario(
            certora_prover, _buffers(b1=_buf("b1", "a"), b2=_buf("b2", "b")),
            b1=_report(a=True), b2=_report(b=True),
        ).turns(
            _submit("b1"), _submit("b2"), _collect(wait=True), _collect(wait=True),
        ).map_run(lambda st: st["reminders_channel"])
        assert any("verified at its current content" in r for r in reminders)

    async def test_no_completion_reminder_while_coverage_is_partial(self, certora_prover: ProverMock):
        reminders = await _scenario(
            certora_prover, _buffers(b1=_buf("b1", "a"), b2=_buf("b2", "b")),
            b1=_report(a=True),
        ).turns(
            _submit("b1"), _collect(wait=True),
        ).map_run(lambda st: st["reminders_channel"])
        assert not any("verified at its current content" in r for r in reminders)


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
