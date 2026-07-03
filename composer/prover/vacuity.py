"""Vacuous-method detection over per-rule prover results.

A parametric method whose sanity check fails in every rule that instantiates it
(or in several rules while every other instantiation timed out or errored) is
*vacuous*: every path through it reverts under
the current verification model, so any rule instantiated with it passes
trivially. Per-rule ``SANITY_FAILED`` results already reach the verify loop
(``composer/prover/results.py`` attaches the ``METHOD_INSTANTIATION`` name to
``RulePath.method``); this module aggregates them into a per-method verdict and
renders the ``<vacuity_alert>`` the agent sees plus the ``verify_spec`` guard
message. The guard itself is purely structural (no spec-text scanning): a
verdict persists in graph state until a run re-instantiates the method
healthily, so *any* route that hides the method (a ``filtered`` block, marking
its rules "expected to fail", deleting the rule) leaves the verdict
outstanding, and the PROVER stamp is withheld unless the agent formally
acknowledges the method via the ``acknowledge_vacuous_method`` tool.

The near-universal root cause is a setup defect — canonically a NONDET summary
on a payable or side-effecting callee — so the alert prescribes a *repair
ladder* that puts root-cause fixes first and ``filtered`` exclusion last.
"""

from typing import Iterable

from pydantic import BaseModel

from composer.prover.ptypes import RuleResult


class VacuityEvidence(BaseModel):
    """Why a method was flagged as vacuous: the rules whose sanity check it
    failed, and a one-line diagnosis suitable for reports and agent state."""

    method: str
    affected_rules: list[str]
    diagnosis: str


def detect_vacuous_methods(results: Iterable[RuleResult]) -> dict[str, VacuityEvidence]:
    """Group ``SANITY_FAILED`` results by instantiated method and flag the
    methods that are sanity-failed in 100% of the rules that instantiate them,
    OR in >= 2 rules while no instantiation reached a healthy verdict.

    The 100% arm catches single-rule runs. The >= 2 arm catches a method that
    is vacuous across the board when the remaining instantiations are
    TIMEOUT/ERROR/SKIPPED — statuses that can *mask* an all-paths-reverting
    method. A VERIFIED or VIOLATED instantiation, by contrast, requires
    actually reaching the method's code, so it *disproves* all-paths
    reversion: sanity failures alongside a healthy instantiation stem from
    the failing rules' own preconditions (a rule-authoring problem), not
    method vacuity, and must not be flagged here. A sanity failure on a
    non-parametric rule (``path.method is None``) is likewise a
    rule-authoring problem and is ignored.
    """
    instantiating: dict[str, set[str]] = {}
    sanity_failed: dict[str, set[str]] = {}
    healthy: dict[str, set[str]] = {}
    for r in results:
        method = r.path.method
        if method is None:
            continue
        instantiating.setdefault(method, set()).add(r.path.rule)
        if r.status == "SANITY_FAILED":
            sanity_failed.setdefault(method, set()).add(r.path.rule)
        elif r.status in ("VERIFIED", "VIOLATED"):
            healthy.setdefault(method, set()).add(r.path.rule)

    flagged: dict[str, VacuityEvidence] = {}
    for method, failed_rules in sanity_failed.items():
        all_rules = instantiating[method]
        everywhere = failed_rules == all_rules
        masked_majority = len(failed_rules) >= 2 and not healthy.get(method)
        if not (everywhere or masked_majority):
            continue
        flagged[method] = VacuityEvidence(
            method=method,
            affected_rules=sorted(failed_rules),
            diagnosis=(
                f"sanity-failed in {len(failed_rules)} of {len(all_rules)} rule(s) "
                "that instantiate it: the method reverts on every path under the "
                "current verification model, so rules over it pass vacuously"
            ),
        )
    return flagged


def instantiated_methods(results: Iterable[RuleResult]) -> set[str]:
    """Every method name instantiated in this result set (any status). Used to
    clear a stale vacuity verdict once a later run shows the method healthy."""
    return {r.path.method for r in results if r.path.method is not None}


_REPAIR_LADDER = """\
This is almost always a SETUP defect, not a property error. The canonical culprits are:
  * a NONDET/CONSTANT summary applied to a payable or side-effecting callee (the summary drops
    the callee's effects — e.g. its ability to accept msg.value — making the caller's success
    path infeasible),
  * an unresolved low-level `.call{value: ...}` that HAVOCs and can always be assumed to fail,
  * a missing `link`, leaving a callee address unresolved.

Repair in this order (the "repair ladder"); do NOT jump to a later step without attempting the earlier ones:
  1. Fix or replace the offending summary so it preserves the callee's semantics — in particular
     payable-ness: a payable callee must be able to accept the transferred value.
  2. Write a minimal mock contract under `certora/mocks` (the `write_mock` tool) implementing the
     callee's interface, and register it in the prover config with `edit_config` (AddFile, plus
     AddLink if the callee is reached through a storage field).
  3. Set `optimistic_fallback` to true via `edit_config` (set_flag) so unresolved calls are assumed
     to succeed instead of always being able to revert.
  4. ONLY as a last resort, formally acknowledge the method with the `acknowledge_vacuous_method`
     tool, recording which of steps 1-3 you attempted and why each failed, and then exclude it
     with a `filtered` block. Hiding a vacuous method (filtering it, marking its rules "expected
     to fail", or deleting the rule) without that acknowledgment will not be accepted as a
     passing result."""


def format_vacuity_alert(evidence: dict[str, VacuityEvidence]) -> str:
    """Render the ``<vacuity_alert>`` block appended to the prover report the
    agent sees. Empty string when nothing was flagged."""
    if not evidence:
        return ""
    lines = [
        "<vacuity_alert>",
        "The following method(s) are VACUOUS — every rule instantiated with them holds trivially",
        "because their assertions are unreachable:",
    ]
    for method in sorted(evidence):
        ev = evidence[method]
        lines.append(f"  - {method}: {ev.diagnosis} (rules: {', '.join(ev.affected_rules)})")
    lines.append("")
    lines.append(_REPAIR_LADDER)
    lines.append("</vacuity_alert>")
    return "\n".join(lines)


def format_vacuity_guard(blocked: dict[str, str]) -> str:
    """Render the message returned by ``verify_spec`` when it withholds the
    PROVER validation stamp: ``blocked`` maps each still-outstanding vacuous
    method (no healthy re-instantiation, no acknowledgment) to its diagnosis.

    The gate feeding this is purely structural — set membership over the
    persisted verdicts and the acknowledgment ledger, never spec-text scanning
    — so the message covers every hiding route (``filtered`` blocks,
    ``expect_rule_failure`` skips, rule deletion) uniformly.
    """
    method_list = "\n".join(f"  - {m}: {d}" for m, d in sorted(blocked.items()))
    return f"""\
<vacuity_guard>
The PROVER validation stamp was WITHHELD. The following method(s) were detected as VACUOUS
(they revert on every path under the current verification model), and no later run has shown
them healthy, nor have they been acknowledged:
{method_list}

A run where every rule passes while a known-vacuous method is hidden (via a `filtered` block,
`expect_rule_failure` on its sanity-failing rules, or removing the rule) hides a setup defect
instead of fixing it. Either:
  * repair the root cause (repair ladder: 1. fix/replace the offending summary preserving payable
    semantics, 2. `write_mock` + `edit_config` add_file/add_link, 3. `optimistic_fallback` via
    `edit_config` set_flag), re-include the method, and rerun `verify_spec` — a run that
    instantiates the method without a sanity failure clears its verdict automatically; or
  * if steps 1-3 genuinely failed, call `acknowledge_vacuous_method` for the method, recording
    which steps you attempted and why each failed, then rerun `verify_spec`. The feedback judge
    will audit the quality of that acknowledgment.
</vacuity_guard>"""
