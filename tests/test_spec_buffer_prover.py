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
from composer.spec.source.prover import (
    StateWithSkips, VALIDATION_KEY, ProverRunLog, completing_run_specs, materialize_buffers,
)
from composer.prover.ptypes import RulePath
from composer.spec.source.spec_buffers import (
    NamedBuffer, buffer_state_digest, check_buffer_completion,
)

from .conftest import ProverMock


SHARED = "ghost g(uint) returns uint;\n"


def test_materialize_buffers_writes_under_the_component_slug_dir(tmp_path):
    # Every buffer is materialized at certora/specs/<slug>/<name>.spec, so a buffer's ``../summaries/``
    # import resolves to the shared summaries a level up and the on-disk names match the delivered tree.
    buffers = {
        "base": NamedBuffer(name="base", cvl='import "../summaries/x.spec";\nrule r { assert true; }\n'),
        "inv": NamedBuffer(name="inv", cvl="rule i { assert true; }\n", is_run_target=False),
    }
    with materialize_buffers(str(tmp_path), buffers, "myslug") as paths:
        assert paths == {
            "base": "certora/specs/myslug/base.spec",
            "inv": "certora/specs/myslug/inv.spec",
        }
        assert (tmp_path / "certora/specs/myslug/base.spec").read_text().startswith('import "../summaries')
        assert (tmp_path / "certora/specs/myslug/inv.spec").is_file()
    # the whole set is removed on exit
    assert not (tmp_path / "certora/specs/myslug/base.spec").exists()


def _runlog(tc, digest, link, rule, selector):
    return ProverRunLog(
        tool_call_id=tc, prover_results=[(RulePath(rule=rule), "VERIFIED")],
        rules={"sort": "include", "selector": selector}, spec_digest="", sort="run",
        declared_rules=["r_a", "r_b"], state_digest=digest, buffer="both", link=link,
    )


def test_completing_run_specs_unions_stripes_and_drops_stale():
    """A buffer's rules proven across striped runs at its current digest yield every run's (link, spec),
    newest first and deduped; a run at a superseded digest is excluded — the report unions over these."""
    bufs = _buffers2()  # run-target "both" (r_a, r_b) + shared
    dig = buffer_state_digest(bufs, "both", version_history=[])
    history = [
        _runlog("t0", "STALE_DIGEST", "old-link", "r_a", ["r_a"]),      # superseded content -> excluded
        _runlog("t1", dig, "link-a", "r_a", ["r_a"]),                   # stripe rule=[r_a]
        _runlog("t2", dig, "link-b", "r_b", ["r_b"]),                   # stripe rule=[r_b]
        _runlog("t3", dig, "link-a", "r_a", ["r_a"]),                   # same link again -> deduped
    ]
    runs = completing_run_specs(history, bufs, version_history=[], slug="myslug")
    assert runs == [("link-a", "myslug/both.spec"), ("link-b", "myslug/both.spec")]


def _buf(name: str, rule: str) -> NamedBuffer:
    return NamedBuffer(
        name=name,
        cvl=f'import "shared.spec";\nrule {rule} {{ assert true; }}\n',
        property_rules={f"P-{name}": [rule]},
    )


def _buffers() -> dict[str, NamedBuffer]:
    return {
        "shared": NamedBuffer(name="shared", cvl=SHARED, is_run_target=False),
        "easy": _buf("easy", "r_easy"),
        "hard": _buf("hard", "r_hard"),
    }


def _buf2(name: str, r1: str, r2: str) -> NamedBuffer:
    return NamedBuffer(
        name=name,
        cvl=f'import "shared.spec";\nrule {r1} {{ assert true; }}\nrule {r2} {{ assert true; }}\n',
        property_rules={f"P-{r1}": [r1], f"P-{r2}": [r2]},
    )


def _buffers2() -> dict[str, NamedBuffer]:
    """One shared buffer plus a two-rule run target, for rule-striping within a single buffer."""
    return {
        "shared": NamedBuffer(name="shared", cvl=SHARED, is_run_target=False),
        "both": _buf2("both", "r_a", "r_b"),
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


def _submit_sel(name: str, *, rule=None, exclude_rules=None):
    args: dict = {"name": name}
    if rule is not None:
        args["rule"] = rule
    if exclude_rules is not None:
        args["exclude_rules"] = exclude_rules
    return {"name": "submit_buffer", "args": args}


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
        st["buffers"], st["validations"], ["prover"], skipped=[], version_history=[],
        config=st["config"],
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
            return buffer_state_digest(st["buffers"], n, version_history=[], config=st["config"])

        assert st["validations"].get("prover:easy") == digest("easy")
        assert st["validations"].get("prover:hard") == digest("hard")
        assert _prover_complete(st) is None

    async def test_resubmit_same_content_does_not_duplicate(self, certora_prover: ProverMock):
        """Re-submitting a buffer at unchanged content (its job still in flight or finished but not yet
        collected) must not launch a second prover job — the guard tells the agent to collect instead."""
        st = await _scenario(
            certora_prover, _buffers(), easy=_report(r_easy=True),
        ).turns(
            _submit("easy"),
            _submit("easy"),      # identical content, not collected → must NOT re-run
            _collect(wait=True),
        ).run()
        easy_runs = [c for c in certora_prover.calls if "easy" in str(c.conf.get("verify", ""))]
        assert len(easy_runs) == 1, f"expected one prover run for easy, got {len(easy_runs)}"
        assert st["validations"].get("prover:easy") == buffer_state_digest(
            st["buffers"], "easy", version_history=[], config=st["config"]
        )

    async def test_rule_stripe_unions_to_completion(self, certora_prover: ProverMock):
        """Two rule subsets of one buffer, submitted separately, each verify their own rule; the runs
        union at the same digest so the buffer completes without ever running both rules together."""
        st = await _scenario(
            certora_prover, _buffers2(), both=_report(r_a=True, r_b=True),
        ).turns(
            _submit_sel("both", rule=["r_a"]),
            _submit_sel("both", rule=["r_b"]),
            _collect(wait=True),
            _collect(wait=True),
        ).run()
        assert _prover_complete(st) is None
        runs = [it for it in st["prover_history"] if it["sort"] == "run" and it.get("buffer") == "both"]
        assert len(runs) == 2
        assert {tuple(r["rules"]["selector"]) for r in runs} == {("r_a",), ("r_b",)}
        assert all(len(r["prover_results"]) == 1 for r in runs)  # each run reported only its own rule

    async def test_striped_subsets_launch_as_separate_jobs(self, certora_prover: ProverMock):
        """Different subsets of one buffer at the same content are distinct jobs — both launch, unlike a
        re-submit of the identical subset."""
        await _scenario(
            certora_prover, _buffers2(), both=_report(r_a=True, r_b=True),
        ).turns(
            _submit_sel("both", rule=["r_a"]),
            _submit_sel("both", rule=["r_b"]),
            _collect(wait=True), _collect(wait=True),
        ).run()
        both_runs = [c for c in certora_prover.calls if "both" in str(c.conf.get("verify", ""))]
        assert len(both_runs) == 2

    async def test_resubmit_same_subset_does_not_duplicate(self, certora_prover: ProverMock):
        """Re-submitting the identical subset at unchanged content does not launch a second job."""
        await _scenario(
            certora_prover, _buffers2(), both=_report(r_a=True, r_b=True),
        ).turns(
            _submit_sel("both", rule=["r_a"]),
            _submit_sel("both", rule=["r_a"]),  # identical subset → deduped
            _collect(wait=True),
        ).run()
        both_runs = [c for c in certora_prover.calls if "both" in str(c.conf.get("verify", ""))]
        assert len(both_runs) == 1

    async def test_submit_rejects_bad_selection(self, certora_prover: ProverMock):
        """An unknown rule, or `rule` and `exclude_rules` together, is rejected without launching a job."""
        await _scenario(
            certora_prover, _buffers2(), both=_report(r_a=True, r_b=True),
        ).turns(
            _submit_sel("both", rule=["r_nonexistent"]),
            _submit_sel("both", rule=["r_a"], exclude_rules=["r_b"]),
        ).run()
        assert certora_prover.calls == []

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

    async def test_shared_edit_reports_all_stale_importers_at_once(self, certora_prover: ProverMock):
        """A shared-buffer edit invalidates every importer, and check_buffer_completion names them all
        in one message — not just the first."""
        st = await _scenario(
            certora_prover, _buffers(),
            easy=_report(r_easy=True), hard=_report(r_hard=True),
        ).turns(
            _submit("easy"), _submit("hard"), _collect(wait=True), _collect(wait=True),
            _put_shared(SHARED + "ghost h(uint) returns uint;\n"),
        ).run()
        msg = _prover_complete(st)
        assert msg is not None
        assert "'easy'" in msg and "'hard'" in msg

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

    async def test_isolated_pairs_do_not_drain_each_others_jobs(self, certora_prover: ProverMock):
        """Each ``make_buffer_tools()`` pair owns its job state: submitting on one pair must not let a
        second pair's blocking collect drain that job. A second pair stands in for a concurrent
        component author, which shares the prover semaphore but must never share the result queue."""
        pair_a = certora_prover.buffers({"easy": _report(r_easy=True)})
        pair_b = certora_prover.buffers({"easy": _report(r_easy=True)})

        def scene(pair):
            return Scenario(StateWithSkips, *pair).init(
                curr_spec=None, buffers=_buffers(), skipped=[], property_rules=[], validations={},
                required_validations=[VALIDATION_KEY], rule_skips={}, config={"files": ["src/Foo.sol"]},
                reminders_channel=[], version_history=[],
            )

        # A submits easy (same tool pair reused across turns, so its background job persists).
        await scene(pair_a).turns(_submit("easy")).run()
        # B submitted nothing, so its blocking collect has no job of its own to await and drains nothing.
        # A shared queue would let B drain A's result here.
        st_b = await scene(pair_b).turns(_collect(wait=True)).run()
        assert st_b["validations"].get("prover:easy") is None
        # A still finds and stamps its own job.
        st_a = await scene(pair_a).turns(_collect(wait=True)).run()
        assert st_a["validations"].get("prover:easy") is not None

    async def test_collect_without_finished_jobs_does_not_block(self, certora_prover: ProverMock):
        """A non-blocking collect with nothing submitted returns a status board rather than hanging."""
        st = await _scenario(certora_prover, _buffers()).turns(
            _collect(wait=False),
        ).run()
        assert _prover_complete(st) is not None  # nothing verified yet

    async def test_feedback_stamps_per_buffer(self):
        """The feedback tool reviews each run-target buffer in isolation and stamps feedback:<buffer>
        at the buffer's current digest."""
        from dataclasses import dataclass
        from composer.spec.source.author import EditorAwareFeedbackTool, _PerBufferJudge

        @dataclass
        class _V:
            good: bool
            feedback: str

        async def judge(snap, spec, skipped, rebuttals, within_tool):
            return _V(good=True, feedback="")

        tool = EditorAwareFeedbackTool.bind(
            _PerBufferJudge(build=lambda name, claimed: judge, properties=[])
        ).as_tool("feedback_tool")
        buffers = {
            "shared": NamedBuffer(name="shared", cvl=SHARED, is_run_target=False),
            "easy": _buf("easy", "r_easy"),
        }
        state = {
            "buffers": buffers, "vfs": {}, "curr_spec": None, "skipped": [],
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
            buffers, "easy", version_history=[], include_claim=True
        )

    async def test_skip_review_stamps_once_not_per_buffer(self):
        """A skipped property is reviewed once as the standalone skips_review unit — not folded into
        each buffer's stamp — and the per-buffer stamp stays independent of the skip."""
        from dataclasses import dataclass
        from composer.spec.source.author import EditorAwareFeedbackTool, _PerBufferJudge
        from composer.spec.source.spec_buffers import SKIPS_VALIDATION_KEY, skips_review_digest
        from composer.authoring.state import SkippedProperty
        from composer.spec.types import PropertyTitle

        @dataclass
        class _V:
            good: bool
            feedback: str

        seen: list[str] = []

        async def judge(snap, spec, skipped, rebuttals, within_tool):
            seen.append("skips" if skipped else "buffer")
            return _V(good=True, feedback="")

        tool = EditorAwareFeedbackTool.bind(
            _PerBufferJudge(build=lambda name, claimed: judge, properties=[])
        ).as_tool("feedback_tool")
        buffers = {
            "shared": NamedBuffer(name="shared", cvl=SHARED, is_run_target=False),
            "easy": _buf("easy", "r_easy"),
        }
        skipped = [SkippedProperty(property_title=PropertyTitle("P-x"), reason="cannot express in CVL")]
        state = {
            "buffers": buffers, "vfs": {}, "curr_spec": None, "skipped": skipped,
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
        assert val.get(SKIPS_VALIDATION_KEY) == skips_review_digest(
            buffers, skipped=[("P-x", "cannot express in CVL")], version_history=[]
        )
        assert val.get("feedback:easy") == buffer_state_digest(
            buffers, "easy", version_history=[], include_claim=True
        )
        assert seen.count("skips") == 1  # reviewed once, not once per buffer

    async def test_publish_rejects_a_declared_but_unmapped_rule(self):
        """The publish gate refuses when a buffer's prover run declared a rule/invariant that no
        property maps — the published mapping would otherwise leave it attributed to no property."""
        from composer.spec.source.author import PublishResultTool
        from composer.spec.types import PropertyTitle

        buffers = {
            "shared": NamedBuffer(name="shared", cvl=SHARED, is_run_target=False),
            "easy": _buf("easy", "r_easy"),
            "hard": _buf("hard", "r_hard"),
        }

        def pdig(n: str) -> str:
            return buffer_state_digest(buffers, n, version_history=[])

        def fdig(n: str) -> str:
            return buffer_state_digest(buffers, n, version_history=[], include_claim=True)

        def _run(buf: str, declared: list[str]) -> ProverRunLog:
            return ProverRunLog(
                tool_call_id="t", prover_results=[], rules={"sort": "include", "selector": declared},
                spec_digest="", sort="run", declared_rules=declared, state_digest=pdig(buf),
                buffer=buf, link="l",
            )

        state = {
            "buffers": buffers, "vfs": {}, "curr_spec": None, "skipped": [],
            "validations": {
                "prover:easy": pdig("easy"), "feedback:easy": fdig("easy"),
                "prover:hard": pdig("hard"), "feedback:hard": fdig("hard"),
            },
            "required_validations": ["prover", "feedback"],
            "version_history": [], "messages": [], "property_rules": [], "rule_skips": {},
            "config": {}, "reminders_channel": [], "failed": None, "budget_curtailed": False,
            "prover_history": [
                _run("easy", ["r_easy", "inv_x"]),  # inv_x declared but named by no property -> unmapped
                _run("hard", ["r_hard"]),
            ],
        }
        tool = PublishResultTool.bind(
            [PropertyTitle("P-easy"), PropertyTitle("P-hard")]
        ).as_tool("result")
        res = await tool.ainvoke(
            {"name": "result", "args": {"state": state, "commentary": ""},
             "id": "t", "type": "tool_call"}
        )
        msg = res if isinstance(res, str) else getattr(res, "content", str(res))
        assert "inv_x" in msg

    async def test_publish_rejects_requireinvariant_of_an_unproved_invariant(self, monkeypatch):
        """The publish gate refuses when a buffer requireInvariant's an invariant it does not declare —
        e.g. one that lives only in an unproven shared buffer it imports, so this run never verifies it."""
        from composer.spec.source.author import PublishResultTool
        from composer.spec.types import PropertyTitle

        # `easy` cites requireInvariant inv_shared but declares only r_easy; inv_shared lives in the
        # imported, never-proved shared buffer.
        buffers = {
            "shared": NamedBuffer(
                name="shared", cvl="invariant inv_shared() true;\n", is_run_target=False
            ),
            "easy": NamedBuffer(
                name="easy",
                cvl='import "shared.spec";\nrule r_easy() { requireInvariant inv_shared(); assert true; }\n',
                property_rules={"P-easy": ["r_easy"]},
            ),
            "hard": _buf("hard", "r_hard"),
        }

        def pdig(n: str) -> str:
            return buffer_state_digest(buffers, n, version_history=[])

        def fdig(n: str) -> str:
            return buffer_state_digest(buffers, n, version_history=[], include_claim=True)

        def _run(buf: str, declared: list[str]) -> ProverRunLog:
            return ProverRunLog(
                tool_call_id="t", prover_results=[], rules={"sort": "include", "selector": declared},
                spec_digest="", sort="run", declared_rules=declared, state_digest=pdig(buf),
                buffer=buf, link="l",
            )

        state = {
            "buffers": buffers, "vfs": {}, "curr_spec": None, "skipped": [],
            "validations": {
                "prover:easy": pdig("easy"), "feedback:easy": fdig("easy"),
                "prover:hard": pdig("hard"), "feedback:hard": fdig("hard"),
            },
            "required_validations": ["prover", "feedback"],
            "version_history": [], "messages": [], "property_rules": [], "rule_skips": {},
            "config": {}, "reminders_channel": [], "failed": None, "budget_curtailed": False,
            "prover_history": [_run("easy", ["r_easy"]), _run("hard", ["r_hard"])],
        }
        # The gate reads requireInvariant citations from the CVL AST (ASTExtraction.jar); mock that
        # here so the unit suite stays JVM-free — this test exercises the gate wiring, not the parse.
        monkeypatch.setattr(
            "composer.spec.source.author.requireinvariant_citations",
            lambda cvl: {"inv_shared"} if "inv_shared" in cvl else set(),
        )
        tool = PublishResultTool.bind(
            [PropertyTitle("P-easy"), PropertyTitle("P-hard")]
        ).as_tool("result")
        res = await tool.ainvoke(
            {"name": "result", "args": {"state": state, "commentary": ""},
             "id": "t", "type": "tool_call"}
        )
        msg = res if isinstance(res, str) else getattr(res, "content", str(res))
        assert "inv_shared" in msg
