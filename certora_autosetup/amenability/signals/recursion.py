"""Mutual-recursion signal: recursive call clusters, especially ones entered from
inside a loop.

Prover impact: the decompiler unfolds every recursive entry to a fixed depth on
every path, so the decompiled program grows combinatorially in the number of
entries. A recursive cluster reached from inside a loop multiplies out of the
block/command budgets and the run aborts during decompilation — before any rule
is checked. Raising those limits does not help; the growth is in the unfolding.

This is a whole-program hazard, not a proportional one: a few hundred lines of
recursive code in one vendored dependency can make a project of ten thousand
lines uningestible. That is why the signal is a structural killer (a severe score
vetoes `high` outright) rather than just another term in the weighted mean, and
why it is the one signal that analyses dependencies: the motivating case was a
vendored library implementing a bytecode interpreter, where every line of project
code was clean.

Curve constants live in weights.yaml under `signal_params`; DEFAULT_PARAMS below
mirrors them for callers that build an AnalysisContext without a scoring config.
"""

from dataclasses import dataclass

from certora_autosetup.amenability.callgraph import (
    Cluster,
    UnitGraph,
    build_unit_graph,
    unit_clusters,
)
from certora_autosetup.amenability.context import AnalysisContext, is_dependency_path
from certora_autosetup.amenability.report import Severity
from certora_autosetup.amenability.signals.base import SignalResult, clamp, make_evidence, signal

SIGNAL_ID = "mutual_recursion"

EVIDENCE_CAP = 10

# Mirrors weights.yaml `signal_params.mutual_recursion` (a unit test keeps them equal).
DEFAULT_PARAMS = {
    "self_cycle_cost": 0.15,
    "mutual_cycle_cost": 0.45,
    "mutual_size_step": 0.15,
    "loop_entry_multiplier": 1.7,
    "unreachable_multiplier": 0.35,
    "reentry_step": 0.15,
    "reentry_multiplier_cap": 1.6,
    "max_cycle_cost": 0.9,
}


@dataclass
class Finding:
    """One recursive cluster, merged across the compilation units containing it."""

    source_path: str  # anchor: the member defined earliest in the first source
    byte_offset: int
    members: tuple[str, ...]  # `Contract.function` labels, sorted
    loop_entered: bool = False
    reachable: bool = False
    entry_sites: int = 0
    in_dependency: bool = False

    @property
    def size(self) -> int:
        return len(self.members)

    def merge(self, other: "Finding") -> None:
        self.loop_entered |= other.loop_entered
        self.reachable |= other.reachable
        self.entry_sites = max(self.entry_sites, other.entry_sites)


def cluster_cost(finding: Finding, params: dict[str, float]) -> float:
    """How much of the score one cluster consumes, in [0, max_cycle_cost].

    Mutual recursion costs more than self-recursion (the unfolding branches over
    the whole cluster), a bigger cluster more than a smaller one, a loop-entered
    cluster far more than a straight-line one (every iteration is another entry),
    and more distinct entry sites more than one. A cluster that no deployable
    contract can reach is damped, not dropped — vendored dead code still compiles
    into the scene, but it does not decide a verdict.
    """

    def p(key: str) -> float:
        return params.get(key, DEFAULT_PARAMS[key])

    if finding.size == 1:
        base = p("self_cycle_cost")
    else:
        base = p("mutual_cycle_cost") + p("mutual_size_step") * (finding.size - 2)
    loop = p("loop_entry_multiplier") if finding.loop_entered else 1.0
    live = 1.0 if finding.reachable else p("unreachable_multiplier")
    reentry = min(
        p("reentry_multiplier_cap"),
        1.0 + p("reentry_step") * max(0, finding.entry_sites - 1),
    )
    return min(p("max_cycle_cost"), clamp(base) * loop * live * reentry)


def score_findings(findings: list[Finding], params: dict[str, float]) -> float:
    """Saturating product over clusters: each one takes its cost out of what is
    left, so the score is monotone in every cluster's cost and in adding clusters,
    and no single cluster can zero it."""
    score = 1.0
    for finding in findings:
        score *= 1.0 - cluster_cost(finding, params)
    return clamp(score)


def _severity(finding: Finding) -> Severity:
    if finding.loop_entered and finding.reachable:
        return Severity.HIGH
    if finding.size > 1:
        return Severity.MEDIUM
    return Severity.LOW


def _detail(finding: Finding) -> str:
    shape = (
        "self-recursive function"
        if finding.size == 1
        else f"mutually recursive cluster of {finding.size} functions"
    )
    where = "entered from inside a loop" if finding.loop_entered else "no loop-entered call site"
    live = "reachable from a public entry point" if finding.reachable else "not reachable from any public entry point"
    members = ", ".join(finding.members[:4])
    if finding.size > 4:
        members += ", ..."
    return f"{shape} ({members}); {where}; {live}"


def collect_findings(ctx: AnalysisContext) -> list[Finding]:
    """Recursive clusters of the whole project, deduplicated across compilation
    units (a vendored dependency is recompiled into every unit that imports it)."""
    findings: dict[frozenset[tuple[str, str]], Finding] = {}
    for unit in ctx.iter_units():
        graph = build_unit_graph(unit)
        for cluster in unit_clusters(graph):
            finding = _to_finding(graph, cluster)
            if finding is None:
                continue
            key = frozenset(
                (ctx.display_path(graph.nodes[m].source_path), graph.nodes[m].label)
                for m in cluster.members
                if m in graph.nodes
            )
            existing = findings.get(key)
            if existing is None:
                findings[key] = finding
            else:
                existing.merge(finding)
    return sorted(findings.values(), key=lambda f: (f.source_path, f.byte_offset))


def _to_finding(graph: UnitGraph, cluster: Cluster) -> Finding | None:
    members = [graph.nodes[m] for m in cluster.members if m in graph.nodes]
    if not members:
        return None
    anchor = min(members, key=lambda n: (n.source_path, n.byte_offset))
    return Finding(
        source_path=anchor.source_path,
        byte_offset=anchor.byte_offset,
        members=tuple(sorted(n.label for n in members)),
        loop_entered=cluster.loop_entered,
        reachable=cluster.reachable,
        entry_sites=cluster.entry_sites,
        in_dependency=any(is_dependency_path(n.source_path) for n in members),
    )


@signal(SIGNAL_ID)
def mutual_recursion(ctx: AnalysisContext) -> SignalResult:
    params = ctx.params.get(SIGNAL_ID, {})
    findings = collect_findings(ctx)
    score = score_findings(findings, params)

    evidence = []
    # Worst clusters first, so the cap keeps the ones that moved the score.
    for finding in sorted(findings, key=lambda f: -cluster_cost(f, params))[:EVIDENCE_CAP]:
        evidence.append(make_evidence(
            ctx, SIGNAL_ID, _severity(finding),
            finding.source_path, finding.byte_offset,
            _detail(finding),
            function=finding.members[0],
        ))

    return SignalResult(
        signal_id=SIGNAL_ID,
        score=score,
        evidence=evidence,
        raw={
            "recursive_clusters": len(findings),
            "loop_entered_clusters": sum(1 for f in findings if f.loop_entered),
            "reachable_clusters": sum(1 for f in findings if f.reachable),
            "clusters_in_dependencies": sum(1 for f in findings if f.in_dependency),
            "largest_cluster": max((f.size for f in findings), default=0),
            "functions_in_cycles": sum(f.size for f in findings),
        },
    )
