"""End-to-end integration test for the stuck-rule nag ("prover nagging")
behavior of ``verify_spec``.

Runs the full autoprove pipeline on the Counter scenario with the nag-variant
tape (``install_nag_tape``). The prover core itself is mocked — the nag
machinery under test lives entirely in ``verify_spec``'s post-processing,
above the ``run_prover`` seam — so no cloud (or local) prover jobs run:
``_fake_run_prover`` reads the spec each call targets and reports every
declared rule VERIFIED except the permanently-stuck one, which reports
SANITY_FAILED (the status the real ``rule_sanity`` check would produce for
its vacuous body).

The taped author runs the spec ``STUCK_RULE_NAG_THRESHOLD`` times, nudging it
with a trailing comment between runs (the streak keys on (rule, status), not
spec digest — and the distinct digests stay clear of verify_spec's
identical-spec re-run gate); the last run must append a ``NagMarker`` to
``prover_history`` and queue the stuck-rule reminder, which the author monitor
injects into the conversation as a ``<system-reminder>`` HumanMessage. The
tape then reacts as the reminder suggests (marks the rule expected-to-fail),
re-verifies, and publishes.

Pass/fail: the pipeline completes without raising, AND the reminder actually
reached the author's prompt. The fake LLM is the only observer of the real
conversation, so delivery is asserted by a ``HarnessFakeLLM`` subclass that
sniffs every prompt it is asked to answer for ``<system-reminder>`` blocks —
exactly once per prompt (a re-fired nag or a non-draining reminders channel
would stack duplicates).

Still marked ``expensive``: no prover money is spent, but the run needs the
testcontainer Postgres and the real local CVL toolchain (``put_cvl_raw``'s
Typechecker gate), which puts it well outside the routine fast pass. Run with
``-m expensive``.
"""
from pathlib import Path
from typing import Any, override

import pytest
from pydantic import Field

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompt_values import PromptValue

from composer.diagnostics.timing import RunSummary, get_current_task_id
from composer.pipeline.core import formalize_task_id
from composer.prover.core import ProverReport
from composer.prover.ptypes import RulePath, StatusCodes
from composer.spec.source.autoprove_common import autoprove_executor
from composer.testing.harness_tape import HarnessFakeLLM
from composer.testing.ui_harness_autoprove_Counter import (
    NAG_STUCK_RULE,
    autoprove_nag_lanes,
    install_nag_tape,
)
from composer.ui.autoprove_console import AutoProveConsoleHandler

from tests.conftest import (
    SPEC_DECL_RE, conf_of_prover_call, needs_postgres, spec_of_prover_conf,
)
from tests.test_autoprove_integration import _install_mocks, _make_args

pytestmark = [pytest.mark.expensive, needs_postgres, pytest.mark.asyncio]

_SCENARIO_NAME = "autoprove_counter"

# Stable prefix of the reminder verify_spec queues when the stuck-rule
# detector fires (the "3" is spelled by the message itself, so match short of it).
_NAG_SNIPPET = "identical failures on the last"


async def _fake_run_prover(
    folder: Path, args: list[str], tool_call_id: str,
    prover_opts: Any, callbacks: Any, cex: Any,
) -> ProverReport:
    """Stand-in for ``run_prover``, patched over the binding ``verify_spec``
    calls through (the same seam the ``certora_prover`` conftest fixture
    patches). Parses the conf the tool just wrote, reads the spec it verifies,
    and reports every declared rule/invariant VERIFIED except the permanently
    stuck ``NAG_STUCK_RULE`` → SANITY_FAILED. Content-derived, so both
    authoring lanes are served without ordering assumptions."""
    conf = conf_of_prover_call(folder, args)
    spec_text = spec_of_prover_conf(folder, conf)
    statuses: dict[RulePath, StatusCodes] = {
        RulePath(rule=name): ("SANITY_FAILED" if name == NAG_STUCK_RULE else "VERIFIED")
        for name in SPEC_DECL_RE.findall(spec_text)
    }
    assert statuses, f"fake prover: no rule/invariant declarations in {conf['verify']}"
    return ProverReport(
        raw_rule_status=statuses,
        result_str="\n".join(f"{p.rule}: {s}" for p, s in statuses.items()),
        link="https://prover.example/fake-run",
    )


async def _fake_declared_rules(folder: Path, args: list[str]) -> list[str]:
    """Stand-in for ``declared_rules_list`` — the same content-derived ground
    truth, without the certoraRun build + typechecker ``-listRules`` subprocesses."""
    return SPEC_DECL_RE.findall(spec_of_prover_conf(folder, conf_of_prover_call(folder, args)))


class _ReminderSniffingLLM(HarnessFakeLLM):
    """Records, per LLM call, the ``<system-reminder>`` human messages present
    in the prompt (keyed by tape lane). The monitor-injected nag reminder lives
    in the author's message history, and the fake LLM is the only place the
    real conversation can be observed."""

    sniffed: list[tuple[str, list[str]]] = Field(default_factory=list, exclude=True)

    @override
    async def ainvoke(self, input: Any, config: Any = None, *, stop: Any = None, **kwargs: Any) -> AIMessage:
        msgs = list(input.to_messages()) if isinstance(input, PromptValue) else list(input)
        reminders : list[str] = [
            m.text for m in msgs
            if isinstance(m, HumanMessage) and "<system-reminder>" in m.text
        ]
        if reminders:
            self.sniffed.append((get_current_task_id() or "<no lane>", reminders))
        return await super().ainvoke(input, config, stop=stop, **kwargs)


async def test_prover_nag_fires_and_run_survives(scenario_provider, langgraph_db, monkeypatch):
    scenario_dir = scenario_provider.by_name(_SCENARIO_NAME)
    fake = _ReminderSniffingLLM(lanes=autoprove_nag_lanes(), with_human_delay=False)
    _install_mocks(
        monkeypatch, scenario_dir, tape_installer=lambda: install_nag_tape(fake=fake)
    )
    # This test is about verify_spec's monitoring behavior, not proving — swap
    # the prover core for the canned status reports, and the rule-listing
    # pre-pass for the same spec-derived ground truth.
    monkeypatch.setattr("composer.spec.source.prover.run_prover", _fake_run_prover)
    monkeypatch.setattr("composer.spec.source.prover.declared_rules_list", _fake_declared_rules)

    # Run the whole pipeline. The first requirement is simply that the nag
    # path doesn't kill (or corrupt) the run: this raises if any phase dies.
    summary = RunSummary()
    async with autoprove_executor(
        _make_args(langgraph_db.rag_db, scenario_dir, str(scenario_dir / "system.md")),
        summary,
    ) as run:
        await run(AutoProveConsoleHandler().make_handler)

    # The nag reminder reached the author's conversation...
    nag_prompts = [
        (lane, [t for t in texts if _NAG_SNIPPET in t])
        for lane, texts in fake.sniffed
    ]
    nag_prompts = [(lane, ts) for lane, ts in nag_prompts if ts]
    assert nag_prompts, (
        "the stuck-rule nag never reached any prompt; system-reminders seen: "
        f"{fake.sniffed}"
    )
    # ...in the component-formalization lane, naming the stuck rule...
    assert all(lane == formalize_task_id(0) for lane, _ in nag_prompts)
    assert all(NAG_STUCK_RULE in t for _, ts in nag_prompts for t in ts)
    # ...and exactly once per prompt: the post-skip verify run must not re-nag,
    # and the reminders channel must drain on injection — either failure would
    # stack a second copy of the reminder into later prompts.
    assert all(len(ts) == 1 for _, ts in nag_prompts)
