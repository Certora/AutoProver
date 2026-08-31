"""Unit tests for per-group completion / pending-rule selection (Layer 2 engine).

Pure logic over synthetic prover history — no prover. Focuses on the incremental
re-run behavior and the interleaving hazard: because groups have distinct spec
digests and ``_iterate_history`` stops at the first foreign digest, a group's
completion must be computed over its own filtered history, not the flat list.
"""

from composer.prover.ptypes import RulePath
from composer.spec.source.prover import (
    ProverRunLog,
    group_is_complete,
    group_pending_rules,
)


def _run(group, digest, results, *, tcid="t", declared=None):
    """A ProverRunLog with the given (rule, status) results at a digest+group."""
    paths = [(RulePath(rule=r), s) for r, s in results]
    return ProverRunLog(
        tool_call_id=tcid,
        prover_results=paths,
        spec_digest=digest,
        rules=None,
        sort="run",
        declared_rules=declared if declared is not None else [r for r, _ in results],
        state_digest=digest,
        group=group,
    )


# --- pending-rule selection (incremental engine) ----------------------------


def test_pending_excludes_already_verified():
    hist = [_run("A", "dA", [("r1", "VERIFIED"), ("r2", "VIOLATED")])]
    pending = group_pending_rules(
        hist, group="A", curr_digest="dA", owned_rules={"r1", "r2"}, expected_to_fail=set()
    )
    assert pending == {"r2"}  # r1 done, r2 still needs work


def test_pending_includes_never_run():
    pending = group_pending_rules(
        [], group="A", curr_digest="dA", owned_rules={"r1", "r2"}, expected_to_fail=set()
    )
    assert pending == {"r1", "r2"}


def test_pending_uses_latest_verdict_not_ever_verified():
    # r1 verified in an older run, then TIMEOUT in a newer run at the same digest:
    # it is still pending (latest verdict wins), so it must be re-run.
    hist = [
        _run("A", "dA", [("r1", "VERIFIED")], tcid="old"),
        _run("A", "dA", [("r1", "TIMEOUT")], tcid="new"),
    ]
    pending = group_pending_rules(
        hist, group="A", curr_digest="dA", owned_rules={"r1"}, expected_to_fail=set()
    )
    assert pending == {"r1"}


def test_pending_forgives_expected_to_fail():
    hist = [_run("A", "dA", [("r1", "VIOLATED")])]
    pending = group_pending_rules(
        hist, group="A", curr_digest="dA", owned_rules={"r1"}, expected_to_fail={"r1"}
    )
    assert pending == set()


def test_pending_ignores_other_group_and_stale_digest():
    hist = [
        _run("B", "dB", [("r1", "VERIFIED")]),        # wrong group
        _run("A", "OLD", [("r1", "VERIFIED")]),       # right group, stale spec digest
    ]
    pending = group_pending_rules(
        hist, group="A", curr_digest="dA", owned_rules={"r1"}, expected_to_fail=set()
    )
    assert pending == {"r1"}  # neither counts


# --- the interleaving hazard ------------------------------------------------


def test_interleaved_foreign_group_does_not_truncate_completion():
    # Group A verified all its rules at dA (run 1). Then group B ran at dB (run 2,
    # a different spec/digest). A flat _iterate_history for dA would STOP at run 2
    # (foreign digest) and lose run 1 -> falsely incomplete. Filtering to group A's
    # own history first must keep A complete.
    hist = [
        _run("A", "dA", [("r1", "VERIFIED"), ("r2", "VERIFIED")]),
        _run("B", "dB", [("r3", "VIOLATED")]),
    ]
    assert group_is_complete(
        hist, group="A", curr_digest="dA", expected_to_fail=set(),
        curr_status=[], owned_rules={"r1", "r2"},
    )
    # ...and pending for A is empty despite the interleaved B run.
    assert group_pending_rules(
        hist, group="A", curr_digest="dA", owned_rules={"r1", "r2"}, expected_to_fail=set()
    ) == set()


def test_group_complete_requires_all_owned_verified():
    hist = [_run("A", "dA", [("r1", "VERIFIED")])]
    assert not group_is_complete(
        hist, group="A", curr_digest="dA", expected_to_fail=set(),
        curr_status=[], owned_rules={"r1", "r2"},  # r2 never verified
    )


def test_group_complete_counts_curr_status():
    # r2 verified in the just-finished run (curr_status), not yet in history.
    hist = [_run("A", "dA", [("r1", "VERIFIED")])]
    assert group_is_complete(
        hist, group="A", curr_digest="dA", expected_to_fail=set(),
        curr_status=[(RulePath(rule="r2"), "VERIFIED")], owned_rules={"r1", "r2"},
    )


def test_untagged_history_is_default_group_none():
    # Backward compat: a legacy run with no `group` key belongs to group None.
    legacy = ProverRunLog(
        tool_call_id="t", prover_results=[(RulePath(rule="r1"), "VERIFIED")],
        spec_digest="dA", rules=None, sort="run", declared_rules=["r1"], state_digest="dA",
    )
    assert group_is_complete(
        [legacy], group=None, curr_digest="dA", expected_to_fail=set(),
        curr_status=[], owned_rules={"r1"},
    )


# --- plan_group_execution (parallel-run decision) ---------------------------

from composer.spec.source.prover import GroupRun, plan_group_execution  # noqa: E402
from composer.spec.source.verification_groups import VerificationGroup  # noqa: E402


def _grp(name, rules, spec=None):
    return VerificationGroup(name=name, owned_rules=frozenset(rules), spec_contents=spec)


def _digest_by_name(g):  # each group a distinct digest keyed by its name
    return f"d_{g.name}"


def test_plan_skips_fully_covered_group():
    # Group A already verified both owned rules at its digest; group B has none run.
    hist = [_run("A", "d_A", [("r1", "VERIFIED"), ("r2", "VERIFIED")])]
    plan = plan_group_execution(
        [_grp("A", ["r1", "r2"]), _grp("B", ["r3", "r4"])],
        history=hist, all_rules=["r1", "r2", "r3", "r4"],
        agent_rules=None, agent_exclude=None, expected_to_fail=set(),
        digest_of=_digest_by_name,
    )
    by = {gr.group.name: gr for gr in plan}
    assert by["A"].pending == frozenset()          # skipped
    assert by["B"].pending == frozenset({"r3", "r4"})


def test_plan_reruns_only_pending_within_group():
    hist = [_run("A", "d_A", [("r1", "VERIFIED"), ("r2", "TIMEOUT")])]
    plan = plan_group_execution(
        [_grp("A", ["r1", "r2"])], history=hist, all_rules=["r1", "r2"],
        agent_rules=None, agent_exclude=None, expected_to_fail=set(), digest_of=_digest_by_name,
    )
    assert plan[0].pending == frozenset({"r2"})     # r1 stays verified, only r2 re-runs


def test_plan_intersects_agent_rule_selection():
    plan = plan_group_execution(
        [_grp("A", ["r1", "r2"]), _grp("B", ["r3"])],
        history=[], all_rules=["r1", "r2", "r3"],
        agent_rules=["r2"], agent_exclude=None, expected_to_fail=set(), digest_of=_digest_by_name,
    )
    by = {gr.group.name: gr for gr in plan}
    assert by["A"].pending == frozenset({"r2"})     # only the agent-selected rule
    assert by["B"].pending == frozenset()           # r3 not selected -> skipped


def test_plan_honors_exclude_selection():
    plan = plan_group_execution(
        [_grp("A", ["r1", "r2"])], history=[], all_rules=["r1", "r2"],
        agent_rules=None, agent_exclude=["r1"], expected_to_fail=set(), digest_of=_digest_by_name,
    )
    assert plan[0].pending == frozenset({"r2"})


def test_plan_records_per_group_digest():
    plan = plan_group_execution(
        [_grp("A", ["r1"]), _grp("B", ["r2"])], history=[], all_rules=["r1", "r2"],
        agent_rules=None, agent_exclude=None, expected_to_fail=set(), digest_of=_digest_by_name,
    )
    assert {gr.group.name: gr.digest for gr in plan} == {"A": "d_A", "B": "d_B"}
