"""What the host hands ``validate``, and what it does with the answer.

A report row is a *check*; the thing the host actually runs is a **target**, and several checks can
share one (Crucible checks a component's whole property set in a single fuzz run). The host owns
that grouping — it decides what runs and in what order — so it passes each target together with the
checks that target covers, rather than leaving the wheel to recover them by re-deriving its own
``checks()`` and filtering by name.

The grouping lives in the ``validate_spec`` tool the author calls, so these drive that tool directly
against a recording wheel. No LLM is involved.
"""

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from langchain_core.messages import ToolMessage

from composer.authoring.state import SkippedProperty, spec_digest
from composer.rustapp import adapter
from composer.rustapp.descriptor import AppDescriptor
from composer.rustapp.session import (
    VALIDATE_KEY, GateDeps, RustSessionState, SessionResult, ValidateSpec,
)
from composer.rustapp.wire import Target, Check, ValidateCoverageError
from composer.spec.source.report.schema import Outcome
from composer.spec.types import PropertyFormulation
from tests.conftest import wire_descriptor, wire_verdict

SPEC = "fn c_farms(f: &mut Fixture) {}"

#: Two properties checked by one shared target, and a third that is its own.
CHECKS = [
    Check(property="stake matches", name="c_stake", target="c_farms"),
    Check(property="no double stake", name="c_dbl", target="c_farms"),
    Check(property="fees capped", name="c_fees", target=None),
]


@dataclass
class _Wheel:
    """The callouts the gate reaches. ``validate`` answers GOOD for every covered unit."""

    #: Every ``Target`` the host passed, in call order.
    targets: list[Target] = field(default_factory=list)
    outcome: str = "GOOD"
    #: Checks the wheel leaves without a verdict though its target covers them.
    omit: frozenset[str] = frozenset()

    def checks(self, _input_json: str) -> str:
        return json.dumps([c.model_dump() for c in CHECKS])

    def judge(self, _input_json: str) -> str | None:
        return None  # no judge, so no review machinery is bound

    def validate(
        self, _input_json: str, _spec: str, target_json: str, _workdir: str, _sandbox: str
    ) -> str:
        target = Target.model_validate_json(target_json)
        self.targets.append(target)
        return json.dumps({
            "kind": "verdicts",
            "verdicts": [
                [c.name, wire_verdict(self.outcome)]
                for c in target.checks if c.name not in self.omit
            ],
        })


def _state(**kw) -> RustSessionState:
    base = {
        "messages": [],
        "curr_spec": SPEC,
        "skipped": [],
        "validations": {},
        "required_validations": [VALIDATE_KEY],
        "property_checks": [],
        "expected_failures": {},
        "verdicts": {},
        "failed": None,
    }
    return cast(RustSessionState, {**base, **kw})


def _deps(wheel: _Wheel, tmp_path: pathlib.Path) -> GateDeps:
    return GateDeps(
        module=cast(Any, wheel),
        input_json="{}",
        workdir=tmp_path,
        sandbox_json="{}",
        emit=lambda _kind, _payload: None,
        checks=list(CHECKS),
    )


async def _validate(wheel: _Wheel, tmp_path: pathlib.Path, state=None, checks=None):
    """Invoke the gate tool exactly as the graph would, and return what it handed back."""
    tool = ValidateSpec.bind(_deps(wheel, tmp_path)).as_tool("validate_spec")
    out = await tool.ainvoke({
        "name": "validate_spec",
        "type": "tool_call",
        "id": "t1",
        "args": {
            "state": state if state is not None else _state(),
            "tool_call_id": "t1",
            "checks": checks,
        },
    })
    # A tool that returns a plain string is wrapped in a ToolMessage by ``ainvoke``; one that
    # returns a state update hands the Command back as-is.
    return out.content if isinstance(out, ToolMessage) else out


@pytest.mark.asyncio
async def test_each_distinct_target_runs_once_carrying_the_checks_it_covers(tmp_path):
    wheel = _Wheel()
    await _validate(wheel, tmp_path)

    # One run per DISTINCT target, not per check — the two properties sharing `c_farms` are one run.
    assert [t.name for t in wheel.targets] == ["c_farms", "c_fees"]
    # …and each run is told exactly which checks it owes a verdict for, so the wheel never re-derives
    # the grouping the host just computed.
    assert [[c.name for c in t.checks] for t in wheel.targets] == [["c_stake", "c_dbl"], ["c_fees"]]
    # Each carries its property title, which is what a backend attributes a counterexample by.
    assert wheel.targets[0].checks[0].property == "stake matches"


@pytest.mark.asyncio
async def test_every_covered_check_gets_its_verdict_recorded(tmp_path):
    # A shared target answers for several checks in one call; all of them must reach the state, or
    # the report silently loses the checks that shared a run.
    command = await _validate(_Wheel(), tmp_path)

    verdicts = command.update["verdicts"]
    assert set(verdicts) == {"c_stake", "c_dbl", "c_fees"}
    assert all(v.outcome is Outcome.GOOD for v in verdicts.values())


@pytest.mark.asyncio
async def test_a_clean_full_run_stamps_the_draft_it_saw(tmp_path):
    command = await _validate(_Wheel(), tmp_path)
    assert command.update["validations"] == {VALIDATE_KEY: spec_digest(SPEC, [])}


@pytest.mark.asyncio
async def test_a_partial_run_never_stamps(tmp_path):
    # Running a subset is for iterating on one problem. Letting it stamp would publish a spec whose
    # other checks nothing had run.
    out = await _validate(_Wheel(), tmp_path, checks=["c_fees"])
    assert isinstance(out, str) and "partial" in out


@pytest.mark.asyncio
async def test_a_partial_run_only_runs_the_targets_it_was_asked_for(tmp_path):
    wheel = _Wheel()
    await _validate(wheel, tmp_path, checks=["c_fees"])
    assert [t.name for t in wheel.targets] == ["c_fees"]


@pytest.mark.asyncio
async def test_an_unknown_check_name_is_refused_rather_than_silently_dropped(tmp_path):
    wheel = _Wheel()
    out = await _validate(wheel, tmp_path, checks=["c_invented"])
    assert isinstance(out, str) and "c_invented" in out
    assert wheel.targets == [], "nothing ran"


@pytest.mark.asyncio
async def test_a_failing_check_leaves_the_gate_unstamped(tmp_path):
    out = await _validate(_Wheel(outcome="BAD"), tmp_path)
    assert isinstance(out, str)
    assert "c_stake" in out and "NOT satisfied" in out


@pytest.mark.asyncio
async def test_a_failure_the_author_marked_as_the_finding_stamps(tmp_path):
    # The counterexample IS the result. Marking it is what lets the run be published with the
    # failure recorded and explained.
    marked = _state(expected_failures={c.name: "the program really does allow it" for c in CHECKS})
    command = await _validate(_Wheel(outcome="BAD"), tmp_path, state=marked)
    assert VALIDATE_KEY in command.update["validations"]


@pytest.mark.asyncio
async def test_a_wheel_that_leaves_a_covered_check_unanswered_is_refused(tmp_path):
    # An unanswered check is not a failing check: nothing downstream has anything to object to, so
    # silence would read as a clean run. A wheel that answers for none of a target's checks is the
    # extreme of it — the gate would stamp a component nothing had checked.
    with pytest.raises(ValidateCoverageError, match="c_dbl"):
        await _validate(_Wheel(omit=frozenset({"c_dbl"})), tmp_path)
    with pytest.raises(ValidateCoverageError):
        await _validate(_Wheel(omit=frozenset({"c_stake", "c_dbl", "c_fees"})), tmp_path)


@pytest.mark.asyncio
async def test_a_skipped_propertys_target_is_not_run(tmp_path):
    # Its property is not being formalized, so there is nothing to check and nothing to report.
    wheel = _Wheel()
    skipped = _state(skipped=[SkippedProperty(property_title="fees capped", reason="no oracle")])
    await _validate(wheel, tmp_path, state=skipped)
    assert [t.name for t in wheel.targets] == ["c_farms"]


# ---------------------------------------------------------------------------
# What the formalizer makes of a finished session
# ---------------------------------------------------------------------------

@dataclass
class _Feat:
    display_name: str = "Farms"
    slug: str = "farms"

    def feature_json(self) -> dict[str, Any]:
        return {"name": self.display_name, "slug": self.slug}


@dataclass
class _Source:
    project_root: str
    contract_name: str = "lending"


@dataclass
class _Ctx:
    recursion_limit: int = 10


@dataclass
class _Run:
    source: _Source
    ctx: _Ctx
    env: None = None


@pytest.mark.asyncio
async def test_the_result_carries_the_targets_the_gating_run_covered(monkeypatch, tmp_path):
    # The deliverable's sections are keyed by target name, so a skipped property's target must not
    # be claimed as checked.
    async def fake_session(**_kw):
        return SessionResult(
            commentary="done",
            spec=SPEC,
            skipped=[SkippedProperty(property_title="fees capped", reason="no oracle")],
            property_checks=[("stake matches", ["c_stake"]), ("no double stake", ["c_dbl"])],
            verdicts={},
            expected_failures={},
        )

    monkeypatch.setattr(adapter, "run_session", fake_session)
    formalizer = adapter.RustFormalizer(
        cast(Any, _Wheel()), AppDescriptor.model_validate(wire_descriptor())
    )
    props = [
        PropertyFormulation(title=c.property, sort="invariant", description="d") for c in CHECKS
    ]
    result = await formalizer.formalize(
        "Farms", cast(Any, _Feat()), props, cast(Any, _Ctx()),
        cast(Any, _Run(_Source(str(tmp_path)), _Ctx())),
    )

    assert isinstance(result, adapter.RustFormalResult)
    assert result.targets == ["c_farms"]
    assert [s.property_title for s in result.skipped] == ["fees capped"]
    assert dict(result.checks) == {"stake matches": ["c_stake"], "no double stake": ["c_dbl"]}
