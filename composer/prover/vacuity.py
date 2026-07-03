"""Vacuous-method detection over per-rule prover results.

A parametric method whose sanity check fails in every rule that instantiates it
(or in several rules while every other instantiation timed out or errored) is
*vacuous*: every path through it reverts under
the current verification model, so any rule instantiated with it passes
trivially. Per-rule ``SANITY_FAILED`` results already reach the verify loop
(``composer/prover/results.py`` attaches the ``METHOD_INSTANTIATION`` name to
``RulePath.method``); this module aggregates them into a per-method verdict,
renders the ``<vacuity_alert>`` the agent sees, and provides the checks backing
the ``verify_spec`` vacuity guards (undocumented ``filtered`` exclusions and
undocumented ``expect_rule_failure`` skips of the affected rules).

The near-universal root cause is a setup defect — canonically a NONDET summary
on a payable or side-effecting callee — so the alert prescribes a *repair
ladder* that puts root-cause fixes first and ``filtered`` exclusion last.
"""

import re
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
  4. ONLY as a last resort, exclude the method with a `filtered` block. The justification comment
     next to the filter MUST name which of steps 1-3 you attempted and why each failed — an
     undocumented filter of a vacuous method will not be accepted as a passing result."""


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


def format_filter_guard(blocked: list[str]) -> str:
    """Render the message returned by ``verify_spec`` when it withholds the
    PROVER validation stamp because vacuous methods are filtered without a
    documented repair attempt."""
    method_list = "\n".join(f"  - {m}" for m in sorted(blocked))
    return f"""\
<vacuity_filter_guard>
The PROVER validation stamp was WITHHELD. The following method(s) were previously detected as
VACUOUS (they revert on every path under the current verification model) and are now excluded via
a `filtered` block with no documented repair attempt:
{method_list}

Filtering a vacuous method hides a setup defect instead of fixing it. Either:
  * repair the root cause (repair ladder: 1. fix/replace the offending summary preserving payable
    semantics, 2. `write_mock` + `edit_config` AddFile/AddLink, 3. `optimistic_fallback` via
    `edit_config` set_flag), un-filter the method, and rerun `verify_spec`; or
  * if steps 1-3 genuinely failed, add a comment next to the `filtered` block documenting which of
    summary-fix / mock / optimistic_fallback you attempted and why each failed, then rerun
    `verify_spec`. The feedback judge will assess the quality of that justification.
</vacuity_filter_guard>"""


def format_skip_guard(blocked: list[str]) -> str:
    """Render the message returned by ``verify_spec`` when it withholds the
    PROVER validation stamp because the sanity-failing rules of vacuous methods
    were marked "expected to fail" without a documented repair attempt."""
    method_list = "\n".join(f"  - {m}" for m in sorted(blocked))
    return f"""\
<vacuity_skip_guard>
The PROVER validation stamp was WITHHELD. The following method(s) are VACUOUS (they revert on
every path under the current verification model) and their sanity-failing rules were marked
"expected to fail" via `expect_rule_failure` with no documented repair attempt:
{method_list}

Marking those rules as expected failures hides a setup defect just like filtering the method would.
Either:
  * repair the root cause (repair ladder: 1. fix/replace the offending summary preserving payable
    semantics, 2. `write_mock` + `edit_config` AddFile/AddLink, 3. `optimistic_fallback` via
    `edit_config` set_flag), un-mark the rules with `expect_rule_passage`, and rerun `verify_spec`; or
  * if steps 1-3 genuinely failed, re-call `expect_rule_failure` with a reason documenting which of
    summary-fix / mock / optimistic_fallback you attempted and why each failed, then rerun
    `verify_spec`. The feedback judge will assess the quality of that justification.
</vacuity_skip_guard>"""


def _bare_method_name(method: str) -> str:
    """``Bank.withdraw(uint256)`` -> ``withdraw``. Prover method-instantiation
    names are contract-qualified with a parameter list; filter expressions
    reference the bare Solidity name (e.g. ``sig:withdraw(uint256).selector``)."""
    return method.split("(", 1)[0].rsplit(".", 1)[-1]


def _filtered_blocks_with_context(spec: str, context_lines: int = 12) -> list[tuple[str, str]]:
    """Return ``(context, body)`` for every ``filtered { ... }`` block in the
    spec. ``body`` is the brace-delimited filter text; ``context`` additionally
    includes the ``context_lines`` lines preceding the ``filtered`` keyword,
    which is where a repair-attempt justification comment is expected to live.

    Deliberately a simple lexical scan (no CVL parser): filter bodies are
    single expressions, so naive brace matching suffices, and a rare miss only
    skips the guard — it can never spuriously block.
    """
    blocks: list[tuple[str, str]] = []
    for match in re.finditer(r"\bfiltered\b", spec):
        open_idx = spec.find("{", match.end())
        if open_idx == -1:
            continue
        depth = 0
        close_idx = -1
        for i in range(open_idx, len(spec)):
            if spec[i] == "{":
                depth += 1
            elif spec[i] == "}":
                depth -= 1
                if depth == 0:
                    close_idx = i
                    break
        if close_idx == -1:
            continue
        body = spec[open_idx : close_idx + 1]
        preceding = spec[: match.start()].splitlines()[-context_lines:]
        blocks.append(("\n".join(preceding) + "\n" + body, body))
    return blocks


# Lenient markers for "a repair-ladder step was attempted and documented". Substring match,
# case-insensitive: "summar" covers summary/summaries/summarized. Presence — not quality — is
# checked here; the feedback judge assesses whether the justification is actually convincing.
_REPAIR_ATTEMPT_MARKERS = ("summar", "mock", "optimistic_fallback")


def _documents_repair_attempt(context: str) -> bool:
    lowered = context.lower()
    return any(marker in lowered for marker in _REPAIR_ATTEMPT_MARKERS)


def undocumented_filtered_vacuous(spec: str, methods: Iterable[str]) -> list[str]:
    """The subset of ``methods`` (prover instantiation names) that appear in a
    ``filtered`` block of ``spec`` where neither the block nor the lines above
    it document a repair-ladder attempt.

    A method mentioned in several filters passes if ANY of them is documented
    — the false-block risk must stay low; the judge arbitrates quality.
    """
    blocks = _filtered_blocks_with_context(spec)
    blocked: list[str] = []
    for method in methods:
        # Token-boundary match, not raw substring: a filter excluding only
        # `depositAll` must not count as containing the vacuous `deposit`.
        name_re = re.compile(rf"\b{re.escape(_bare_method_name(method))}\b")
        containing = [ctx for (ctx, body) in blocks if name_re.search(body)]
        if containing and not any(_documents_repair_attempt(ctx) for ctx in containing):
            blocked.append(method)
    return sorted(blocked)


def undocumented_skipped_vacuous(
    evidence: dict[str, VacuityEvidence], rule_skips: dict[str, str]
) -> list[str]:
    """The subset of this run's vacuous methods with a sanity-failing rule
    excluded from the pass/fail verdict via a rule skip (``expect_rule_failure``)
    whose reason does not document a repair-ladder attempt.

    Closes the other mechanical bypass of the filtered-vacuous guard: instead of
    filtering the method, the agent can mark each of its sanity-failing rules
    "expected to fail" — those rules then drop out of ``verify_spec``'s
    all-verified check while the method never enters a filter body. Works off
    the current run's evidence (not the persisted verdicts) because only it
    names the affected rules; in the skip scenario the method stays
    instantiated, so detection re-fires on every run. Mirrors the filter
    check's leniency: one documented skip reason covering the method clears it.
    """
    blocked: list[str] = []
    for method, ev in evidence.items():
        reasons = [rule_skips[r] for r in ev.affected_rules if r in rule_skips]
        if reasons and not any(_documents_repair_attempt(reason) for reason in reasons):
            blocked.append(method)
    return sorted(blocked)
